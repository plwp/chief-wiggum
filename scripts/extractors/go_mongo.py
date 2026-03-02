#!/usr/bin/env python3
"""
Go + MongoDB extractor for stitch-audit.

Handles:
- Go struct definitions with json:"tag" and bson:"tag" extraction
- bson.M{...} operation key extraction (InsertOne, UpdateOne, FindOne patterns)
- Handler file classification (gin.Context, http.Handler, echo.Context markers)
- Pattern scanning: json/bson tag style distribution, tag coverage

Limitations (Tier 1 regex):
- Embedded (anonymous) structs noted as "inherited" but fields not inlined
- Nested bson.M inside $set / $push only partially captured
- Complex multi-line struct tags may miss edge cases
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .base import Extractor, Field, Schema

# Markers that indicate a Go file is an HTTP handler
_HANDLER_MARKERS = re.compile(
    r"gin\.Context|echo\.Context|http\.Handler|http\.Request|chi\.Router|mux\.Router"
)

# Markers that indicate a Go file does MongoDB operations
_MONGO_MARKERS = re.compile(
    r"bson\.M\b|bson\.D\b|mongo\.Collection|InsertOne|UpdateOne|FindOne|Find\("
)

# Match a Go struct definition
_STRUCT_RE = re.compile(r"^type\s+(\w+)\s+struct\s*\{", re.MULTILINE)

# Match a struct field line:  Name Type `json:"x" bson:"y"`
_FIELD_RE = re.compile(
    r"^\s+(\w+)\s+(\S+)"  # name, type
    r"(?:\s+`([^`]+)`)?"  # optional backtick tags
    r"\s*$",
    re.MULTILINE,
)

# Extract a specific tag value: json:"name,omitempty" -> name
_TAG_RE = re.compile(r'(\w+):"([^"]*)"')

# bson.M{ "key": value } — capture keys inside bson.M literals
_BSON_M_KEY_RE = re.compile(r'"(\w+)"(?:\s*:)')

# MongoDB operation patterns: collection.Method(ctx, bson.M{...})
_MONGO_OP_RE = re.compile(
    r"\.\s*(InsertOne|UpdateOne|UpdateMany|FindOne|Find|DeleteOne|DeleteMany|Aggregate)"
    r"\s*\([^)]*bson\.M\s*\{([^}]*)\}",
    re.DOTALL,
)

# Extended pattern: match bson.M blocks that may span lines
_BSON_M_BLOCK_RE = re.compile(r"bson\.M\s*\{([^}]*)\}", re.DOTALL)


class GoMongoExtractor(Extractor):
    """Extract schemas from Go structs, bson tags, and MongoDB operations."""

    def name(self) -> str:
        return "go_mongo"

    def detect(self, repo_path: Path) -> bool:
        """Check for Go files with bson imports or MongoDB usage."""
        for go_file in repo_path.rglob("*.go"):
            if go_file.is_relative_to(repo_path / "vendor"):
                continue
            try:
                content = go_file.read_text(errors="replace")
                if "bson" in content or _MONGO_MARKERS.search(content):
                    return True
            except OSError:
                continue
        return False

    def discover(self, repo_path: Path, keyword: str) -> dict[str, list[Path]]:
        """Find Go files related to keyword, classified by layer."""
        kw_lower = keyword.lower()
        result: dict[str, list[Path]] = {
            "api_handlers": [],
            "database_ops": [],
        }

        for go_file in repo_path.rglob("*.go"):
            if go_file.is_relative_to(repo_path / "vendor"):
                continue
            # Skip test files — test data setup is not production DB ops
            if go_file.name.endswith("_test.go"):
                continue
            try:
                content = go_file.read_text(errors="replace")
            except OSError:
                continue

            # Check if file mentions the keyword
            if kw_lower not in content.lower():
                continue

            # Classify by layer
            if _HANDLER_MARKERS.search(content):
                result["api_handlers"].append(go_file)
            if _MONGO_MARKERS.search(content):
                result["database_ops"].append(go_file)
            # A file can appear in both — that's fine for a handler that
            # also does direct DB ops.

        return {k: v for k, v in result.items() if v}

    def extract(self, file_path: Path, keyword: str | None = None) -> list[Schema]:
        """Extract struct definitions and bson.M operations from a Go file."""
        try:
            content = file_path.read_text(errors="replace")
        except OSError:
            return []

        lines = content.split("\n")
        schemas: list[Schema] = []

        # Determine layer
        is_handler = bool(_HANDLER_MARKERS.search(content))
        is_mongo = bool(_MONGO_MARKERS.search(content))

        # 1. Extract struct definitions
        for match in _STRUCT_RE.finditer(content):
            struct_name = match.group(1)

            # If keyword given, skip structs that don't relate to it
            if keyword and keyword.lower() not in struct_name.lower():
                # Also check if the struct is in a keyword-related file
                if keyword.lower() not in file_path.stem.lower():
                    continue

            struct_start = match.start()
            start_line = content[:struct_start].count("\n") + 1

            # Find the closing brace
            brace_depth = 0
            struct_end = struct_start
            for i, ch in enumerate(content[struct_start:], struct_start):
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        struct_end = i
                        break

            struct_body = content[struct_start:struct_end + 1]
            fields = self._extract_struct_fields(struct_body, start_line)

            if not fields:
                continue

            layer = "api_handlers" if is_handler else "database_ops"
            schemas.append(Schema(
                file=str(file_path),
                layer=layer,
                schema_type="go_struct",
                name=struct_name,
                fields=fields,
            ))

        # 2. Extract bson.M operation keys
        if is_mongo:
            bson_schemas = self._extract_bson_ops(content, file_path, keyword)
            schemas.extend(bson_schemas)

        return schemas

    def _extract_struct_fields(self, struct_body: str, base_line: int) -> list[Field]:
        """Parse fields from a Go struct body."""
        fields: list[Field] = []
        struct_lines = struct_body.split("\n")

        for i, line in enumerate(struct_lines[1:], 1):  # Skip the type...struct{ line
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("//") or line_stripped == "}":
                continue

            # Embedded struct (no type, just a name)
            if re.match(r"^\s*\*?\w+\s*$", line_stripped.split("`")[0].strip()):
                continue

            field_match = _FIELD_RE.match(line)
            if not field_match:
                continue

            fname = field_match.group(1)
            ftype = field_match.group(2)
            tags_str = field_match.group(3) or ""

            # Parse struct tags
            tags: dict[str, str] = {}
            for tag_match in _TAG_RE.finditer(tags_str):
                tag_key = tag_match.group(1)
                tag_val = tag_match.group(2).split(",")[0]  # Strip ",omitempty" etc.
                if tag_val == "-":
                    tags[f"{tag_key}_tag"] = "-"  # Explicitly ignored
                else:
                    tags[f"{tag_key}_tag"] = tag_val

            # Determine required from validate tag
            required = None
            if "validate" in tags_str:
                validate_match = re.search(r'validate:"([^"]*)"', tags_str)
                if validate_match:
                    validate_val = validate_match.group(1)
                    tags["validator"] = validate_val
                    if "required" in validate_val:
                        required = True

            # Determine required from pointer type
            if ftype.startswith("*"):
                if required is None:
                    required = False

            fields.append(Field(
                name=fname,
                type=ftype,
                line=base_line + i,
                required=required,
                tags=tags,
            ))

        return fields

    def _extract_bson_ops(
        self, content: str, file_path: Path, keyword: str | None
    ) -> list[Schema]:
        """Extract field names from bson.M{} operations."""
        schemas: list[Schema] = []

        for op_match in _MONGO_OP_RE.finditer(content):
            op_name = op_match.group(1)
            bson_body = op_match.group(2)

            # Check keyword relevance
            if keyword:
                # Look at surrounding context (100 chars before the op)
                ctx_start = max(0, op_match.start() - 200)
                context = content[ctx_start:op_match.end()]
                if keyword.lower() not in context.lower():
                    continue

            op_line = content[:op_match.start()].count("\n") + 1
            keys = _BSON_M_KEY_RE.findall(bson_body)

            # Filter out MongoDB operators ($set, $push, etc.)
            fields = []
            for key in keys:
                if key.startswith("$"):
                    # Look inside the operator's value for actual field names
                    # e.g., "$set": bson.M{"field1": val, "field2": val}
                    op_block_re = re.compile(
                        rf'"\${re.escape(key[1:])}":\s*bson\.M\s*\{{([^}}]*)\}}',
                        re.DOTALL,
                    )
                    for inner_match in op_block_re.finditer(bson_body):
                        inner_keys = _BSON_M_KEY_RE.findall(inner_match.group(1))
                        for inner_key in inner_keys:
                            if not inner_key.startswith("$"):
                                fields.append(Field(
                                    name=inner_key,
                                    type=None,
                                    line=op_line,
                                    tags={"bson_tag": inner_key, "mongo_op": op_name},
                                ))
                else:
                    fields.append(Field(
                        name=key,
                        type=None,
                        line=op_line,
                        tags={"bson_tag": key, "mongo_op": op_name},
                    ))

            if fields:
                schemas.append(Schema(
                    file=str(file_path),
                    layer="database_ops",
                    schema_type="bson_m_op",
                    name=f"{op_name}@L{op_line}",
                    fields=fields,
                ))

        return schemas

    def scan_patterns(self, repo_path: Path, scan_path: str | None = None) -> dict:
        """Scan Go files for convention inconsistencies."""
        root = repo_path / scan_path if scan_path else repo_path

        json_styles: Counter[str] = Counter()  # snake_case, camelCase, etc.
        bson_styles: Counter[str] = Counter()
        missing_json = 0
        missing_bson = 0
        total_fields = 0
        # Track fields in structs that have at least one json tag
        # (bson-only internal structs shouldn't count as "missing json")
        serialized_structs_missing_json = 0

        for go_file in root.rglob("*.go"):
            if go_file.is_relative_to(repo_path / "vendor"):
                continue
            if go_file.name.endswith("_test.go"):
                continue
            try:
                content = go_file.read_text(errors="replace")
            except OSError:
                continue

            # Process struct by struct to track which have json tags
            for struct_match in _STRUCT_RE.finditer(content):
                struct_start = struct_match.start()
                brace_depth = 0
                struct_end = struct_start
                for i, ch in enumerate(content[struct_start:], struct_start):
                    if ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1
                        if brace_depth == 0:
                            struct_end = i
                            break

                struct_body = content[struct_start:struct_end + 1]
                struct_fields = list(_FIELD_RE.finditer(struct_body))
                if not struct_fields:
                    continue

                has_any_json = any(
                    'json:"' in (m.group(3) or "") for m in struct_fields
                )

                for match in struct_fields:
                    tags_str = match.group(3) or ""
                    total_fields += 1

                    json_match = re.search(r'json:"([^"]*)"', tags_str)
                    if json_match:
                        tag_val = json_match.group(1).split(",")[0]
                        if tag_val != "-":
                            json_styles[_classify_naming(tag_val)] += 1
                    else:
                        missing_json += 1
                        # Only count as "should have json" if the struct
                        # has other fields with json tags (i.e., it's
                        # serialized, not a bson-only internal struct)
                        if has_any_json:
                            serialized_structs_missing_json += 1

                    bson_match = re.search(r'bson:"([^"]*)"', tags_str)
                    if bson_match:
                        tag_val = bson_match.group(1).split(",")[0]
                        if tag_val != "-":
                            bson_styles[_classify_naming(tag_val)] += 1
                    else:
                        missing_bson += 1

        return {
            "extractor": self.name(),
            "total_fields": total_fields,
            "json_tag_styles": dict(json_styles),
            "bson_tag_styles": dict(bson_styles),
            "missing_json_tags": missing_json,
            "missing_json_tags_in_serialized_structs": serialized_structs_missing_json,
            "missing_bson_tags": missing_bson,
        }


def _classify_naming(name: str) -> str:
    """Classify a name as snake_case, camelCase, PascalCase, or other."""
    if "_" in name:
        return "snake_case"
    if name[0].isupper():
        return "PascalCase"
    if name[0].islower() and any(c.isupper() for c in name[1:]):
        return "camelCase"
    if name.islower():
        return "lowercase"
    return "other"

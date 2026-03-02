#!/usr/bin/env python3
"""
TypeScript extractor for stitch-audit.

Handles:
- TypeScript interface and type definitions
- Zod schema (z.object({...})) field extraction with validator types
- Form field extraction (register("field"), name="field")
- Admin view classification (path-based: admin/manage/dashboard)
- Pattern scanning: naming convention distribution

Limitations (Tier 1 regex):
- Pick<T, K>, Omit<T, K>, mapped types not resolved
- Multi-file type composition (extends across files) not traced
- Complex Zod chains (.transform, .pipe) partially captured
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .base import Extractor, Field, Schema

# TypeScript interface/type definition
_INTERFACE_RE = re.compile(
    r"^(?:export\s+)?(?:interface|type)\s+(\w+)(?:\s+extends\s+[\w,\s<>]+)?\s*(?:=\s*)?\{",
    re.MULTILINE,
)

# Interface/type field line:  name: Type;  or  name?: Type;
_TS_FIELD_RE = re.compile(
    r"^\s+(\w+)(\??):\s*(.+?)\s*;?\s*$",
    re.MULTILINE,
)

# Zod schema: z.object({ ... })
_ZOD_SCHEMA_RE = re.compile(
    r"(?:const|let|var|export\s+const)\s+(\w+)\s*=\s*z\.object\s*\(\s*\{",
    re.MULTILINE,
)

# Zod field:  fieldName: z.string().optional(),
_ZOD_FIELD_RE = re.compile(
    r"^\s+(\w+):\s*(z\.\w+\([^)]*\)(?:\.\w+\([^)]*\))*)",
    re.MULTILINE,
)

# React Hook Form register("fieldName") or register('fieldName')
_REGISTER_RE = re.compile(r'register\s*\(\s*["\'](\w+(?:\.\w+)*)["\']')

# HTML/JSX name="fieldName" or name={'fieldName'}
_NAME_ATTR_RE = re.compile(r'name\s*=\s*["\'{](\w+(?:\.\w+)*)["\'}]')

# Admin/manage/dashboard path markers
_ADMIN_PATH_RE = re.compile(r"admin|manage|dashboard", re.IGNORECASE)

# Form markers — files that are likely form components
_FORM_MARKERS = re.compile(
    r"<form|useForm|FormProvider|handleSubmit|onSubmit|register\(|<Form"
)


class TypeScriptExtractor(Extractor):
    """Extract schemas from TypeScript interfaces, Zod schemas, and form fields."""

    def name(self) -> str:
        return "typescript"

    def detect(self, repo_path: Path) -> bool:
        """Check for TypeScript files in the repo."""
        for pattern in ("*.ts", "*.tsx"):
            for ts_file in repo_path.rglob(pattern):
                if "node_modules" in ts_file.parts:
                    continue
                return True
        return False

    def discover(self, repo_path: Path, keyword: str) -> dict[str, list[Path]]:
        """Find TypeScript files related to keyword, classified by layer."""
        kw_lower = keyword.lower()
        result: dict[str, list[Path]] = {
            "frontend_forms": [],
            "api_handlers": [],
            "admin_views": [],
        }

        for pattern in ("*.ts", "*.tsx"):
            for ts_file in repo_path.rglob(pattern):
                if "node_modules" in ts_file.parts or ".next" in ts_file.parts:
                    continue

                try:
                    content = ts_file.read_text(errors="replace")
                except OSError:
                    continue

                if kw_lower not in content.lower() and kw_lower not in str(ts_file).lower():
                    continue

                rel = str(ts_file.relative_to(repo_path))

                # Classify by layer
                if _ADMIN_PATH_RE.search(rel):
                    result["admin_views"].append(ts_file)
                elif _FORM_MARKERS.search(content):
                    result["frontend_forms"].append(ts_file)
                elif self._is_api_handler(content, rel):
                    result["api_handlers"].append(ts_file)
                else:
                    # Default: check if it defines types/interfaces (shared types)
                    if _INTERFACE_RE.search(content) or _ZOD_SCHEMA_RE.search(content):
                        result["api_handlers"].append(ts_file)
                    elif _FORM_MARKERS.search(content):
                        result["frontend_forms"].append(ts_file)

        return {k: v for k, v in result.items() if v}

    def _is_api_handler(self, content: str, rel_path: str) -> bool:
        """Check if file is an API route handler."""
        api_markers = (
            "NextApiRequest",
            "NextApiResponse",
            "NextRequest",
            "NextResponse",
            "express.Router",
            "app.get(",
            "app.post(",
            "router.get(",
            "router.post(",
        )
        # Path-based detection
        if "/api/" in rel_path or "/routes/" in rel_path:
            return True
        return any(m in content for m in api_markers)

    def extract(self, file_path: Path, keyword: str | None = None) -> list[Schema]:
        """Extract interfaces, Zod schemas, and form fields from a TypeScript file."""
        try:
            content = file_path.read_text(errors="replace")
        except OSError:
            return []

        schemas: list[Schema] = []
        rel_path = str(file_path)

        # Determine layer
        is_admin = bool(_ADMIN_PATH_RE.search(rel_path))
        is_form = bool(_FORM_MARKERS.search(content))
        is_api = self._is_api_handler(content, rel_path)

        # 1. Extract TypeScript interfaces/types
        for match in _INTERFACE_RE.finditer(content):
            iface_name = match.group(1)

            if keyword and keyword.lower() not in iface_name.lower():
                if keyword.lower() not in Path(file_path).stem.lower():
                    continue

            iface_start = match.start()
            start_line = content[:iface_start].count("\n") + 1

            # Find closing brace
            body = self._find_block(content, match.end() - 1)
            if body is None:
                continue

            fields = self._extract_ts_fields(body, start_line)
            if not fields:
                continue

            layer = "admin_views" if is_admin else "api_handlers"
            schemas.append(Schema(
                file=str(file_path),
                layer=layer,
                schema_type="ts_interface",
                name=iface_name,
                fields=fields,
            ))

        # 2. Extract Zod schemas
        for match in _ZOD_SCHEMA_RE.finditer(content):
            schema_name = match.group(1)

            if keyword and keyword.lower() not in schema_name.lower():
                if keyword.lower() not in Path(file_path).stem.lower():
                    continue

            schema_start = match.start()
            start_line = content[:schema_start].count("\n") + 1

            body = self._find_block(content, match.end() - 1)
            if body is None:
                continue

            fields = self._extract_zod_fields(body, start_line)
            if not fields:
                continue

            layer = "frontend_forms" if is_form else "api_handlers"
            schemas.append(Schema(
                file=str(file_path),
                layer=layer,
                schema_type="zod_schema",
                name=schema_name,
                fields=fields,
            ))

        # 3. Extract form fields (register/name attributes)
        if is_form:
            form_fields = self._extract_form_fields(content, file_path)
            if form_fields:
                layer = "admin_views" if is_admin else "frontend_forms"
                schemas.append(Schema(
                    file=str(file_path),
                    layer=layer,
                    schema_type="form_fields",
                    name=f"form@{Path(file_path).stem}",
                    fields=form_fields,
                ))

        return schemas

    def _find_block(self, content: str, open_brace_pos: int) -> str | None:
        """Find the content between matching braces starting at open_brace_pos."""
        if open_brace_pos >= len(content) or content[open_brace_pos] != "{":
            return None
        depth = 0
        for i in range(open_brace_pos, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    return content[open_brace_pos:i + 1]
        return None

    def _extract_ts_fields(self, body: str, base_line: int) -> list[Field]:
        """Parse fields from a TypeScript interface/type body."""
        fields: list[Field] = []
        for match in _TS_FIELD_RE.finditer(body):
            fname = match.group(1)
            optional = match.group(2) == "?"
            ftype = match.group(3).rstrip(";").strip()
            line = base_line + body[:match.start()].count("\n")

            fields.append(Field(
                name=fname,
                type=ftype,
                line=line,
                required=not optional,
                tags={},
            ))
        return fields

    def _extract_zod_fields(self, body: str, base_line: int) -> list[Field]:
        """Parse fields from a Zod schema body."""
        fields: list[Field] = []
        for match in _ZOD_FIELD_RE.finditer(body):
            fname = match.group(1)
            zod_chain = match.group(2)
            line = base_line + body[:match.start()].count("\n")

            # Determine type from Zod chain
            type_match = re.search(r"z\.(\w+)", zod_chain)
            ftype = type_match.group(1) if type_match else None

            # Determine required/optional
            required = ".optional()" not in zod_chain and ".nullable()" not in zod_chain

            # Collect validators
            tags: dict[str, str] = {}
            validators = re.findall(r"\.(\w+)\(([^)]*)\)", zod_chain)
            for vname, vargs in validators:
                if vname in ("min", "max", "email", "url", "regex", "length"):
                    tags[f"validator_{vname}"] = vargs or "true"

            fields.append(Field(
                name=fname,
                type=ftype,
                line=line,
                required=required,
                tags=tags,
            ))
        return fields

    def _extract_form_fields(self, content: str, file_path: Path) -> list[Field]:
        """Extract field names from form registrations and name attributes."""
        seen: dict[str, int] = {}  # name -> first line number
        fields: list[Field] = []

        for match in _REGISTER_RE.finditer(content):
            fname = match.group(1)
            line = content[:match.start()].count("\n") + 1
            if fname not in seen:
                seen[fname] = line

        for match in _NAME_ATTR_RE.finditer(content):
            fname = match.group(1)
            line = content[:match.start()].count("\n") + 1
            if fname not in seen:
                seen[fname] = line

        for fname, line in seen.items():
            fields.append(Field(
                name=fname,
                type=None,
                line=line,
                required=None,
                tags={"source": "form_field"},
            ))

        return fields

    def scan_patterns(self, repo_path: Path, scan_path: str | None = None) -> dict:
        """Scan TypeScript files for naming convention inconsistencies."""
        root = repo_path / scan_path if scan_path else repo_path

        naming_styles: Counter[str] = Counter()
        total_fields = 0
        zod_count = 0
        interface_count = 0

        for pattern in ("*.ts", "*.tsx"):
            for ts_file in root.rglob(pattern):
                if "node_modules" in ts_file.parts or ".next" in ts_file.parts:
                    continue
                try:
                    content = ts_file.read_text(errors="replace")
                except OSError:
                    continue

                # Count interfaces and Zod schemas
                interface_count += len(_INTERFACE_RE.findall(content))
                zod_count += len(_ZOD_SCHEMA_RE.findall(content))

                # Classify field naming
                for match in _TS_FIELD_RE.finditer(content):
                    fname = match.group(1)
                    naming_styles[_classify_naming(fname)] += 1
                    total_fields += 1

        return {
            "extractor": self.name(),
            "total_fields": total_fields,
            "naming_styles": dict(naming_styles),
            "interface_count": interface_count,
            "zod_schema_count": zod_count,
        }


def _classify_naming(name: str) -> str:
    """Classify a name as snake_case, camelCase, PascalCase, or other."""
    if "_" in name:
        return "snake_case"
    if name[0:1].isupper():
        return "PascalCase"
    if name[0:1].islower() and any(c.isupper() for c in name[1:]):
        return "camelCase"
    if name.islower():
        return "lowercase"
    return "other"

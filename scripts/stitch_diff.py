#!/usr/bin/env python3
"""
Cross-boundary schema diffing for stitch-audit.

Entirely stack-agnostic — works on standardized Schema/Field JSON from
any extractor. The diff logic doesn't know or care whether the fields
came from Go structs or Python dataclasses.

Boundaries diffed (by layer, not by language):
    frontend_forms  ->  api_handlers
    api_handlers    ->  database_ops
    database_ops    ->  admin_views
    frontend_forms  ->  admin_views   (completeness check)

Field name resolution per layer:
    frontend_forms: field.name (form field name / Zod key)
    api_handlers:   field.tags["json_tag"] -> fallback field.name
    database_ops:   field.tags["bson_tag"] -> fallback field.name
    admin_views:    field.name (TS interface field)

Severity levels:
    BREAK  — Data lost, data hidden, type mismatch
    WARN   — Naming inconsistency, validation mismatch, required/optional divergence
    INFO   — Dead fields, API-only fields, convention drift

Usage:
    python3 stitch_diff.py <extraction.json> [-o findings.json] [--format json|text]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extractors.base import Field, Schema, schemas_from_json


@dataclass
class Finding:
    """A single diff finding between two layers."""

    severity: str  # BREAK | WARN | INFO
    category: str  # data_lost | data_hidden | type_mismatch | naming | validation | dead_field
    message: str
    source_layer: str
    target_layer: str
    source_file: str | None = None
    target_file: str | None = None
    source_field: str | None = None
    target_field: str | None = None
    source_line: int | None = None
    target_line: int | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Ordered boundaries to diff
BOUNDARIES = [
    ("frontend_forms", "api_handlers"),
    ("api_handlers", "database_ops"),
    ("database_ops", "admin_views"),
    ("frontend_forms", "admin_views"),
]


def _canonical(name: str) -> str:
    """Canonicalize a field name for comparison: lowercase, strip _/-."""
    return re.sub(r"[_\-]", "", name.lower())


def _resolve_name(f: Field, layer: str) -> str:
    """Resolve the effective field name for a given layer."""
    if layer == "api_handlers":
        return f.tags.get("json_tag") or f.name
    if layer == "database_ops":
        return f.tags.get("bson_tag") or f.name
    # frontend_forms, admin_views: use field.name directly
    return f.name


def _collect_fields_by_layer(schemas: list[Schema]) -> dict[str, list[tuple[Field, Schema]]]:
    """Group fields by layer, pairing each field with its parent schema."""
    by_layer: dict[str, list[tuple[Field, Schema]]] = {}
    for schema in schemas:
        layer = schema.layer
        if layer not in by_layer:
            by_layer[layer] = []
        for f in schema.fields:
            by_layer[layer].append((f, schema))
    return by_layer


def diff_boundary(
    source_layer: str,
    target_layer: str,
    source_fields: list[tuple[Field, Schema]],
    target_fields: list[tuple[Field, Schema]],
) -> list[Finding]:
    """Diff fields across a single boundary between two layers."""
    findings: list[Finding] = []

    # Build canonical name maps
    source_map: dict[str, tuple[Field, Schema]] = {}
    for f, s in source_fields:
        name = _resolve_name(f, source_layer)
        canon = _canonical(name)
        source_map[canon] = (f, s)

    target_map: dict[str, tuple[Field, Schema]] = {}
    for f, s in target_fields:
        name = _resolve_name(f, target_layer)
        canon = _canonical(name)
        target_map[canon] = (f, s)

    # Fields in source but not in target -> data lost or data hidden
    for canon, (sf, ss) in source_map.items():
        src_name = _resolve_name(sf, source_layer)
        if canon not in target_map:
            # Determine severity based on direction
            if source_layer == "frontend_forms" and target_layer in ("api_handlers", "database_ops"):
                severity = "BREAK"
                category = "data_lost"
                msg = f"Field '{src_name}' sent from {source_layer} but not received in {target_layer}"
            elif source_layer == "database_ops" and target_layer == "admin_views":
                severity = "BREAK"
                category = "data_hidden"
                msg = f"Field '{src_name}' stored in DB but not displayed in {target_layer}"
            else:
                severity = "INFO"
                category = "dead_field"
                msg = f"Field '{src_name}' in {source_layer} has no counterpart in {target_layer}"

            findings.append(Finding(
                severity=severity,
                category=category,
                message=msg,
                source_layer=source_layer,
                target_layer=target_layer,
                source_file=ss.file,
                source_field=src_name,
                source_line=sf.line,
            ))
            continue

        # Field exists in both — check for mismatches
        tf, ts = target_map[canon]
        tgt_name = _resolve_name(tf, target_layer)

        # Naming inconsistency (canonical matches but actual names differ)
        if src_name != tgt_name:
            findings.append(Finding(
                severity="WARN",
                category="naming",
                message=f"Naming mismatch: '{src_name}' ({source_layer}) vs '{tgt_name}' ({target_layer})",
                source_layer=source_layer,
                target_layer=target_layer,
                source_file=ss.file,
                target_file=ts.file,
                source_field=src_name,
                target_field=tgt_name,
                source_line=sf.line,
                target_line=tf.line,
            ))

        # Type mismatch
        if sf.type and tf.type and not _types_compatible(sf.type, tf.type):
            findings.append(Finding(
                severity="BREAK",
                category="type_mismatch",
                message=f"Type mismatch for '{src_name}': {sf.type} ({source_layer}) vs {tf.type} ({target_layer})",
                source_layer=source_layer,
                target_layer=target_layer,
                source_file=ss.file,
                target_file=ts.file,
                source_field=src_name,
                target_field=tgt_name,
                source_line=sf.line,
                target_line=tf.line,
                details={"source_type": sf.type, "target_type": tf.type},
            ))

        # Required/optional divergence
        if sf.required is not None and tf.required is not None:
            if sf.required != tf.required:
                findings.append(Finding(
                    severity="WARN",
                    category="validation",
                    message=(
                        f"Required/optional mismatch for '{src_name}': "
                        f"{'required' if sf.required else 'optional'} ({source_layer}) vs "
                        f"{'required' if tf.required else 'optional'} ({target_layer})"
                    ),
                    source_layer=source_layer,
                    target_layer=target_layer,
                    source_file=ss.file,
                    target_file=ts.file,
                    source_field=src_name,
                    target_field=tgt_name,
                    source_line=sf.line,
                    target_line=tf.line,
                ))

    # Fields in target but not in source -> server-set or dead
    for canon, (tf, ts) in target_map.items():
        tgt_name = _resolve_name(tf, target_layer)
        if canon not in source_map:
            findings.append(Finding(
                severity="INFO",
                category="dead_field",
                message=f"Field '{tgt_name}' in {target_layer} has no source in {source_layer} (possibly server-set)",
                source_layer=source_layer,
                target_layer=target_layer,
                target_file=ts.file,
                target_field=tgt_name,
                target_line=tf.line,
            ))

    return findings


# Cross-language type compatibility mappings
_TYPE_ALIASES: dict[str, set[str]] = {
    "string": {"string", "str", "text", "varchar"},
    "number": {"number", "int", "int32", "int64", "float", "float32", "float64", "double", "decimal"},
    "boolean": {"boolean", "bool"},
    "array": {"array", "[]", "list", "slice"},
    "object": {"object", "map", "dict", "bson.m", "interface{}"},
    "date": {"date", "time.time", "datetime", "timestamp"},
}


def _types_compatible(t1: str, t2: str) -> bool:
    """Check if two type strings are semantically compatible across languages."""
    c1 = t1.lower().strip("*&")
    c2 = t2.lower().strip("*&")

    if c1 == c2:
        return True

    # Check array types: []string vs string[]
    if c1.startswith("[]") or c1.endswith("[]") or c2.startswith("[]") or c2.endswith("[]"):
        base1 = c1.strip("[]")
        base2 = c2.strip("[]")
        is_arr1 = "[]" in c1
        is_arr2 = "[]" in c2
        if is_arr1 != is_arr2:
            return False
        return _types_compatible(base1, base2) if base1 and base2 else True

    # Check aliases
    for _group, aliases in _TYPE_ALIASES.items():
        if c1 in aliases and c2 in aliases:
            return True

    return False


def diff_all(schemas: list[Schema]) -> list[Finding]:
    """Run diff across all standard boundaries."""
    by_layer = _collect_fields_by_layer(schemas)
    findings: list[Finding] = []

    for src_layer, tgt_layer in BOUNDARIES:
        src_fields = by_layer.get(src_layer, [])
        tgt_fields = by_layer.get(tgt_layer, [])
        if not src_fields or not tgt_fields:
            continue
        findings.extend(diff_boundary(src_layer, tgt_layer, src_fields, tgt_fields))

    return findings


def format_text(findings: list[Finding]) -> str:
    """Format findings as human-readable text."""
    if not findings:
        return "No findings."

    lines: list[str] = []
    by_severity: dict[str, list[Finding]] = {}
    for f in findings:
        by_severity.setdefault(f.severity, []).append(f)

    for severity in ("BREAK", "WARN", "INFO"):
        group = by_severity.get(severity, [])
        if not group:
            continue
        lines.append(f"\n{'='*60}")
        lines.append(f" {severity} ({len(group)} findings)")
        lines.append(f"{'='*60}")
        for f in group:
            lines.append(f"\n  [{f.category}] {f.message}")
            if f.source_file:
                loc = f"    source: {f.source_file}"
                if f.source_line:
                    loc += f":{f.source_line}"
                lines.append(loc)
            if f.target_file:
                loc = f"    target: {f.target_file}"
                if f.target_line:
                    loc += f":{f.target_line}"
                lines.append(loc)

    summary = {s: len(fs) for s, fs in by_severity.items()}
    lines.append(f"\nSummary: {summary}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-boundary schema diffing for stitch-audit"
    )
    parser.add_argument("extraction_json", help="Path to extraction JSON file")
    parser.add_argument("-o", "--output", help="Write output to file")
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args()

    extraction_path = Path(args.extraction_json)
    if not extraction_path.exists():
        print(f"Error: {extraction_path} not found", file=sys.stderr)
        sys.exit(1)

    data = extraction_path.read_text()
    schemas = schemas_from_json(data)
    print(f"Loaded {len(schemas)} schemas", file=sys.stderr)

    findings = diff_all(schemas)
    print(
        f"Found {len(findings)} findings "
        f"({sum(1 for f in findings if f.severity == 'BREAK')} BREAK, "
        f"{sum(1 for f in findings if f.severity == 'WARN')} WARN, "
        f"{sum(1 for f in findings if f.severity == 'INFO')} INFO)",
        file=sys.stderr,
    )

    if args.format == "json":
        output = json.dumps([f.to_dict() for f in findings], indent=2)
    else:
        output = format_text(findings)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"OK: findings written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()

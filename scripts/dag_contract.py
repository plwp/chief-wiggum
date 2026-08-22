#!/usr/bin/env python3
"""Validate and project versioned dynamic-DAG contract records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.dag import (  # noqa: E402
    canonical_json_bytes,
    dependency_block_to_intent_graph,
    project_legacy_waves,
    validate_canonical_bytes,
    validate_record,
    validate_snapshot,
)

MAX_INPUT_BYTES = 16 * 1024 * 1024


def _ints(value: str | None) -> list[int]:
    return [] if not value else [int(item) for item in value.replace(",", " ").split()]


def _validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        print(json.dumps({"ok": False, "input_error": f"record exceeds {MAX_INPUT_BYTES} byte limit"}))
        return 1
    if args.canonical and (encoding_errors := validate_canonical_bytes(raw)):
        print(json.dumps({"ok": False, "errors": [error.to_dict() for error in encoding_errors]}, indent=2))
        return 2
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "input_error": str(exc)}, indent=2))
        return 1
    record_type = args.record_type or (record.get("record_type") if isinstance(record, dict) else None)
    errors = validate_snapshot(record) if record_type == "graph_snapshot" else validate_record(record, record_type)
    if errors:
        print(json.dumps({"ok": False, "errors": [error.to_dict() for error in errors]}, indent=2))
        return 2
    print(json.dumps({"ok": True, "record_type": record_type, "schema_version": record["schema_version"]}))
    return 0


def _canonicalize(args: argparse.Namespace) -> int:
    try:
        record = json.loads(Path(args.path).read_bytes())
        sys.stdout.buffer.write(canonical_json_bytes(record))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _project(args: argparse.Namespace) -> int:
    description = Path(args.description).read_text()
    intent = dependency_block_to_intent_graph(
        description,
        graph_id=args.graph_id,
        issues=_ints(args.issues),
        source_ref=args.source_ref,
    )
    result = project_legacy_waves(intent, closed=_ints(args.closed), gated=_ints(args.gated))
    if result.plan is not None:
        print(json.dumps(result.plan, indent=2))
    else:
        print(f"Error: {result.error}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dynamic DAG data-contract tools")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a DAG record")
    validate.add_argument("path")
    validate.add_argument("--record-type")
    validate.add_argument("--canonical", action="store_true", help="also require canonical bytes")
    validate.set_defaults(func=_validate)

    canonicalize = sub.add_parser("canonicalize", help="emit canonical DAG JSON bytes")
    canonicalize.add_argument("path")
    canonicalize.set_defaults(func=_canonicalize)

    project = sub.add_parser("project-waves", help="import a dependency block and emit the legacy six-field wave plan")
    project.add_argument("description")
    project.add_argument("--graph-id", required=True)
    project.add_argument("--issues", required=True)
    project.add_argument("--source-ref", required=True)
    project.add_argument("--closed")
    project.add_argument("--gated")
    project.set_defaults(func=_project)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

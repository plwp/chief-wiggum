#!/usr/bin/env python3
"""CLI for the traceability matrix parser/updater (P2-13).

Parses, audits, and updates the ``traceability.md`` table /architect generates.
``/implement`` Step 13 flips rows to covered; ``/close-epic`` Step 2 audits.

Examples:
    # Audit coverage as JSON
    python3 scripts/traceability.py audit docs/epics/x/traceability.md

    # Mark a ticket's rows covered (in place)
    python3 scripts/traceability.py update docs/epics/x/traceability.md \
      --ticket 42 --status covered --ac "GET /health"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import traceability as tr  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Traceability matrix parser/updater")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Parse and summarize coverage")
    p_audit.add_argument("path")

    p_render = sub.add_parser("render", help="Re-render the parsed table")
    p_render.add_argument("path")

    p_update = sub.add_parser("update", help="Set status on matching rows, in place")
    p_update.add_argument("path")
    p_update.add_argument("--ticket", type=int, required=True)
    p_update.add_argument("--status", required=True, choices=tr.STATUSES)
    p_update.add_argument("--ac", help="Narrow to rows whose AC contains this text")
    p_update.add_argument("--test", help="Narrow to rows whose test refs contain this text")

    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    matrix = tr.parse_matrix(path.read_text())

    if args.command == "audit":
        print(json.dumps(tr.audit(matrix), indent=2))
    elif args.command == "render":
        print(tr.render_markdown(matrix))
    elif args.command == "update":
        try:
            n = tr.update_status(
                matrix, ticket=args.ticket, status=args.status,
                ac_contains=args.ac, test_contains=args.test,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if n == 0:
            print(f"Warning: no rows matched ticket #{args.ticket}", file=sys.stderr)
        # Rewrite only the table span, preserving surrounding prose.
        path.write_text(tr.replace_table(path.read_text(), matrix))
        print(f"OK: updated {n} row(s) to {args.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

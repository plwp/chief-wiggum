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

    # Preview an update without writing (chief-wiggum#342)
    python3 scripts/traceability.py update docs/epics/x/traceability.md \
      --ticket 42 --status covered --dry-run

Exit codes for ``update``: 0 on a successful write (or a dry-run preview of
one), 1 on a usage/parse error (bad status, file not found), 2 when ZERO
rows matched the ticket — the file is never written in that case
(chief-wiggum#342), matching this repo's fail-closed discipline: "0 matched
rows" must never be treated as "helpfully normalize whatever I did find".
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
    p_update.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing the file (chief-wiggum#342)",
    )

    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    if args.command == "audit":
        matrix = tr.parse_matrix(path.read_text())
        print(json.dumps(tr.audit(matrix), indent=2))
        return 0
    if args.command == "render":
        matrix = tr.parse_matrix(path.read_text())
        print(tr.render_markdown(matrix))
        return 0

    # command == "update" — routed through tr.update_file (chief-wiggum#342),
    # NOT the single-table parse_matrix/update_status/replace_table combo
    # audit/render still use above: update_file is the only path that (a)
    # never re-renders a non-canonical table and (b) never writes the file at
    # all when zero rows matched, across every table in the document.
    try:
        new_text, n, warnings = tr.update_file(
            path.read_text(), ticket=args.ticket, status=args.status,
            ac_contains=args.ac, test_contains=args.test,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    if n == 0:
        prefix = "DRY RUN: " if args.dry_run else ""
        print(
            f"{prefix}Error: no rows matched ticket #{args.ticket} — nothing written",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print(f"DRY RUN: would update {n} row(s) to {args.status} (no file written)")
        return 0

    # new_text is guaranteed non-None here — update_file only returns None
    # alongside n == 0, handled above.
    path.write_text(new_text)
    print(f"OK: updated {n} row(s) to {args.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

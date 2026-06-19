#!/usr/bin/env python3
"""CLI for epic artifact discovery and context loading (P0-5).

Emits a structured inventory of what epic context exists for a ticket/epic and
which gates apply (formal models, UI spec, transition map, design contract,
unresolved markers). `/implement` Step 1 and `/implement-wave` Step 1 consume it.

Examples:
    # Inventory for an epic in a resolved repo path
    python3 scripts/epic_inventory.py /path/to/repo --epic-slug order-lifecycle --json

    # Markdown summary for a user report
    python3 scripts/epic_inventory.py /path/to/repo --epic-slug order-lifecycle --markdown --issue 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import artifacts  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Epic artifact inventory")
    parser.add_argument("repo_path", help="Local path to the target repo")
    parser.add_argument("--epic-slug", help="Epic slug (docs/epics/<slug>)")
    parser.add_argument("--issue", type=int, help="Ticket number for context")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Emit JSON (default)")
    out.add_argument("--markdown", action="store_true", help="Emit markdown summary")
    args = parser.parse_args(argv)

    try:
        inv = artifacts.build_inventory(
            args.repo_path, epic_slug=args.epic_slug, issue=args.issue
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.markdown:
        print(inv.render_markdown())
    else:
        print(inv.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())

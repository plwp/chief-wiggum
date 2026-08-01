#!/usr/bin/env python3
"""CLI to build `$TICKET_TMP/ticket.json` from `gh issue view --json` output (#83).

The upstream writer half of the #83 fix: before this existed, `ticket.json`
carried title/body/acceptance_criteria only, and comments (needed to detect
issue-author/maintainer amendments to the acceptance criteria) never reached
`review.TicketContext.from_dict` at all. `comments` is always emitted as an
array, even when empty (`[]`), never an absent key (CTR-fh-002, IT-fh-10).

Example (see `/implement` Step 2):
    gh issue view 83 --repo acme/app --json title,body,author,comments \
      > "$TICKET_TMP/issue-raw.json"
    python3 scripts/write_ticket_context.py \
      --issue-json "$TICKET_TMP/issue-raw.json" --number 83 \
      --acceptance-criteria "Fold comments into the review prompt" \
      --output "$TICKET_TMP/ticket.json"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import review  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build ticket.json from `gh issue view --json` output"
    )
    parser.add_argument(
        "--issue-json", required=True, help="Path to `gh issue view --json ...` output"
    )
    parser.add_argument("--number", type=int, default=None, help="Issue number")
    parser.add_argument(
        "--acceptance-criteria",
        action="append",
        default=[],
        metavar="LINE",
        help="One acceptance-criterion line (repeatable)",
    )
    parser.add_argument("--output", required=True, help="Where to write ticket.json")
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.issue_json).read_text())
    ticket = review.build_ticket_context_json(
        raw, number=args.number, acceptance_criteria=args.acceptance_criteria
    )
    text = json.dumps(ticket, indent=2) + "\n"
    Path(args.output).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

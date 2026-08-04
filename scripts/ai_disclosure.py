#!/usr/bin/env python3
"""CLI wrapper for `chief_wiggum.ai_disclosure` (chief-wiggum#317).

Workflow adapters that assemble a PR/issue body or a commit message as a
plain file (rather than through a Python call already wired to the library,
like `draft_pr.py` -> `chief_wiggum.shipping.build_pr_body`) run this so the
AI-authorship disclosure is mechanical, not a prose instruction the agent
might forget.

Examples:
    # Append the markdown disclosure line to an issue body file, in place
    python3 scripts/ai_disclosure.py body --file "$CW_TMP/issue-body.md"

    # Append the commit trailer to a drafted commit message, in place
    python3 scripts/ai_disclosure.py commit-trailer --file "$CW_TMP/commit-msg.txt"

    # Or via stdin/stdout
    cat body.md | python3 scripts/ai_disclosure.py body > body-final.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import ai_disclosure  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)

    p_body = sub.add_parser("body", help="Append the PR/issue disclosure line")
    p_body.add_argument("--file", help="Read/write this file in place; default stdin/stdout")

    p_commit = sub.add_parser("commit-trailer", help="Append the commit-message trailer")
    p_commit.add_argument("--file", help="Read/write this file in place; default stdin/stdout")

    args = parser.parse_args(argv)

    fn = ai_disclosure.ensure_commit_trailer if args.kind == "commit-trailer" else ai_disclosure.ensure_disclosure
    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    out = fn(text)

    if args.file:
        Path(args.file).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CLI to draft a PR body from upstream manifests (P1-11).

Assembles a PR body with a themed Mermaid diagram, verification evidence,
optional model-conformance and UX sections, validates the required sections, and
optionally prints the `gh pr create` command. Used by /ship and /implement.

Example:
    python3 scripts/draft_pr.py --issue 42 --summary "Add X" \
      --change "Add module" --change "Wire CLI" \
      --verification "$TICKET_TMP/reviews/../verification.json" \
      --review "$TICKET_TMP/reviews/review-manifest.json" \
      --mermaid-file diagram.mmd --out "$TICKET_TMP/pr-body.md"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import shipping  # noqa: E402


def _load_json(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft a PR body from manifests")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--title", help="Issue/PR title for the suggested PR title")
    parser.add_argument("--summary", default="")
    parser.add_argument("--change", action="append", default=[], help="A bullet for the Changes section")
    parser.add_argument("--mermaid-file", help="File with a mermaid diagram body")
    parser.add_argument("--mermaid-sequence", action="store_true")
    parser.add_argument("--verification", help="verification report JSON")
    parser.add_argument("--review", help="review-manifest.json")
    parser.add_argument("--ux", help="UX gate manifest JSON")
    parser.add_argument("--model-conformance", help="Markdown text or file for the Model Conformance section")
    parser.add_argument("--base")
    parser.add_argument("--out", help="Write the PR body to this file (else stdout)")
    parser.add_argument("--print-command", action="store_true", help="Also print the gh pr create command")
    args = parser.parse_args(argv)

    mermaid = None
    if args.mermaid_file and Path(args.mermaid_file).exists():
        mermaid = Path(args.mermaid_file).read_text()

    conformance = args.model_conformance
    if conformance and Path(conformance).exists():
        conformance = Path(conformance).read_text()

    body = shipping.build_pr_body(
        issue=args.issue,
        summary=args.summary,
        changes=args.change or None,
        mermaid=mermaid,
        mermaid_sequence=args.mermaid_sequence,
        verification=_load_json(args.verification),
        review=_load_json(args.review),
        ux=_load_json(args.ux),
        model_conformance=conformance,
    )

    missing = shipping.validate_sections(body)
    if missing:
        print(f"Error: PR body missing required sections: {', '.join(missing)}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(body)
        print(f"OK: PR body written to {args.out}")
    else:
        print(body)

    if args.print_command:
        title = shipping.suggest_title(args.title, issue=args.issue)
        body_file = args.out or "<pr-body-file>"
        print("\n# Suggested command:")
        print(" ".join(shipping.gh_pr_create_command(title, body_file, base=args.base)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

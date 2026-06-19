#!/usr/bin/env python3
"""CLI for the project verification runner (P1-9).

Detects how to test/lint/build/smoke a repo and emits structured evidence
(command, cwd, exit code, duration, log tail) as JSON or markdown — so /ship and
/implement produce machine-readable verification evidence instead of prose.

Examples:
    # Run the test + lint profiles and emit markdown evidence
    python3 scripts/run_verification.py --repo . --profile test,lint --markdown

    # Show the planned commands without running anything
    python3 scripts/run_verification.py --repo . --profile test,lint,build,smoke --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import verification  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect and run project verification")
    parser.add_argument("--repo", default=".", help="Repo path (default: cwd)")
    parser.add_argument(
        "--profile",
        default="test",
        help="Comma-separated profiles: test,lint,build,smoke",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands, run nothing")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Emit JSON evidence (default)")
    out.add_argument("--markdown", action="store_true", help="Emit markdown evidence")
    args = parser.parse_args(argv)

    profiles = [p.strip() for p in args.profile.split(",") if p.strip()]
    unknown = [p for p in profiles if p not in verification.PROFILES]
    if unknown:
        print(f"Error: unknown profile(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    report = verification.verify(args.repo, profiles, dry_run=args.dry_run)

    if args.markdown:
        print(report.render_markdown())
    else:
        print(json.dumps(report.to_dict(), indent=2))

    # Dry-run is informational; a real run fails if any executed step failed.
    if args.dry_run:
        return 0
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

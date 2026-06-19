#!/usr/bin/env python3
"""CLI for the shared workflow context resolver.

Replaces the repeated ``CW_HOME``/``CW_TMP``/``TARGET_REPO``/``DEFAULT_BRANCH``/
``EPIC_SLUG`` shell setup in the command prompts with one tested call.

Examples:
    # repo context (shell exports for `eval`)
    eval "$(python3 scripts/workflow_context.py acme/app --shell)"

    # ticket context as JSON
    python3 scripts/workflow_context.py acme/app#42 --json

    # epic context, offline (no clone / branch lookup)
    python3 scripts/workflow_context.py acme/app --epic "Epic: Name" --no-resolve --json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure sibling modules (env, repo) and the package are importable when this
# wrapper is run directly as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import context  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve shared workflow context")
    parser.add_argument(
        "target",
        nargs="?",
        help="owner/repo or owner/repo#42 (omit for home-only context)",
    )
    parser.add_argument("--epic", help="Epic / milestone name")
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="Do not clone/resolve the repo or look up the default branch",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Emit JSON (default)")
    out.add_argument("--shell", action="store_true", help="Emit shell exports")
    args = parser.parse_args(argv)

    try:
        ctx = context.resolve(
            args.target,
            epic=args.epic,
            resolve_repo_path=not args.no_resolve,
            detect_branch=not args.no_resolve,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.shell:
        print(ctx.shell_exports())
    else:
        print(ctx.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())

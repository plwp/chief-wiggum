#!/usr/bin/env python3
"""CLI for worktree and branch safety checks (P1-8).

Gives the command prompts an executable replacement for prose-only worktree
warnings. Designed to be dropped into a sub-agent prompt so it aborts before
touching the main checkout.

Exit codes: 0 = check passed, 1 = safety violation / git error, 2 = usage.

Examples:
    # Abort unless cwd is a worktree distinct from the main checkout
    python3 scripts/git_safety.py assert-worktree --main "$TARGET_REPO"

    # Abort unless the main checkout is on the default branch with a clean tree
    # (catches a worker that leaked a branch into main — isolation leak)
    python3 scripts/git_safety.py assert-main-pristine --main "$TARGET_REPO" --default-branch main

    # Validate a branch name
    python3 scripts/git_safety.py check-branch feat/my-thing

    # Is the working tree clean?
    python3 scripts/git_safety.py is-clean
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import gitops  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worktree/branch safety checks")
    sub = parser.add_subparsers(dest="command", required=True)

    p_wt = sub.add_parser("assert-worktree", help="Assert cwd is a worktree, not the main checkout")
    p_wt.add_argument("--main", required=True, help="Path to the main checkout")
    p_wt.add_argument("--worktree", default=".", help="Worktree path (default: cwd)")

    p_pristine = sub.add_parser(
        "assert-main-pristine",
        help="Assert the main checkout is on the default branch with a clean tree "
        "(catches a worker that leaked a branch into the main checkout)",
    )
    p_pristine.add_argument("--main", required=True, help="Path to the main checkout")
    p_pristine.add_argument("--default-branch", required=True, help="Expected default branch")

    p_branch = sub.add_parser("check-branch", help="Validate a branch name")
    p_branch.add_argument("name")

    p_clean = sub.add_parser("is-clean", help="Exit 0 if the working tree is clean")
    p_clean.add_argument("--repo", default=".")

    p_ff = sub.add_parser("can-fast-forward", help="Exit 0 if base can fast-forward to branch")
    p_ff.add_argument("--repo", default=".")
    p_ff.add_argument("base")
    p_ff.add_argument("branch")

    args = parser.parse_args(argv)

    try:
        if args.command == "assert-worktree":
            root = gitops.assert_worktree(args.worktree, args.main)
            print(f"OK: worktree {root} is isolated from main")
        elif args.command == "assert-main-pristine":
            gitops.assert_main_pristine(args.main, args.default_branch)
            print(f"OK: main checkout is pristine (on {args.default_branch}, clean tree)")
        elif args.command == "check-branch":
            gitops.assert_branch_name(args.name)
            print(f"OK: {args.name} is a valid branch name")
        elif args.command == "is-clean":
            if not gitops.is_clean(args.repo):
                print("Working tree is not clean", file=sys.stderr)
                return 1
            print("OK: working tree is clean")
        elif args.command == "can-fast-forward":
            if not gitops.can_fast_forward(args.repo, args.base, args.branch):
                print(f"{args.base} cannot fast-forward to {args.branch}", file=sys.stderr)
                return 1
            print(f"OK: {args.base} can fast-forward to {args.branch}")
    except gitops.GitSafetyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

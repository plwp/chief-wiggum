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

    # Sweep merged-ticket worktrees after a wave promotes (#329) — never
    # touches the main worktree, a parked (unmerged) branch, or a branch
    # named with --keep.
    python3 scripts/git_safety.py gc-worktrees --repo "$TARGET_REPO" \\
      --default-branch main --keep feat/47-parked-ticket
"""

from __future__ import annotations

import argparse
import json
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

    p_list_wt = sub.add_parser("list-worktrees", help="List worktrees as JSON (#329)")
    p_list_wt.add_argument("--repo", required=True)

    p_gc_wt = sub.add_parser(
        "gc-worktrees",
        help="Remove worktrees whose branch is merged into --default-branch (#329); "
        "never touches the main worktree, a detached HEAD, an unmerged (parked) "
        "branch, or a branch passed via --keep",
    )
    p_gc_wt.add_argument("--repo", required=True, help="Path to the target repo (main checkout)")
    p_gc_wt.add_argument("--default-branch", required=True)
    p_gc_wt.add_argument(
        "--keep", action="append", default=[],
        help="Branch to never remove even if merged (a parked ticket); repeatable",
    )
    p_gc_wt.add_argument(
        "--force", action="store_true",
        help="Pass --force to `git worktree remove` (drop even with uncommitted changes)",
    )
    p_gc_wt.add_argument("--dry-run", action="store_true", help="Report what would be removed, remove nothing")

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
        elif args.command == "list-worktrees":
            print(json.dumps(gitops.list_worktrees(args.repo), indent=2))
        elif args.command == "gc-worktrees":
            removed = gitops.gc_merged_worktrees(
                args.repo,
                args.default_branch,
                keep_branches=args.keep,
                force=args.force,
                dry_run=args.dry_run,
            )
            verb = "would remove" if args.dry_run else "removed"
            if removed:
                for entry in removed:
                    print(f"{verb}: {entry['path']} (branch {entry.get('branch')})")
            else:
                print(f"nothing to {'remove' if args.dry_run else 'do'} — no merged non-main worktrees")
    except gitops.GitSafetyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

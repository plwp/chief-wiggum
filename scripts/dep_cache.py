#!/usr/bin/env python3
"""CLI for shared dependency-cache provisioning (#329).

`/implement-wave` runs multiple workers concurrently, each in its own
worktree. Symlinking a shared node_modules/.venv across them (the
`/implement` single-ticket rule) is unsafe under concurrency — see
`chief_wiggum/dep_provisioning.py`'s module docstring for why. This CLI
detects which ecosystems a worktree needs and prints the env vars that point
each ecosystem's package manager at a SHARED, concurrency-safe cache store
instead — never at the installed tree itself.

Exit codes: 0 = success (including "nothing detected"), 2 = usage.

Examples:
    # JSON plan (default)
    python3 scripts/dep_cache.py plan --worktree "$worktree"

    # Shell-eval form, orchestrator exports before launching a worker
    eval "$(python3 scripts/dep_cache.py plan --worktree "$worktree" --shell)"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import dep_provisioning  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared dependency-cache provisioning (#329)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Detect ecosystems and print shared-cache env vars")
    p_plan.add_argument("--worktree", required=True, help="Worker's worktree root")
    p_plan.add_argument(
        "--cache-root", default=None,
        help="Shared cache root (default: ~/.chief-wiggum/cache/deps)",
    )
    p_plan.add_argument("--shell", action="store_true", help="Emit `export`/`mkdir -p` lines instead of JSON")

    args = parser.parse_args(argv)

    if args.command == "plan":
        result = dep_provisioning.plan(args.worktree, cache_root=args.cache_root)
        if args.shell:
            sys.stdout.write(dep_provisioning.render_shell(result))
        else:
            print(json.dumps(dep_provisioning.to_dict(result), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CLI for the #245 advisory per-repo wave lock (staging/promote phase).

`/implement-wave`'s Step 4d-4g operate directly on the shared target-repo
cache checkout (`~/.chief-wiggum/repos/<owner>/<repo>`). Two concurrent
`/implement-wave` orchestrators against the same repo used to be able to
merge/promote into that ONE checkout at the same time with no signal beyond
whatever guard happened to catch the collision after the fact
(`assert-main-pristine` mid-merge — confirmed live, 2026-08-02, on a
multi-tenant video SaaS).

Acquire before Step 4d (create the staging branch), release after Step 4g
(promote to main). A second concurrent run's `acquire` fails LOUDLY, naming
the session/pid already holding the lock, instead of colliding invisibly.

Exit codes: 0 = success, 1 = contention / no lock held to release, 2 = usage.

Examples:
    python3 scripts/wave_lock.py acquire --repo "$TARGET_REPO" --session "$SESSION_ID" --wave 2
    python3 scripts/wave_lock.py release --repo "$TARGET_REPO" --session "$SESSION_ID"
    python3 scripts/wave_lock.py status  --repo "$TARGET_REPO"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import repo_lock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advisory per-repo wave lock (#245)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_acquire = sub.add_parser(
        "acquire", help="Acquire the wave lock for the staging/promote phase; fails loudly on contention"
    )
    p_acquire.add_argument("--repo", required=True, help="Path to the target-repo cache checkout")
    p_acquire.add_argument("--session", required=True, help="This orchestrator session's unique id")
    p_acquire.add_argument("--wave", default=None, help="Wave number/label, for status/diagnostics")
    p_acquire.add_argument("--pid", type=int, default=None, help="Override pid recorded (default: this process)")

    p_release = sub.add_parser("release", help="Release the wave lock")
    p_release.add_argument("--repo", required=True)
    p_release.add_argument("--session", required=True)
    p_release.add_argument(
        "--force", action="store_true",
        help="Release even if held by a different session (only if you are certain it is dead)",
    )

    p_status = sub.add_parser("status", help="Show the current lock holder, if any (JSON)")
    p_status.add_argument("--repo", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "acquire":
            lock = repo_lock.acquire(args.repo, args.session, wave=args.wave, pid=args.pid)
            print(f"OK: wave lock acquired for {args.repo} (session {lock.session_id})")
        elif args.command == "release":
            released = repo_lock.release(args.repo, args.session, force=args.force)
            if released:
                print(f"OK: wave lock released for {args.repo}")
            else:
                print(f"No wave lock held for {args.repo}")
                return 1
        elif args.command == "status":
            info = repo_lock.status(args.repo)
            if info is None:
                print(json.dumps({"locked": False}))
            else:
                print(json.dumps({"locked": True, **info}, indent=2))
    except repo_lock.WaveLockError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

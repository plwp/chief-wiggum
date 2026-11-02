"""A per-repo file lock around the task service's mirror run (chief-wiggum#506).

The service's mirror is idempotent, but two concurrent runs for the same repo
can both post before either records. ``/implement-wave`` writes from parallel
workers, so each mirror call takes this lock: writers for the same repo run
the mirror one at a time, writers for different repos never wait on each other.

The lock is an ``flock`` on ``<user dir>/tmp/locks/<owner>__<repo>.lock``. The
kernel drops it when the holder exits, so a crashed worker cannot wedge the
repo, and no stale-lock reclaiming is needed.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import time
from collections.abc import Iterator
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 600.0
_POLL_SECONDS = 0.05


class MirrorLockTimeout(RuntimeError):
    """Another writer held the repo's mirror lock for the whole timeout."""


def lock_path(user_dir: Path, owner_repo: str) -> Path:
    safe = re.sub(r"[^\w.-]+", "__", owner_repo)
    return Path(user_dir) / "tmp" / "locks" / f"{safe}.lock"


@contextlib.contextmanager
def mirror_lock(
    user_dir: Path, owner_repo: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[Path]:
    """Hold the repo's mirror lock, or raise MirrorLockTimeout after ``timeout``."""
    path = lock_path(user_dir, owner_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(path, "a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MirrorLockTimeout(
                        f"another writer has been mirroring {owner_repo} for over "
                        f"{timeout:g}s (lock {path}); raise mcp.mirror_lock_timeout_seconds "
                        "or check for a stuck mirror command"
                    ) from None
                time.sleep(_POLL_SECONDS)
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

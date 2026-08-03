"""Advisory per-repo lock for the `/implement-wave` staging/promote phase (#245).

Concurrent `/implement-wave` orchestrators against the SAME target repo share
one cached checkout (`~/.chief-wiggum/repos/<owner>/<repo>`, resolved by
`repo.py`) for the merge/staging/promote phase (Step 4d-4g in
`.claude/commands/implement-wave.md`). Worker isolation (each ticket runs in
its own worktree) already prevents collisions during implementation; nothing
previously prevented two orchestrators from both `cd`-ing into that ONE
shared checkout and merging/promoting at the same time — confirmed live
2026-08-02 (dogeared-coach): a second wave's staging merge hit
`assert-main-pristine` mid-conflict from a first wave's in-flight staging
merge on the same checkout.

This lock is advisory: a sibling `<repo>.wave-lock` JSON file NEXT TO the
cache checkout (never inside the git repo itself — never git-tracked, never
part of any worker's diff). It FAILS LOUDLY on contention rather than
silently sharing state — a second concurrent run gets a `WaveLockError`
naming exactly who holds it (session id, pid, when), instead of colliding
invisibly mid-merge (the #289 doctrine: a wrong/contended state must render
as a visible error, never a silent substitution).

A lock whose owning pid is no longer running is treated as abandoned and is
reclaimed automatically on the next `acquire` — a crashed orchestrator must
not wedge the repo's staging phase forever.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path


class WaveLockError(RuntimeError):
    """Raised when the wave lock is held by another live session."""


def _lock_path_for(repo_path: str | Path) -> Path:
    """The lock file sibling to the cache checkout: ``<repo>.wave-lock`` —
    NEVER inside the checkout itself, so it can never ride in a worker's diff
    and is never mistaken for a tracked file."""
    p = Path(repo_path)
    return p.parent / f"{p.name}.wave-lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check (POSIX ``kill(pid, 0)``). A pid we cannot
    prove dead is treated as alive — reclaiming is a fallback for a
    CONFIRMED-dead holder, never a guess."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    except OSError:
        return False
    return True


@dataclass
class WaveLock:
    path: Path
    session_id: str
    pid: int
    acquired_at: float
    wave: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "host": socket.gethostname(),
            "wave": self.wave,
        }


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def acquire(
    repo_path: str | Path,
    session_id: str,
    *,
    wave: str | None = None,
    pid: int | None = None,
) -> WaveLock:
    """Acquire the advisory wave lock for ``repo_path``'s staging/promote phase.

    Raises ``WaveLockError`` if a DIFFERENT, still-live session holds it —
    the second concurrent run must be able to tell it is contending, and who
    with. A lock held by a confirmed-dead pid (crashed orchestrator) is
    reclaimed rather than left to wedge the repo forever.

    Re-acquiring with the SAME ``session_id`` (idempotent re-entry — e.g.
    retrying a step within one wave run) always succeeds and refreshes the
    lock's timestamp.

    This is an ADVISORY, best-effort lock (a plain read-then-write over a JSON
    file, not an OS-level lock/fcntl) — sufficient for the staging/promote
    phase's actual concurrency profile (a handful of long-lived orchestrator
    sessions, not a tight contended loop), matching the mechanism #245 asked
    for. It narrows the collision window from "the whole staging/promote
    phase" to "a read-then-write on one small file"; it does not close it to
    zero for two truly simultaneous first-acquires.
    """
    lock_path = _lock_path_for(repo_path)
    existing = _read_lock(lock_path)
    if existing is not None and existing.get("session_id") != session_id:
        holder_pid = existing.get("pid")
        if isinstance(holder_pid, int) and _pid_alive(holder_pid):
            raise WaveLockError(
                f"wave lock for {repo_path} is held by session "
                f"{existing.get('session_id')!r} (pid {holder_pid}, "
                f"acquired at {existing.get('acquired_at')}) — this repo's "
                f"cache checkout is mid staging/promote in another "
                f"/implement-wave run. Wait for it to finish, or if you are "
                f"certain that session is dead, remove {lock_path} manually."
            )
        # Holder pid is confirmed dead: fall through and reclaim.

    lock = WaveLock(
        path=lock_path,
        session_id=session_id,
        pid=pid if pid is not None else os.getpid(),
        acquired_at=time.time(),
        wave=wave,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock.to_dict(), indent=2))
    return lock


def release(repo_path: str | Path, session_id: str, *, force: bool = False) -> bool:
    """Release the lock. Returns ``False`` if nothing was locked.

    Refuses to remove a lock held by a DIFFERENT live session unless
    ``force=True`` — a session must never release someone else's active lock
    by accident.
    """
    lock_path = _lock_path_for(repo_path)
    existing = _read_lock(lock_path)
    if existing is None:
        return False
    if existing.get("session_id") != session_id and not force:
        raise WaveLockError(
            f"refusing to release wave lock for {repo_path}: held by session "
            f"{existing.get('session_id')!r}, not {session_id!r} (pass "
            f"force=True only if you are certain that session is gone)"
        )
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return False
    return True


def status(repo_path: str | Path) -> dict | None:
    """Current lock holder info, or ``None`` if unlocked.

    ``live`` reports whether the holder's pid is still running — a dead pid
    means the lock is abandoned and will be reclaimed on the next
    :func:`acquire`, never left permanently held.
    """
    lock_path = _lock_path_for(repo_path)
    existing = _read_lock(lock_path)
    if existing is None:
        return None
    holder_pid = existing.get("pid")
    existing = dict(existing)
    existing["live"] = isinstance(holder_pid, int) and _pid_alive(holder_pid)
    return existing

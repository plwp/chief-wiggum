#!/usr/bin/env python3
"""
Repository management for chief-wiggum.

Resolves owner/repo references to local paths, cloning via `gh` if needed.
Repos are cached in ~/.chief-wiggum/repos/ to avoid re-cloning.

As a module:
    from repo import resolve_repo
    path = resolve_repo("acme/app")  # returns Path to local clone

As a CLI:
    python3 repo.py resolve acme/app        # print local path (clone if needed)
    python3 repo.py list                     # list cached repos
    python3 repo.py clean acme/app           # remove a cached clone
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".chief-wiggum" / "repos"

# chief-wiggum root is two levels up from this script (scripts/ -> root)
CW_HOME = Path(__file__).resolve().parent.parent

# Strict pattern: alphanumeric, hyphens, underscores, dots (GitHub rules)
_OWNER_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# Seconds of NO output before a clone is treated as stalled (chief-wiggum#268).
# Progress, not elapsed wall-clock time, is the right signal for a clone — a
# large repo legitimately runs long past any fixed ceiling (a 776MB/12,500-file
# repo took several minutes against the old fixed 120s limit).
CLONE_INACTIVITY_TIMEOUT = 300


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group, so a stalled
    `git` (and anything it spawned) cannot keep running — and keep writing to
    the cache path — after the caller has been told the clone failed
    (chief-wiggum#268). Mirrors ``consult_ai._kill_group``: a plain
    ``subprocess.run(timeout=)`` only kills the Python-side wait; the child
    keeps running to completion in the background, which is exactly the
    "orphaned git" bug this guards against."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue  # escalate to SIGKILL


def _clone_with_inactivity_timeout(
    cmd: list[str], *, inactivity_timeout: float = CLONE_INACTIVITY_TIMEOUT,
) -> None:
    """Run a clone command with NO fixed wall-clock ceiling (chief-wiggum#268
    AC1): a large repo legitimately takes longer than any fixed timeout.
    Progress, not elapsed time, is the right signal — the process is killed
    only after `inactivity_timeout` seconds pass with ZERO output.

    Runs in its own process group (``start_new_session``) so a stall can be
    reaped in FULL: on timeout, `_kill_group` kills the whole group and waits
    for it to actually exit, guaranteeing no orphaned `git` survives this call
    (AC3) — never a bare ``subprocess.run(timeout=)``, which kills only the
    Python-side wait and lets the child keep running (and writing) in the
    background.

    Raises ``subprocess.TimeoutExpired`` on a genuine stall, or
    ``subprocess.CalledProcessError`` on a clean nonzero exit. In both cases
    the process group is guaranteed dead before this returns.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True, bufsize=1,
    )
    last_activity = time.monotonic()
    lock = threading.Lock()

    def _reader() -> None:
        nonlocal last_activity
        assert proc.stdout is not None
        for _ in iter(proc.stdout.readline, ""):
            with lock:
                last_activity = time.monotonic()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired as exc:
                with lock:
                    idle = time.monotonic() - last_activity
                if idle > inactivity_timeout:
                    _kill_group(proc)
                    raise subprocess.TimeoutExpired(cmd, inactivity_timeout) from exc
    finally:
        reader.join(timeout=5)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def _validate_name(name: str, label: str) -> None:
    """Validate that a name component is safe (no path traversal)."""
    if not name or not _OWNER_REPO_RE.match(name):
        print(f"Error: invalid {label}: {name!r}", file=sys.stderr)
        sys.exit(1)
    if name in (".", "..") or ".." in name:
        print(f"Error: path traversal in {label}: {name!r}", file=sys.stderr)
        sys.exit(1)


def resolve_repo(owner_repo: str) -> Path:
    """
    Resolve an owner/repo reference to a local path.

    1. Check if we're already inside the repo (cwd matches)
    2. Check the cache directory
    3. Clone via gh if not found

    Returns the path to the local repo root.
    """
    owner, repo = _parse_owner_repo(owner_repo)

    # Check if cwd is already inside this repo
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        if result.stdout.strip().lower() == f"{owner}/{repo}".lower():
            # Use git to find the actual repo root
            root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True, timeout=5,
            )
            return Path(root.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    # Check cache
    cached = CACHE_DIR / owner / repo
    # Verify resolved path is actually under CACHE_DIR (prevent symlink attacks)
    if not cached.resolve().is_relative_to(CACHE_DIR.resolve()):
        print(f"Error: resolved path escapes cache directory", file=sys.stderr)
        sys.exit(1)
    if cached.exists() and (cached / ".git").exists():
        # Pull latest
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=cached, capture_output=True, check=False, timeout=30,
        )
        return cached

    # Clone via gh — into a TEMP path, renamed to the final cache path only on
    # success. A killed/interrupted clone can then never leave a directory at
    # `cached` that a later resolve's cache-hit check (above) would treat as
    # valid (chief-wiggum#268 AC2).
    cached.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {owner_repo}...", file=sys.stderr)
    # mkdtemp's empty dir is a valid `git clone` target as-is (git clones INTO
    # an existing empty directory without complaint).
    tmp_target = Path(tempfile.mkdtemp(prefix=f".{repo}-clone-", dir=str(cached.parent)))
    try:
        _clone_with_inactivity_timeout(["gh", "repo", "clone", owner_repo, str(tmp_target)])
    except BaseException:
        shutil.rmtree(tmp_target, ignore_errors=True)
        raise
    if cached.exists():
        shutil.rmtree(cached, ignore_errors=True)
    os.replace(tmp_target, cached)
    return cached


def list_repos() -> list[str]:
    """List all cached repos as owner/repo strings."""
    repos = []
    if not CACHE_DIR.exists():
        return repos
    for owner_dir in sorted(CACHE_DIR.iterdir()):
        if not owner_dir.is_dir():
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if (repo_dir / ".git").exists():
                repos.append(f"{owner_dir.name}/{repo_dir.name}")
    return repos


def clean_repo(owner_repo: str) -> bool:
    """Remove a cached repo clone."""
    owner, repo = _parse_owner_repo(owner_repo)
    cached = CACHE_DIR / owner / repo
    if cached.exists():
        shutil.rmtree(cached)
        return True
    return False


def _parse_owner_repo(owner_repo: str) -> tuple[str, str]:
    """Parse 'owner/repo' or 'owner/repo#123' into (owner, repo)."""
    # Strip issue number if present
    repo_part = owner_repo.split("#")[0]
    if repo_part != repo_part.strip("/"):
        print(f"Error: expected owner/repo format, got: {owner_repo}", file=sys.stderr)
        sys.exit(1)
    parts = repo_part.split("/")
    if len(parts) != 2:
        print(f"Error: expected owner/repo format, got: {owner_repo}", file=sys.stderr)
        sys.exit(1)
    owner, repo = parts[0], parts[1]
    _validate_name(owner, "owner")
    _validate_name(repo, "repo")
    return owner, repo


def main():
    if len(sys.argv) < 2:
        print("Usage: repo.py <resolve|list|clean> [owner/repo]")
        print()
        print("Commands:")
        print("  resolve owner/repo   Resolve to local path (clone if needed)")
        print("  home                 Print chief-wiggum install directory")
        print("  list                 List cached repos")
        print("  clean owner/repo     Remove a cached clone")
        print()
        print(f"Cache dir: {CACHE_DIR}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "home":
        print(CW_HOME)
        return

    if cmd == "list":
        repos = list_repos()
        if repos:
            print(f"Cached repos ({len(repos)}):")
            for r in repos:
                print(f"  {r}")
        else:
            print("No cached repos.")
        return

    if len(sys.argv) < 3:
        print(f"Usage: repo.py {cmd} owner/repo", file=sys.stderr)
        sys.exit(1)

    owner_repo = sys.argv[2]

    if cmd == "resolve":
        path = resolve_repo(owner_repo)
        print(path)

    elif cmd == "clean":
        if clean_repo(owner_repo):
            print(f"Removed {owner_repo}")
        else:
            print(f"{owner_repo} not found in cache")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

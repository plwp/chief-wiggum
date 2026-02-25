#!/usr/bin/env python3
"""
Repository management for chief-wiggum.

Resolves owner/repo references to local paths, cloning via `gh` if needed.
Repos are cached in ~/.chief-wiggum/repos/ to avoid re-cloning.

As a module:
    from repo import resolve_repo
    path = resolve_repo("plwp/dgrd")  # returns Path to local clone

As a CLI:
    python3 repo.py resolve plwp/dgrd       # print local path (clone if needed)
    python3 repo.py list                      # list cached repos
    python3 repo.py clean plwp/dgrd          # remove a cached clone
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

CACHE_DIR = Path.home() / ".chief-wiggum" / "repos"

# chief-wiggum root is two levels up from this script (scripts/ -> root)
CW_HOME = Path(__file__).resolve().parent.parent

# Strict pattern: alphanumeric, hyphens, underscores, dots (GitHub rules)
_OWNER_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


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

    # Clone via gh
    cached.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {owner_repo}...", file=sys.stderr)
    subprocess.run(
        ["gh", "repo", "clone", owner_repo, str(cached)],
        check=True, timeout=120,
    )
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
    parts = repo_part.strip("/").split("/")
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

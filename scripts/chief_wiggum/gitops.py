"""Worktree and branch safety checks (P1-8).

The command prompts repeatedly warn sub-agents not to operate on the main
checkout, create rogue PRs, or merge from the wrong branch — but enforcement is
prose. This module makes those checks executable.

Every helper is **non-destructive**: the read helpers only inspect git state,
and the one mutating helper (``create_staging_branch``) only ever *creates* a
branch — none run destructive commands (no ``reset --hard``, ``clean -f``,
``push --force``, ``branch -D``). A ``runner`` is injectable so the logic is
unit-testable with mocked subprocess calls.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]


class GitSafetyError(RuntimeError):
    """Raised when a worktree/branch safety invariant is violated."""


def _git(args: list[str], cwd: str | Path, runner: Runner) -> subprocess.CompletedProcess:
    return runner(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def is_clean(repo: str | Path, *, runner: Runner = subprocess.run) -> bool:
    """True if the working tree has no staged/unstaged/untracked changes."""
    result = _git(["status", "--porcelain"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"git status failed: {(result.stderr or '').strip()}")
    return result.stdout.strip() == ""


def current_branch(repo: str | Path, *, runner: Runner = subprocess.run) -> str:
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"cannot resolve current branch: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def worktree_root(repo: str | Path, *, runner: Runner = subprocess.run) -> Path:
    result = _git(["rev-parse", "--show-toplevel"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"not a git worktree: {(result.stderr or '').strip()}")
    return Path(result.stdout.strip())


def changed_files(
    repo: str | Path, base: str, *, runner: Runner = subprocess.run
) -> list[str]:
    """Files changed on HEAD relative to ``base`` (``base...HEAD``)."""
    result = _git(["diff", "--name-only", f"{base}...HEAD"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"git diff failed: {(result.stderr or '').strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


# --- branch name validation (pure) ------------------------------------------

_INVALID_BRANCH_CHARS = re.compile(r"[ ~^:?*\[\\\x00-\x1f\x7f]")


def is_valid_branch_name(name: str) -> bool:
    """Validate a branch name against git's ref-format rules (pure, no subprocess)."""
    if not name or name in (".", ".."):
        return False
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return False
    if name.startswith("-"):
        return False
    if name.endswith(".") or name.endswith(".lock"):
        return False
    if ".." in name or "@{" in name or name == "@":
        return False
    if _INVALID_BRANCH_CHARS.search(name):
        return False
    # Git applies these rules to every "/"-separated path component, not just the
    # whole ref: no component may be empty, start with ".", or end with ".lock"/".".
    for component in name.split("/"):
        if not component or component.startswith("."):
            return False
        if component.endswith(".lock") or component.endswith("."):
            return False
    return True


def assert_branch_name(name: str) -> None:
    if not is_valid_branch_name(name):
        raise GitSafetyError(f"invalid branch name: {name!r}")


# --- worktree isolation -----------------------------------------------------


def assert_worktree(
    worktree: str | Path,
    main_checkout: str | Path,
    *,
    runner: Runner = subprocess.run,
) -> Path:
    """Assert ``worktree`` is a real git worktree distinct from the main checkout.

    Returns the resolved worktree root. Raises if it resolves to the same path
    as the main checkout (a sub-agent must never operate on the main checkout).
    """
    wt_root = worktree_root(worktree, runner=runner).resolve()
    main_root = worktree_root(main_checkout, runner=runner).resolve()
    if wt_root == main_root:
        raise GitSafetyError(
            f"refusing to operate on the main checkout: worktree {wt_root} == main {main_root}"
        )
    return wt_root


# --- fast-forward promotion -------------------------------------------------


def can_fast_forward(
    repo: str | Path, base: str, branch: str, *, runner: Runner = subprocess.run
) -> bool:
    """True if ``base`` can fast-forward to ``branch`` (base is an ancestor).

    Distinguishes "not fast-forwardable" (returns False) from a git error such
    as an unknown ref (raises GitSafetyError).
    """
    result = _git(["merge-base", "--is-ancestor", base, branch], repo, runner)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitSafetyError(
        f"cannot compare {base}..{branch}: {(result.stderr or '').strip()}"
    )


def create_staging_branch(
    repo: str | Path,
    name: str,
    start_point: str,
    *,
    runner: Runner = subprocess.run,
) -> str:
    """Create a (non-destructive) staging branch at ``start_point``.

    Validates the name first and uses ``git branch`` (never ``-f``/``-D``), so
    an existing branch causes a failure rather than a clobber.
    """
    assert_branch_name(name)
    # Reject an option-like start point and pass ``--`` so neither operand can be
    # parsed as an option (e.g. a start_point of ``-D`` turning this destructive).
    if start_point.startswith("-"):
        raise GitSafetyError(f"invalid start point: {start_point!r}")
    result = _git(["branch", "--", name, start_point], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(
            f"could not create staging branch {name!r}: {(result.stderr or '').strip()}"
        )
    return name

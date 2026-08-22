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

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]


class GitSafetyError(RuntimeError):
    """Raised when a worktree/branch safety invariant is violated."""


def _git(args: list[str], cwd: str | Path, runner: Runner) -> subprocess.CompletedProcess:
    return runner(
        [os.environ.get("CW_WAVE_REAL_GIT", "git"), *args],
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


# --- parallel-worker Git command guard (#376) -------------------------------

_WAVE_GIT_SAFE_GLOBAL_FLAGS = {
    "--glob-pathspecs",
    "--icase-pathspecs",
    "--literal-pathspecs",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "--noglob-pathspecs",
    "--paginate",
}
_WAVE_GIT_FORBIDDEN_GLOBAL_OPTIONS = {
    "--bare",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--work-tree",
    "-c",
}
_WAVE_GIT_FORBIDDEN_SUBCOMMANDS = {"stash", "update-ref"}


def _stash_safety_error() -> GitSafetyError:
    return GitSafetyError(
        "git stash is forbidden for parallel workers because refs/stash is shared "
        "by every worktree; preserve intended ticket files with a worktree-local "
        "WIP commit instead"
    )


def _references_stash(token: str) -> bool:
    """Recognize a stash ref both directly and inside fetch/push refspecs."""
    return any(
        component.lstrip("+-^ ").startswith("refs/stash")
        for component in token.split(":")
    )


def validate_wave_git_args(
    args: Iterable[str], worktree: str | Path
) -> tuple[list[str], Path, str]:
    """Validate worker-controlled Git arguments before any Git command executes.

    Returns normalized arguments, the effective cwd after Git's repeated ``-C``
    options, and the subcommand. Repository/config routing is deliberately
    rejected: it could escape the declared worktree or smuggle ``stash`` through
    an inline alias. Configured aliases are checked separately against the real
    repository by :func:`assert_wave_git_command`.
    """
    normalized = list(args)
    if normalized and normalized[0] == "--":
        normalized = normalized[1:]
    if not normalized:
        raise GitSafetyError("wave-git requires a Git subcommand after --")

    for token in normalized:
        if token == "stash" or _references_stash(token):
            raise _stash_safety_error()

    effective_cwd = Path(worktree).resolve()
    index = 0
    subcommand = ""
    while index < len(normalized):
        token = normalized[index]
        if token == "-C":
            if index + 1 >= len(normalized):
                raise GitSafetyError("git -C requires a path")
            destination = Path(normalized[index + 1])
            effective_cwd = (
                destination if destination.is_absolute() else effective_cwd / destination
            ).resolve()
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            destination = Path(token[2:])
            effective_cwd = (
                destination if destination.is_absolute() else effective_cwd / destination
            ).resolve()
            index += 1
            continue
        if token in _WAVE_GIT_SAFE_GLOBAL_FLAGS:
            index += 1
            continue
        if token in _WAVE_GIT_FORBIDDEN_GLOBAL_OPTIONS or any(
            token.startswith(f"{option}=")
            for option in _WAVE_GIT_FORBIDDEN_GLOBAL_OPTIONS
            if option.startswith("--")
        ):
            raise GitSafetyError(
                f"wave-git forbids repository/config routing option {token!r}"
            )
        if token == "--":
            index += 1
            if index >= len(normalized):
                raise GitSafetyError("wave-git requires a Git subcommand after --")
            subcommand = normalized[index]
            break
        if token.startswith("-"):
            raise GitSafetyError(f"wave-git does not allow global Git option {token!r}")
        subcommand = token
        break

    if not subcommand:
        raise GitSafetyError("wave-git requires a Git subcommand after --")
    if subcommand in _WAVE_GIT_FORBIDDEN_SUBCOMMANDS:
        raise _stash_safety_error()
    return normalized, effective_cwd, subcommand


def active_git_hooks(
    repo: str | Path, *, runner: Runner = subprocess.run
) -> list[Path]:
    """Return executable hooks Git would run from the repository's hooks path."""
    result = _git(
        ["rev-parse", "--path-format=absolute", "--git-path", "hooks"], repo, runner
    )
    if result.returncode != 0:
        raise GitSafetyError(
            f"cannot resolve Git hooks path: {(result.stderr or '').strip()}"
        )
    hooks_dir = Path(result.stdout.strip())
    if not hooks_dir.is_dir():
        return []
    return sorted(
        path
        for path in hooks_dir.iterdir()
        if path.is_file()
        and not path.name.endswith(".sample")
        and os.access(path, os.X_OK)
    )


# @cw-trace guards INV-dag-013
def assert_wave_git_command(
    args: Iterable[str],
    worktree: str | Path,
    main_checkout: str | Path,
    *,
    runner: Runner = subprocess.run,
) -> tuple[list[str], Path, Path]:
    """Validate a wave-worker Git command and return its argv and worktree root."""
    invocation_cwd = Path(worktree).resolve()
    declared_root = assert_worktree(invocation_cwd, main_checkout, runner=runner)
    normalized, effective_cwd, subcommand = validate_wave_git_args(args, invocation_cwd)
    effective_root = worktree_root(effective_cwd, runner=runner).resolve()
    if effective_root != declared_root:
        raise GitSafetyError(
            f"wave-git must remain in the declared worker worktree {declared_root}; "
            f"the command resolves to {effective_root}"
        )

    hooks = active_git_hooks(effective_cwd, runner=runner)
    if hooks:
        names = ", ".join(path.name for path in hooks)
        raise GitSafetyError(
            "wave-git refuses execution while active Git hooks are present "
            f"({names}); fan-out cannot prove that hooks avoid shared refs/stash"
        )

    alias = _git(["config", "--get", f"alias.{subcommand}"], effective_cwd, runner)
    if alias.returncode == 0:
        raise GitSafetyError(
            f"Git aliases are forbidden in wave-git (subcommand {subcommand!r}); "
            "spell out the built-in command so stash cannot be hidden"
        )
    if alias.returncode != 1:
        raise GitSafetyError(
            f"cannot inspect Git alias {subcommand!r}: {(alias.stderr or '').strip()}"
        )
    return normalized, invocation_cwd, declared_root


def wave_git_environment(
    main_checkout: str | Path,
    worktree: str | Path,
    *,
    environ: dict[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, str]:
    """Return environment overrides that route raw/nested Git through wave-git."""
    declared_root = assert_worktree(worktree, main_checkout, runner=runner)
    source_env = os.environ if environ is None else environ
    real_git = source_env.get("CW_WAVE_REAL_GIT") or shutil.which(
        "git", path=source_env.get("PATH")
    )
    if not real_git:
        raise GitSafetyError("cannot locate the real Git executable for wave-git")
    scripts_dir = Path(__file__).resolve().parents[1]
    shim_dir = scripts_dir / "wave-git-bin"
    return {
        "CW_WAVE_MAIN": str(Path(main_checkout).resolve()),
        "CW_WAVE_WORKTREE_ROOT": str(declared_root),
        "CW_WAVE_REAL_GIT": str(Path(real_git).resolve()),
        "CW_WAVE_GIT_SAFETY": str(scripts_dir / "git_safety.py"),
        "PATH": f"{shim_dir}{os.pathsep}{source_env.get('PATH', '')}",
    }


def run_wave_git(
    args: Iterable[str],
    worktree: str | Path,
    main_checkout: str | Path,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Run an allowed wave-worker Git command, preserving Git's exit status."""
    normalized, invocation_cwd, _ = assert_wave_git_command(
        args, worktree, main_checkout, runner=runner
    )
    child_env = os.environ.copy()
    child_env.update(
        wave_git_environment(main_checkout, worktree, runner=runner)
    )
    real_git = child_env["CW_WAVE_REAL_GIT"]
    result = runner(
        [real_git, *normalized], cwd=str(invocation_cwd), env=child_env
    )
    return result.returncode


def assert_main_pristine(
    main_checkout: str | Path,
    default_branch: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    """Assert the MAIN checkout is pristine: on ``default_branch`` with a clean tree.

    The orchestrator-side complement of ``assert_worktree``. It catches the isolation
    LEAK that ``assert_worktree`` (which each worker runs on itself) cannot see: a worker
    that ran ``git checkout`` in the main checkout instead of its worktree leaves main on
    a feature branch, silently contaminating the base of the next worker/wave. Called
    before creating worktrees and before merging, so a leak fails loudly and early
    instead of surfacing later as a mystified diff.
    """
    branch = current_branch(main_checkout, runner=runner)
    if branch != default_branch:
        raise GitSafetyError(
            f"main checkout is on {branch!r}, not the default branch {default_branch!r} — "
            f"a worker likely checked out a branch in the main checkout instead of its "
            f"worktree (isolation leak). Restore: git -C <main> checkout {default_branch}"
        )
    if not is_clean(main_checkout, runner=runner):
        raise GitSafetyError(
            f"main checkout has uncommitted changes on {default_branch!r} — refusing to "
            f"branch/merge off a dirty base. Inspect `git -C <main> status` first."
        )


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


# --- worktree teardown (#329) ------------------------------------------------
#
# Neither /implement nor /implement-wave ever ran `git worktree remove` — every
# ticket/wave worker's worktree accumulated forever in the shared target-repo
# cache checkout. The safe cleanup criterion is "this worktree's branch is
# provably merged into the default branch" (`git branch --merged`), which is
# ALSO exactly the set a workflow wants to remove: a merged ticket has no more
# local work to do, and a parked ticket (protected-path violation, unresolved
# conflict, failed review) is by definition NOT merged, so it is never touched
# by this sweep without any separate "is this ticket parked" bookkeeping.


def list_worktrees(repo: str | Path, *, runner: Runner = subprocess.run) -> list[dict]:
    """Parse `git worktree list --porcelain` into a list of {path, head, branch}.

    `branch` is None for a detached-HEAD worktree (never auto-removed by
    `gc_merged_worktrees` below — an unnamed branch can't be proven merged).
    """
    result = _git(["worktree", "list", "--porcelain"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"git worktree list failed: {(result.stderr or '').strip()}")
    entries: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):], "branch": None}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
    if current:
        entries.append(current)
    return entries


def merged_branches(
    repo: str | Path, default_branch: str, *, runner: Runner = subprocess.run
) -> set[str]:
    """Local branches already merged into ``default_branch`` (ancestor check)."""
    result = _git(
        ["branch", "--merged", default_branch, "--format=%(refname:short)"], repo, runner
    )
    if result.returncode != 0:
        raise GitSafetyError(f"git branch --merged failed: {(result.stderr or '').strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def remove_worktree(
    repo: str | Path, path: str | Path, *, force: bool = False, runner: Runner = subprocess.run
) -> None:
    """Remove a single worktree. Never `-D`/`reset --hard` — `worktree remove`
    already refuses a worktree with uncommitted changes unless `force=True`."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    result = _git(args, repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"could not remove worktree {path}: {(result.stderr or '').strip()}")


def prune_worktrees(repo: str | Path, *, runner: Runner = subprocess.run) -> None:
    result = _git(["worktree", "prune"], repo, runner)
    if result.returncode != 0:
        raise GitSafetyError(f"git worktree prune failed: {(result.stderr or '').strip()}")


def gc_merged_worktrees(
    repo: str | Path,
    default_branch: str,
    *,
    keep_branches: Iterable[str] = (),
    force: bool = False,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> list[dict]:
    """Remove every non-main worktree whose branch is merged into ``default_branch``.

    Safe to call opportunistically from any workflow step that has the target
    repo path: it never touches the main worktree (branch == default_branch,
    or a resolved path match), never touches a detached-HEAD worktree, never
    touches a branch explicitly listed in ``keep_branches`` (parked tickets
    the caller knows about), and never touches an unmerged branch (a ticket
    still in flight, or one that failed and was never merged). Returns the
    removed entries; prunes stale worktree admin state afterward if anything
    was removed.
    """
    repo_root = worktree_root(repo, runner=runner).resolve()
    entries = list_worktrees(repo, runner=runner)
    merged = merged_branches(repo, default_branch, runner=runner)
    keep = set(keep_branches)
    removed: list[dict] = []
    for entry in entries:
        branch = entry.get("branch")
        if entry.get("bare"):
            continue
        if branch is None:
            continue  # detached HEAD — can't prove merged, never auto-removed
        if branch == default_branch:
            continue  # the main worktree
        try:
            if Path(entry["path"]).resolve() == repo_root:
                continue
        except OSError:
            pass
        if branch in keep:
            continue  # parked — explicit keep
        if branch not in merged:
            continue  # not merged — still in flight or never landed
        if not dry_run:
            remove_worktree(repo, entry["path"], force=force, runner=runner)
        removed.append(entry)
    if removed and not dry_run:
        prune_worktrees(repo, runner=runner)
    return removed

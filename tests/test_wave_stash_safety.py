"""Shared-stash reproduction and wave-worker guard coverage (#376)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from chief_wiggum import gitops


ROOT = Path(__file__).resolve().parents[1]
GIT_SAFETY = ROOT / "scripts" / "git_safety.py"


def _git_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        }
    )
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def sibling_worktrees(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    main = tmp_path / "main"
    worker_a = tmp_path / "worker-a"
    worker_b = tmp_path / "worker-b"
    env = _git_env(tmp_path)
    main.mkdir()
    _git(main, "init", "-q", "-b", "main", env=env)
    _git(main, "config", "user.name", "CW Test", env=env)
    _git(main, "config", "user.email", "cw@example.invalid", env=env)
    (main / "a.txt").write_text("base-a\n")
    (main / "b.txt").write_text("base-b\n")
    _git(main, "add", "a.txt", "b.txt", env=env)
    _git(main, "commit", "-q", "-m", "base", env=env)
    _git(main, "worktree", "add", "-q", "-b", "worker-a", str(worker_a), env=env)
    _git(main, "worktree", "add", "-q", "-b", "worker-b", str(worker_b), env=env)
    return main, worker_a, worker_b, env


def _wave_git(
    main: Path,
    worktree: Path,
    *args: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GIT_SAFETY),
            "wave-git",
            "--main",
            str(main),
            "--worktree",
            str(worktree),
            "--",
            *args,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
    )


def test_sibling_worktrees_really_share_the_stash_stack(sibling_worktrees):
    """Characterize the live failure: A pops B's newer repository-global entry."""
    main, worker_a, worker_b, env = sibling_worktrees
    (worker_a / "a.txt").write_text("worker-a\n")
    _git(worker_a, "stash", "push", "-m", "worker-a", env=env)
    (worker_b / "b.txt").write_text("worker-b\n")
    _git(worker_b, "stash", "push", "-m", "worker-b", env=env)

    assert len(_git(main, "stash", "list", env=env).stdout.splitlines()) == 2
    _git(worker_a, "stash", "pop", env=env)

    assert (worker_a / "b.txt").read_text() == "worker-b\n"
    assert (worker_a / "a.txt").read_text() == "base-a\n"
    remaining = _git(main, "stash", "list", env=env).stdout
    assert "worker-a" in remaining
    assert "worker-b" not in remaining


# @cw-trace verifies INV-dag-013
def test_wave_guard_refuses_stash_without_touching_ref_or_dirty_work(sibling_worktrees):
    main, worker_a, worker_b, env = sibling_worktrees
    (worker_b / "b.txt").write_text("existing stash entry\n")
    _git(worker_b, "stash", "push", "-m", "worker-b-existing", env=env)
    (worker_a / "a.txt").write_text("uncommitted worker-a\n")
    before_ref = _git(main, "rev-parse", "--verify", "--quiet", "refs/stash", env=env).stdout
    before_diff = _git(worker_a, "diff", "--", "a.txt", env=env).stdout

    result = _wave_git(main, worker_a, "stash", "push", "-m", "unsafe", env=env)

    assert result.returncode == 1
    assert "refs/stash is shared" in result.stderr
    assert "WIP commit" in result.stderr
    assert _git(main, "rev-parse", "--verify", "--quiet", "refs/stash", env=env).stdout == before_ref
    assert _git(worker_a, "diff", "--", "a.txt", env=env).stdout == before_diff


def test_guarded_wip_commits_are_recoverable_on_independent_branches(sibling_worktrees):
    main, worker_a, worker_b, env = sibling_worktrees
    (worker_a / "a.txt").write_text("saved-a\n")
    (worker_b / "b.txt").write_text("saved-b\n")

    for worktree, filename, message in (
        (worker_a, "a.txt", "WIP: worker A"),
        (worker_b, "b.txt", "WIP: worker B"),
    ):
        assert _wave_git(main, worktree, "add", "--", filename, env=env).returncode == 0
        assert _wave_git(main, worktree, "commit", "-m", message, env=env).returncode == 0

    assert _git(main, "show", "worker-a:a.txt", env=env).stdout == "saved-a\n"
    assert _git(main, "show", "worker-a:b.txt", env=env).stdout == "base-b\n"
    assert _git(main, "show", "worker-b:a.txt", env=env).stdout == "base-a\n"
    assert _git(main, "show", "worker-b:b.txt", env=env).stdout == "saved-b\n"


@pytest.mark.parametrize(
    "args",
    [
        ["stash"],
        ["--no-pager", "stash", "pop"],
        ["-C", "subdir", "stash", "push"],
        ["update-ref", "refs/stash", "HEAD"],
        ["-c", "alias.save=stash", "save"],
    ],
)
def test_wave_git_validator_rejects_stash_and_indirection(args, tmp_path):
    with pytest.raises(gitops.GitSafetyError):
        gitops.validate_wave_git_args(args, tmp_path)


@pytest.mark.parametrize(
    "args",
    [
        ["--git-dir", "/tmp/elsewhere", "status"],
        ["--work-tree=/tmp/elsewhere", "status"],
        ["--config-env", "alias.save=ENV", "save"],
        ["--exec-path=/tmp/helpers", "status"],
    ],
)
def test_wave_git_validator_rejects_repository_and_config_routing(args, tmp_path):
    with pytest.raises(gitops.GitSafetyError):
        gitops.validate_wave_git_args(args, tmp_path)


def test_wave_git_rejects_sibling_reroute_and_configured_alias(sibling_worktrees):
    main, worker_a, worker_b, env = sibling_worktrees
    reroute = _wave_git(main, worker_a, "-C", str(worker_b), "status", env=env)
    assert reroute.returncode == 1
    assert "declared worker worktree" in reroute.stderr

    _git(worker_a, "config", "alias.inspect", "status", env=env)
    alias = _wave_git(main, worker_a, "inspect", env=env)
    assert alias.returncode == 1
    assert "aliases are forbidden" in alias.stderr


def test_wave_git_allows_subdirectory_and_propagates_git_exit(sibling_worktrees):
    main, worker_a, _, env = sibling_worktrees
    subdir = worker_a / "nested"
    subdir.mkdir()
    allowed = _wave_git(main, worker_a, "-C", "nested", "status", "--short", env=env)
    assert allowed.returncode == 0

    failed = _wave_git(main, worker_a, "show", "missing-ref", env=env)
    assert failed.returncode not in (0, 1, 2)
    assert "unknown revision" in failed.stderr or "bad revision" in failed.stderr


def test_worker_contract_and_both_harness_views_require_guarded_wip_commits():
    contract = (ROOT / "docs" / "worker-contracts.md").read_text()
    claude_workflow = (ROOT / ".claude" / "commands" / "implement-wave.md").read_text()
    portable_workflow = (
        ROOT / "skills" / "chief-wiggum" / "references" / "workflows" / "implement-wave.md"
    ).read_text()

    for text in (contract, claude_workflow, portable_workflow):
        assert "git stash" in text
        assert "wave-git" in text
        assert "WIP commit" in text

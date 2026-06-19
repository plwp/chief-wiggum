"""Tests for worktree and branch safety checks (P1-8)."""

from __future__ import annotations

import subprocess

import git_safety
import pytest
from chief_wiggum import gitops


def _runner(stdout="", returncode=0, stderr=""):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return run


# --- cleanliness ------------------------------------------------------------


def test_is_clean_true_on_empty_porcelain():
    assert gitops.is_clean(".", runner=_runner(stdout="")) is True


def test_is_clean_false_with_changes():
    assert gitops.is_clean(".", runner=_runner(stdout=" M file.py\n?? new.py\n")) is False


def test_is_clean_raises_on_git_error():
    with pytest.raises(gitops.GitSafetyError):
        gitops.is_clean(".", runner=_runner(returncode=128, stderr="not a repo"))


# --- branch name validation -------------------------------------------------


@pytest.mark.parametrize("name", ["feat/x", "fix-123", "release/v1.2.3", "a/b/c"])
def test_valid_branch_names(name):
    assert gitops.is_valid_branch_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "", "has space", "ends.lock", "..", "/leading", "trailing/", "a..b",
        "x~y", "feat^", "q?", "-dash", "double//slash",
        "foo.lock/bar", "foo/bar.lock/baz", "foo/.hidden", ".lead/x", "a/b./c",
    ],
)
def test_invalid_branch_names(name):
    assert gitops.is_valid_branch_name(name) is False


def test_assert_branch_name_raises():
    with pytest.raises(gitops.GitSafetyError):
        gitops.assert_branch_name("bad name")


# --- worktree isolation -----------------------------------------------------


def test_assert_worktree_rejects_main_checkout(tmp_path):
    # Both resolve to the same toplevel -> violation.
    same = str(tmp_path)
    runner = _runner(stdout=same)
    with pytest.raises(gitops.GitSafetyError, match="main checkout"):
        gitops.assert_worktree(tmp_path, tmp_path, runner=runner)


def test_assert_worktree_accepts_distinct_paths(tmp_path):
    wt = tmp_path / "wt"
    main = tmp_path / "main"
    wt.mkdir()
    main.mkdir()

    def run(args, **kwargs):
        # Return the toplevel matching the cwd we were called with.
        return subprocess.CompletedProcess(args, 0, stdout=kwargs["cwd"], stderr="")

    root = gitops.assert_worktree(wt, main, runner=run)
    assert root == wt.resolve()


# --- fast-forward -----------------------------------------------------------


def test_can_fast_forward_true_when_ancestor():
    assert gitops.can_fast_forward(".", "main", "feat", runner=_runner(returncode=0)) is True


def test_can_fast_forward_false_when_not_ancestor():
    assert gitops.can_fast_forward(".", "main", "feat", runner=_runner(returncode=1)) is False


def test_can_fast_forward_raises_on_unknown_ref():
    with pytest.raises(gitops.GitSafetyError):
        gitops.can_fast_forward(".", "main", "nope", runner=_runner(returncode=128, stderr="bad ref"))


# --- changed files / staging branch -----------------------------------------


def test_changed_files_parses_names():
    out = "a.py\nb/c.py\n\n"
    assert gitops.changed_files(".", "main", runner=_runner(stdout=out)) == ["a.py", "b/c.py"]


def test_create_staging_branch_validates_name_first():
    with pytest.raises(gitops.GitSafetyError):
        gitops.create_staging_branch(".", "bad name", "main", runner=_runner())


def test_create_staging_branch_maps_git_failure():
    with pytest.raises(gitops.GitSafetyError, match="staging branch"):
        gitops.create_staging_branch(
            ".", "staging/x", "main", runner=_runner(returncode=128, stderr="exists")
        )


def test_create_staging_branch_rejects_option_like_start_point():
    with pytest.raises(gitops.GitSafetyError, match="start point"):
        gitops.create_staging_branch(".", "staging/x", "-D", runner=_runner())


def test_create_staging_branch_passes_double_dash():
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    gitops.create_staging_branch(".", "staging/x", "main", runner=run)
    assert captured["args"] == ["git", "branch", "--", "staging/x", "main"]


# --- CLI --------------------------------------------------------------------


def test_cli_check_branch_ok(capsys):
    assert git_safety.main(["check-branch", "feat/x"]) == 0


def test_cli_check_branch_bad(capsys):
    assert git_safety.main(["check-branch", "bad name"]) == 1
    assert "Error" in capsys.readouterr().err


def test_cli_assert_worktree_same_path_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "assert_worktree", lambda *a, **k: (_ for _ in ()).throw(gitops.GitSafetyError("main checkout")))
    rc = git_safety.main(["assert-worktree", "--main", str(tmp_path)])
    assert rc == 1

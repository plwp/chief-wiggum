"""Tests for worktree and branch safety checks (P1-8)."""

from __future__ import annotations

import json
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


# --- main-checkout pristine guard (#96) -------------------------------------


def _pristine_runner(*, branch="main", porcelain=""):
    """Runner that answers `rev-parse` (current branch) and `status` (cleanliness)
    differently, so assert_main_pristine's two git calls can be driven independently."""
    def run(args, **kwargs):
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, stdout=branch, stderr="")
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return run


def test_assert_main_pristine_ok_on_clean_default_branch():
    # No exception when main is on the default branch with a clean tree.
    gitops.assert_main_pristine(".", "main", runner=_pristine_runner(branch="main", porcelain=""))


def test_assert_main_pristine_rejects_feature_branch_in_main():
    # The #96 failure: a worker left main on a feature branch (isolation leak).
    with pytest.raises(gitops.GitSafetyError, match="isolation leak"):
        gitops.assert_main_pristine(".", "main", runner=_pristine_runner(branch="feat/162"))


def test_assert_main_pristine_rejects_dirty_tree():
    with pytest.raises(gitops.GitSafetyError, match="uncommitted"):
        gitops.assert_main_pristine(
            ".", "main", runner=_pristine_runner(branch="main", porcelain=" M svc.go\n")
        )


def test_assert_main_pristine_cli_wired():
    # The subcommand is registered and delegates to gitops (feature-branch main -> exit 1).
    rc = git_safety.main(["assert-main-pristine", "--main", ".", "--default-branch", "main"])
    assert rc in (0, 1)  # 0 if this repo is on main+clean, 1 otherwise — never a crash/usage error


# --- worktree teardown (#329) ------------------------------------------------

_PORCELAIN = """\
worktree /repo/main
HEAD aaa111
branch refs/heads/main

worktree /repo/.claude/worktrees/42
HEAD bbb222
branch refs/heads/feat/42-merged

worktree /repo/.claude/worktrees/43
HEAD ccc333
branch refs/heads/feat/43-parked

worktree /repo/.claude/worktrees/detached
HEAD ddd444
detached
"""


def _wt_runner(*, merged="main\nfeat/42-merged\n", remove_ok=True, prune_ok=True):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "worktree"] and args[2] == "list":
            return subprocess.CompletedProcess(args, 0, stdout=_PORCELAIN, stderr="")
        if args[:2] == ["git", "branch"] and "--merged" in args:
            return subprocess.CompletedProcess(args, 0, stdout=merged, stderr="")
        if args[:3] == ["git", "worktree", "remove"]:
            rc = 0 if remove_ok else 1
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="" if remove_ok else "dirty")
        if args[:3] == ["git", "worktree", "prune"]:
            rc = 0 if prune_ok else 1
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="" if prune_ok else "boom")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, stdout="/repo/main", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    run.calls = calls
    return run


def test_list_worktrees_parses_porcelain():
    entries = gitops.list_worktrees(".", runner=_wt_runner())
    assert entries == [
        {"path": "/repo/main", "head": "aaa111", "branch": "main"},
        {"path": "/repo/.claude/worktrees/42", "head": "bbb222", "branch": "feat/42-merged"},
        {"path": "/repo/.claude/worktrees/43", "head": "ccc333", "branch": "feat/43-parked"},
        {"path": "/repo/.claude/worktrees/detached", "head": "ddd444", "branch": None},
    ]


def test_merged_branches_parses_refnames():
    assert gitops.merged_branches(".", "main", runner=_wt_runner()) == {"main", "feat/42-merged"}


def test_gc_merged_worktrees_removes_only_merged_non_main():
    runner = _wt_runner()
    removed = gitops.gc_merged_worktrees("/repo/main", "main", runner=runner)
    assert [e["branch"] for e in removed] == ["feat/42-merged"]
    remove_calls = [c for c in runner.calls if c[:3] == ["git", "worktree", "remove"]]
    assert remove_calls == [["git", "worktree", "remove", "/repo/.claude/worktrees/42"]]
    # Prune runs because something was removed.
    assert any(c[:3] == ["git", "worktree", "prune"] for c in runner.calls)


def test_gc_merged_worktrees_never_touches_main_or_detached():
    runner = _wt_runner(merged="main\nfeat/42-merged\n")
    removed = gitops.gc_merged_worktrees("/repo/main", "main", runner=runner)
    branches = {e["branch"] for e in removed}
    assert "main" not in branches
    assert None not in branches


def test_gc_merged_worktrees_keeps_unmerged_parked_ticket():
    # feat/43-parked never appears in --merged output -> never removed, no
    # bookkeeping needed to mark it "parked".
    runner = _wt_runner(merged="main\nfeat/42-merged\n")
    removed = gitops.gc_merged_worktrees("/repo/main", "main", runner=runner)
    assert "feat/43-parked" not in {e["branch"] for e in removed}


def test_gc_merged_worktrees_respects_explicit_keep_even_if_merged():
    # Even a MERGED branch is preserved if the caller explicitly keeps it.
    runner = _wt_runner(merged="main\nfeat/42-merged\n")
    removed = gitops.gc_merged_worktrees(
        "/repo/main", "main", keep_branches=["feat/42-merged"], runner=runner
    )
    assert removed == []


def test_gc_merged_worktrees_dry_run_removes_nothing():
    runner = _wt_runner()
    removed = gitops.gc_merged_worktrees("/repo/main", "main", dry_run=True, runner=runner)
    assert [e["branch"] for e in removed] == ["feat/42-merged"]
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in runner.calls)
    assert not any(c[:3] == ["git", "worktree", "prune"] for c in runner.calls)


def test_gc_merged_worktrees_no_prune_when_nothing_removed():
    runner = _wt_runner(merged="main\n")
    removed = gitops.gc_merged_worktrees("/repo/main", "main", runner=runner)
    assert removed == []
    assert not any(c[:3] == ["git", "worktree", "prune"] for c in runner.calls)


def test_remove_worktree_raises_on_failure():
    with pytest.raises(gitops.GitSafetyError, match="could not remove worktree"):
        gitops.remove_worktree(".", "/repo/x", runner=_wt_runner(remove_ok=False))


def test_cli_gc_worktrees_dry_run(capsys):
    import git_safety as gs

    orig = gitops.gc_merged_worktrees
    gitops.gc_merged_worktrees = lambda *a, **k: [{"path": "/repo/.claude/worktrees/42", "branch": "feat/42-merged"}]
    try:
        rc = gs.main(["gc-worktrees", "--repo", "/repo/main", "--default-branch", "main", "--dry-run"])
    finally:
        gitops.gc_merged_worktrees = orig
    assert rc == 0
    out = capsys.readouterr().out
    assert "would remove" in out
    assert "feat/42-merged" in out


def test_cli_list_worktrees_emits_json(capsys):
    orig = gitops.list_worktrees
    gitops.list_worktrees = lambda repo: [{"path": "/repo/main", "branch": "main"}]
    try:
        rc = git_safety.main(["list-worktrees", "--repo", "/repo/main"])
    finally:
        gitops.list_worktrees = orig
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"path": "/repo/main", "branch": "main"}]

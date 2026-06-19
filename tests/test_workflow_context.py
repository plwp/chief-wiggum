"""Tests for the shared workflow context resolver (P0-1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from chief_wiggum import context

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- parse_target / issue parsing -------------------------------------------


def test_parse_target_repo_only():
    ref = context.parse_target("acme/widget-api")
    assert (ref.owner, ref.repo, ref.issue) == ("acme", "widget-api", None)
    assert ref.slug == "acme/widget-api"


def test_parse_target_with_issue_suffix():
    ref = context.parse_target("acme/widget-api#42")
    assert ref.issue == 42


@pytest.mark.parametrize("ref", ["acme/repo#0", "acme/repo#-3", "acme/repo#abc", "acme/repo#"])
def test_parse_target_rejects_bad_issue(ref):
    with pytest.raises(ValueError):
        context.parse_target(ref)


def test_parse_target_raises_valueerror_on_path_traversal():
    # The reusable helper must raise ValueError, not leak repo.py's SystemExit.
    with pytest.raises(ValueError):
        context.parse_target("acme/../repo")


# --- env override + cwd discovery -------------------------------------------


def test_resolve_uses_env_override_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEF_WIGGUM_HOME", str(REPO_ROOT))
    ctx = context.resolve(tmp=tmp_path)
    assert ctx.home == REPO_ROOT


def test_resolve_discovers_home_from_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("CHIEF_WIGGUM_HOME", raising=False)
    monkeypatch.delenv("CW_HOME", raising=False)
    monkeypatch.chdir(REPO_ROOT)
    ctx = context.resolve(tmp=tmp_path)
    assert ctx.home == REPO_ROOT


# --- temp dir creation ------------------------------------------------------


def test_resolve_creates_real_unique_temp_dir(monkeypatch):
    # Exercise the real side effect: each resolve() with no tmp override must
    # create a fresh, unique, existing session directory.
    ctx_a = context.resolve(home=REPO_ROOT)
    ctx_b = context.resolve(home=REPO_ROOT)
    try:
        assert ctx_a.tmp.is_dir()
        assert ctx_b.tmp.is_dir()
        assert ctx_a.tmp != ctx_b.tmp
    finally:
        for ctx in (ctx_a, ctx_b):
            ctx.tmp.rmdir()


def test_resolve_reuses_supplied_temp_dir(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    ctx = context.resolve(home=REPO_ROOT, tmp=session)
    assert ctx.tmp == session


def test_ticket_tmp_is_per_issue(tmp_path):
    ctx = context.WorkflowContext(home=REPO_ROOT, tmp=tmp_path, default_branch="main", issue=42)
    sub = ctx.ticket_tmp()
    assert sub == tmp_path / "42"
    assert sub.is_dir()


# --- epic slugging ----------------------------------------------------------


def test_resolve_slugs_epic_and_builds_epic_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(context.repo, "resolve_repo", lambda slug: tmp_path / "repo")
    monkeypatch.setattr(context, "detect_default_branch", lambda p: "main")
    ctx = context.resolve(
        "acme/app",
        epic="Epic: Order Lifecycle!",
        tmp=tmp_path,
        home=REPO_ROOT,
        branch_detector=lambda p: "main",
    )
    assert ctx.epic_slug == "epic-order-lifecycle"
    assert ctx.epic_dir == tmp_path / "repo" / "docs" / "epics" / "epic-order-lifecycle"


def test_no_epic_means_no_epic_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(context.repo, "resolve_repo", lambda slug: tmp_path / "repo")
    ctx = context.resolve(
        "acme/app", tmp=tmp_path, home=REPO_ROOT, branch_detector=lambda p: "main"
    )
    assert ctx.epic_slug is None
    assert ctx.epic_dir is None


# --- default branch detection + fallback ------------------------------------


def _fake_runner(stdout="", returncode=0, raises=None):
    def runner(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return runner


def test_detect_default_branch_from_gh(tmp_path):
    branch = context.detect_default_branch(
        tmp_path, runner=_fake_runner(stdout="develop\n", returncode=0)
    )
    assert branch == "develop"


def test_detect_default_branch_falls_back_to_main(tmp_path):
    branch = context.detect_default_branch(tmp_path, runner=_fake_runner(stdout="", returncode=1))
    assert branch == "main"


def test_detect_default_branch_survives_subprocess_error(tmp_path):
    branch = context.detect_default_branch(
        tmp_path, runner=_fake_runner(raises=OSError("gh missing"))
    )
    assert branch == "main"


def test_resolve_without_repo_assumes_main(tmp_path):
    ctx = context.resolve(tmp=tmp_path, home=REPO_ROOT)
    assert ctx.default_branch == "main"
    assert ctx.repo_path is None
    assert ctx.target is None


# --- serialization ----------------------------------------------------------


def test_to_dict_and_shell_exports(monkeypatch, tmp_path):
    monkeypatch.setattr(context.repo, "resolve_repo", lambda slug: tmp_path / "repo")
    ctx = context.resolve(
        "acme/app#7",
        epic="Epic: Name",
        tmp=tmp_path,
        home=REPO_ROOT,
        branch_detector=lambda p: "trunk",
    )
    data = ctx.to_dict()
    assert data["target"] == "acme/app"
    assert data["issue"] == 7
    assert data["default_branch"] == "trunk"
    assert data["epic_slug"] == "epic-name"

    exports = ctx.shell_exports()
    assert "export DEFAULT_BRANCH='trunk'" in exports
    assert "export ISSUE_NUMBER=7" in exports
    assert "export EPIC_SLUG='epic-name'" in exports
    assert 'export EPIC_DIR="$TARGET_REPO/docs/epics/epic-name"' in exports


def test_shell_exports_quote_hostile_values(monkeypatch, tmp_path):
    # A repo path / branch with spaces, single quotes, or shell metacharacters
    # must be safely quoted so `eval` cannot execute injected commands.
    nasty = tmp_path / "re po';touch pwned;'"
    monkeypatch.setattr(context.repo, "resolve_repo", lambda slug: nasty)
    ctx = context.resolve(
        "acme/app",
        tmp=tmp_path,
        home=REPO_ROOT,
        branch_detector=lambda p: "feat/x'; rm -rf /; '",
    )
    exports = ctx.shell_exports()
    # The injected command text survives only inside a single-quoted literal.
    assert "touch pwned" not in exports.replace(context.env.shell_quote(str(nasty)), "")
    assert context.env.shell_quote(str(nasty)) in exports
    assert context.env.shell_quote("feat/x'; rm -rf /; '") in exports

"""Tests for the epic artifact installer (P1-12)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import install_epic_artifacts as ie
import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "docs" / "formal-methods" / "examples"


def _source(tmp_path, *, ui_spec=False, missing=()):
    src = tmp_path / "src"
    src.mkdir()
    for name in ie.REQUIRED_PROSE:
        if name not in missing:
            (src / name).write_text(f"# {name}")
    shutil.copy(EXAMPLES / "order-lifecycle.contracts.json", src / "contracts.json")
    shutil.copy(EXAMPLES / "order-lifecycle.state-machine.json", src / "state-machines.json")
    if ui_spec:
        shutil.copy(EXAMPLES / "kanban-app-ui-spec.json", src / "ui-spec.json")
        (src / "ui-spec.md").write_text("# UI Spec")
    for name in missing:
        (src / name).unlink(missing_ok=True)
    return src


def _epic(tmp_path, slug="e"):
    # The installer requires epic_dir == <target_repo>/docs/epics/<slug>.
    return tmp_path / "docs" / "epics" / slug


def _noop_transition(target, sm, out):
    out.write_text("{}")


# --- validation -------------------------------------------------------------


def test_missing_required_artifacts_raises(tmp_path):
    src = _source(tmp_path, missing=("contracts.md",))
    with pytest.raises(ie.InstallError, match="contracts.md"):
        ie.install_epic_artifacts(
            src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=False, transition_map_fn=_noop_transition,
        )


def test_validate_source_lists_missing(tmp_path):
    src = _source(tmp_path, missing=("adr.md", "contracts.json"))
    assert set(ie.validate_source(src)) == {"adr.md", "contracts.json"}


# --- install ----------------------------------------------------------------


def test_install_copies_prose_and_models(tmp_path):
    src = _source(tmp_path)
    epic = _epic(tmp_path)
    result = ie.install_epic_artifacts(
        src, epic, epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=_noop_transition,
    )
    assert (epic / "contracts.md").exists()
    assert (epic / "models" / "state-machines.json").exists()
    assert "models/contracts.json" in result.copied
    # Machine/test views generated.
    assert any("test-paths.json" in g for g in result.generated)
    assert result.transition_map and Path(result.transition_map).exists()


def test_optional_ui_spec_installed_when_present(tmp_path):
    src = _source(tmp_path, ui_spec=True)
    epic = _epic(tmp_path)
    result = ie.install_epic_artifacts(
        src, epic, epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=_noop_transition,
    )
    assert (epic / "ui-spec.md").exists()
    assert (epic / "models" / "ui-spec.json").exists()
    assert "[UI Spec]" in result.issue_comment


def test_ui_spec_absent_omitted_from_comment(tmp_path):
    src = _source(tmp_path)
    result = ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=_noop_transition,
    )
    assert "[UI Spec]" not in result.issue_comment


# --- transition map invocation ----------------------------------------------


def test_transition_map_fn_invoked_with_args(tmp_path):
    src = _source(tmp_path)
    calls = {}

    def tm(target, sm, out):
        calls["target"] = target
        calls["sm"] = sm
        out.write_text("{}")

    ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=tm,
    )
    assert calls["target"] == tmp_path
    assert calls["sm"].name == "state-machines.json"


def test_transition_map_failure_is_warning_not_fatal(tmp_path):
    src = _source(tmp_path)

    def boom(target, sm, out):
        raise OSError("verify_transitions missing")

    result = ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=boom,
    )
    assert any("transition map" in w for w in result.warnings)
    assert result.transition_map is None


# --- dry run ----------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    src = _source(tmp_path)
    epic = _epic(tmp_path)
    result = ie.install_epic_artifacts(
        src, epic, epic_name="E", epic_slug="e", target_repo=tmp_path,
        dry_run=True, transition_map_fn=_noop_transition,
    )
    assert result.dry_run is True
    assert not epic.exists()
    assert "contracts.md" in result.copied  # planned
    assert result.issue_comment  # still rendered


# --- commit guard -----------------------------------------------------------


def test_refuses_commit_on_dirty_repo(tmp_path):
    src = _source(tmp_path)

    def dirty_git(args, **kwargs):
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, stdout=" M file\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(ie.InstallError, match="uncommitted"):
        ie.install_epic_artifacts(
            src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=True, transition_map_fn=_noop_transition, git_runner=dirty_git,
        )


def test_allow_dirty_overrides_clean_check(tmp_path):
    src = _source(tmp_path)
    calls = []

    def git(args, **kwargs):
        calls.append(args[1] if len(args) > 1 else args[0])
        return subprocess.CompletedProcess(args, 0, stdout=" M dirty\n", stderr="")

    result = ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=True, allow_dirty=True, transition_map_fn=_noop_transition, git_runner=git,
    )
    assert result.committed is True
    assert "commit" in calls


# --- path safety + commit failures ------------------------------------------


def test_rejects_epic_dir_outside_target(tmp_path):
    src = _source(tmp_path)
    with pytest.raises(ie.InstallError, match="epic_dir must be"):
        ie.install_epic_artifacts(
            src, tmp_path / "elsewhere", epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=False, transition_map_fn=_noop_transition,
        )


def test_rejects_invalid_slug(tmp_path):
    src = _source(tmp_path)
    with pytest.raises(ie.InstallError, match="invalid epic slug"):
        ie.install_epic_artifacts(
            src, _epic(tmp_path, "Bad Slug"), epic_name="E", epic_slug="Bad Slug", target_repo=tmp_path,
            commit=False, transition_map_fn=_noop_transition,
        )


def test_commit_failure_raises(tmp_path):
    src = _source(tmp_path)

    def git(args, **kwargs):
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no identity")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(ie.InstallError, match="git commit failed"):
        ie.install_epic_artifacts(
            src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=True, allow_dirty=True, transition_map_fn=_noop_transition, git_runner=git,
        )


def test_nothing_to_commit_is_not_failure(tmp_path):
    src = _source(tmp_path)

    def git(args, **kwargs):
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, stdout="nothing to commit, working tree clean", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    result = ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=True, allow_dirty=True, transition_map_fn=_noop_transition, git_runner=git,
    )
    assert result.committed is False
    assert any("nothing to commit" in w for w in result.warnings)


def test_git_add_stages_only_this_epic(tmp_path):
    src = _source(tmp_path)
    staged = {}

    def git(args, **kwargs):
        if "add" in args:
            staged["path"] = args[-1]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=True, allow_dirty=True, transition_map_fn=_noop_transition, git_runner=git,
    )
    assert staged["path"] == "docs/epics/e"


def test_incomplete_ui_spec_pair_warns_and_skips(tmp_path):
    src = _source(tmp_path)
    (src / "ui-spec.md").write_text("# UI")  # md present, json absent
    result = ie.install_epic_artifacts(
        src, _epic(tmp_path), epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=_noop_transition,
    )
    assert any("incomplete UI spec" in w for w in result.warnings)
    assert "[UI Spec]" not in result.issue_comment
    assert not (_epic(tmp_path) / "ui-spec.md").exists()


# --- issue comment rendering ------------------------------------------------


def test_render_issue_comment_links_artifacts():
    body = ie.render_issue_comment("order-lifecycle", "Epic: Orders", has_ui_spec=False)
    assert "## Epic Architecture" in body
    assert "docs/epics/order-lifecycle/contracts.md" in body


# --- CLI --------------------------------------------------------------------


def test_cli_dry_run(tmp_path, capsys):
    src = _source(tmp_path)
    rc = ie.main([
        "--source", str(src), "--epic-dir", str(_epic(tmp_path)),
        "--epic-name", "E", "--epic-slug", "e", "--target-repo", str(tmp_path), "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cli_missing_artifacts_exit_1(tmp_path, capsys):
    src = _source(tmp_path, missing=("invariants.md",))
    rc = ie.main([
        "--source", str(src), "--epic-dir", str(_epic(tmp_path)),
        "--epic-name", "E", "--epic-slug", "e", "--target-repo", str(tmp_path), "--no-commit",
    ])
    assert rc == 1
    assert "invariants.md" in capsys.readouterr().err

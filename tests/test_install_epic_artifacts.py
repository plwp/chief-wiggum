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


def _noop_transition(target, sm, out):
    out.write_text("{}")


# --- validation -------------------------------------------------------------


def test_missing_required_artifacts_raises(tmp_path):
    src = _source(tmp_path, missing=("contracts.md",))
    with pytest.raises(ie.InstallError, match="contracts.md"):
        ie.install_epic_artifacts(
            src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=False, transition_map_fn=_noop_transition,
        )


def test_validate_source_lists_missing(tmp_path):
    src = _source(tmp_path, missing=("adr.md", "contracts.json"))
    assert set(ie.validate_source(src)) == {"adr.md", "contracts.json"}


# --- install ----------------------------------------------------------------


def test_install_copies_prose_and_models(tmp_path):
    src = _source(tmp_path)
    epic = tmp_path / "epic"
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
    epic = tmp_path / "epic"
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
        src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
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
        src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=tm,
    )
    assert calls["target"] == tmp_path
    assert calls["sm"].name == "state-machines.json"


def test_transition_map_failure_is_warning_not_fatal(tmp_path):
    src = _source(tmp_path)

    def boom(target, sm, out):
        raise OSError("verify_transitions missing")

    result = ie.install_epic_artifacts(
        src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=False, transition_map_fn=boom,
    )
    assert any("transition map" in w for w in result.warnings)
    assert result.transition_map is None


# --- dry run ----------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    src = _source(tmp_path)
    epic = tmp_path / "epic"
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
            src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
            commit=True, transition_map_fn=_noop_transition, git_runner=dirty_git,
        )


def test_allow_dirty_overrides_clean_check(tmp_path):
    src = _source(tmp_path)
    calls = []

    def git(args, **kwargs):
        calls.append(args[1] if len(args) > 1 else args[0])
        return subprocess.CompletedProcess(args, 0, stdout=" M dirty\n", stderr="")

    result = ie.install_epic_artifacts(
        src, tmp_path / "epic", epic_name="E", epic_slug="e", target_repo=tmp_path,
        commit=True, allow_dirty=True, transition_map_fn=_noop_transition, git_runner=git,
    )
    assert result.committed is True
    assert "commit" in calls


# --- issue comment rendering ------------------------------------------------


def test_render_issue_comment_links_artifacts():
    body = ie.render_issue_comment("order-lifecycle", "Epic: Orders", has_ui_spec=False)
    assert "## Epic Architecture" in body
    assert "docs/epics/order-lifecycle/contracts.md" in body


# --- CLI --------------------------------------------------------------------


def test_cli_dry_run(tmp_path, capsys):
    src = _source(tmp_path)
    rc = ie.main([
        "--source", str(src), "--epic-dir", str(tmp_path / "epic"),
        "--epic-name", "E", "--epic-slug", "e", "--target-repo", str(tmp_path), "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cli_missing_artifacts_exit_1(tmp_path, capsys):
    src = _source(tmp_path, missing=("invariants.md",))
    rc = ie.main([
        "--source", str(src), "--epic-dir", str(tmp_path / "epic"),
        "--epic-name", "E", "--epic-slug", "e", "--target-repo", str(tmp_path), "--no-commit",
    ])
    assert rc == 1
    assert "invariants.md" in capsys.readouterr().err

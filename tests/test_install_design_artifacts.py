"""Tests for the design artifact installer (P2-15)."""

from __future__ import annotations

import json
import subprocess

import install_design_artifacts as idesign
import pytest


def _design(assets=None, tbd=False):
    d = {"source": {"kind": "net-new"}, "tokens": {"colors": {"primary": "#003f5c"}}}
    if tbd:
        d["source"]["notes"] = "TBD: confirm brand colour with client"
    if assets is not None:
        d["assets"] = assets
    return d


def _ref_asset(path="docs/design/reference/home.png"):
    return {"id": "home-ref", "type": "reference-screenshot", "path": path, "applies_to": ["home"]}


def _setup(tmp_path, *, design=None, mockups=("home.html",), screenshots=("home.png",)):
    dj = tmp_path / "design.json"
    dj.write_text(json.dumps(design if design is not None else _design()))
    mock_dir = tmp_path / "direction"
    mock_dir.mkdir()
    for m in mockups:
        (mock_dir / m).write_text("<html>:root{}</html>")
    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    for s in screenshots:
        (shot_dir / s).write_bytes(b"img")
    return dj, mock_dir, shot_dir


def _noop_git(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


# --- validation -------------------------------------------------------------


def test_invalid_design_json_raises(tmp_path):
    dj, mock, shots = _setup(tmp_path, design={"tokens": {}})  # missing 'source'
    with pytest.raises(idesign.DesignInstallError, match="invalid design.json"):
        idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=False)


def test_malformed_json_raises(tmp_path):
    dj = tmp_path / "design.json"
    dj.write_text("{not json")
    (tmp_path / "d").mkdir()
    (tmp_path / "s").mkdir()
    with pytest.raises(idesign.DesignInstallError, match="cannot read"):
        idesign.install_design_artifacts(dj, tmp_path / "d", tmp_path / "s", tmp_path, commit=False)


# --- assembly ---------------------------------------------------------------


def test_install_copies_and_renders_styleguide(tmp_path):
    dj, mock, shots = _setup(tmp_path, design=_design(assets=[_ref_asset()]))
    result = idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=False)
    dd = tmp_path / "docs" / "design"
    assert (dd / "design.json").exists()
    assert (dd / "mockups" / "home.html").exists()
    assert (dd / "reference" / "home.png").exists()
    assert (dd / "styleguide.html").exists()
    assert result.styleguide and "styleguide.html" in result.styleguide
    assert result.broken_assets == []


def test_missing_screenshots_warns(tmp_path):
    dj, mock, shots = _setup(tmp_path, screenshots=())
    result = idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=False)
    assert any("no reference screenshots" in w for w in result.warnings)


def test_broken_asset_reference_warns(tmp_path):
    # design.json references missing.png but only home.png is installed.
    dj, mock, shots = _setup(
        tmp_path, design=_design(assets=[_ref_asset("docs/design/reference/missing.png")])
    )
    result = idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=False)
    assert result.broken_assets == ["docs/design/reference/missing.png"]
    assert any("reference-screenshot asset" in w for w in result.warnings)


# --- TBD markers ------------------------------------------------------------


def test_tbd_markers_reported_with_frontend_impact(tmp_path):
    dj, mock, shots = _setup(tmp_path, design=_design(tbd=True))
    result = idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=False)
    assert result.tbd_markers
    assert result.blocks_frontend is True
    assert any("frontend tickets will be gated" in w for w in result.warnings)


# --- dry run ----------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    dj, mock, shots = _setup(tmp_path)
    result = idesign.install_design_artifacts(dj, mock, shots, tmp_path, dry_run=True)
    assert result.dry_run is True
    assert not (tmp_path / "docs" / "design").exists()
    assert "design.json" in result.copied
    assert "styleguide.html" in result.copied


# --- commit guard -----------------------------------------------------------


def test_refuses_commit_on_dirty_repo(tmp_path):
    dj, mock, shots = _setup(tmp_path)

    def dirty(args, **kwargs):
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, stdout=" M f\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(idesign.DesignInstallError, match="uncommitted"):
        idesign.install_design_artifacts(dj, mock, shots, tmp_path, commit=True, git_runner=dirty)


def test_commit_failure_raises(tmp_path):
    dj, mock, shots = _setup(tmp_path)

    def git(args, **kwargs):
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no identity")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(idesign.DesignInstallError, match="git commit failed"):
        idesign.install_design_artifacts(
            dj, mock, shots, tmp_path, commit=True, allow_dirty=True, git_runner=git
        )


def test_commit_success(tmp_path):
    dj, mock, shots = _setup(tmp_path)
    result = idesign.install_design_artifacts(
        dj, mock, shots, tmp_path, commit=True, allow_dirty=True, git_runner=_noop_git
    )
    assert result.committed is True


# --- CLI --------------------------------------------------------------------


def test_cli_dry_run(tmp_path, capsys):
    dj, mock, shots = _setup(tmp_path)
    rc = idesign.main([
        "--design-json", str(dj), "--mockups", str(mock), "--screenshots", str(shots),
        "--target-repo", str(tmp_path), "--dry-run",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cli_invalid_design_exit_1(tmp_path, capsys):
    dj, mock, shots = _setup(tmp_path, design={"tokens": {}})
    rc = idesign.main([
        "--design-json", str(dj), "--mockups", str(mock), "--screenshots", str(shots),
        "--target-repo", str(tmp_path), "--no-commit",
    ])
    assert rc == 1
    assert "invalid design.json" in capsys.readouterr().err

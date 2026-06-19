"""Tests for model-derived test artifact generation (P1-6)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import generate_formal_test_artifacts as gen

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "docs" / "formal-methods" / "examples"
SM_EXAMPLE = EXAMPLES / "order-lifecycle.state-machine.json"
CONTRACTS_EXAMPLE = EXAMPLES / "order-lifecycle.contracts.json"


def _models_dir(tmp_path, *, state_machine=False, contracts=False) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    if state_machine:
        shutil.copy(SM_EXAMPLE, d / "state-machines.json")
    if contracts:
        shutil.copy(CONTRACTS_EXAMPLE, d / "contracts.json")
    return d


def _result(manifest, name):
    return next(r for r in manifest.results if r.name == name)


# --- per-model presence -----------------------------------------------------


def test_state_machine_only(tmp_path):
    models = _models_dir(tmp_path, state_machine=True)
    out = tmp_path / "out"
    manifest = gen.generate_artifacts(models, out)
    assert _result(manifest, "state-machines.json").status == "ok"
    assert _result(manifest, "contracts.json").status == "missing"
    assert manifest.generated_files
    assert manifest.ok is True


def test_contracts_only(tmp_path):
    models = _models_dir(tmp_path, contracts=True)
    out = tmp_path / "out"
    manifest = gen.generate_artifacts(models, out)
    assert _result(manifest, "contracts.json").status == "ok"
    assert _result(manifest, "state-machines.json").status == "missing"
    assert manifest.ok is True


def test_both_models(tmp_path):
    models = _models_dir(tmp_path, state_machine=True, contracts=True)
    out = tmp_path / "out"
    manifest = gen.generate_artifacts(models, out)
    assert _result(manifest, "state-machines.json").status == "ok"
    assert _result(manifest, "contracts.json").status == "ok"
    # Contract assertions + test plan produced.
    names = {Path(f).name for f in manifest.generated_files}
    assert "contract-assertions.md" in names
    assert "test-plan.md" in names


def test_missing_models_dir_yields_all_missing(tmp_path):
    out = tmp_path / "out"
    manifest = gen.generate_artifacts(tmp_path / "models", out)
    assert all(r.status == "missing" for r in manifest.results)
    assert manifest.ok is True
    assert manifest.generated_files == []


# --- invalid / malformed ----------------------------------------------------


def test_schema_mismatch_is_invalid(tmp_path):
    # A contracts.json that actually holds a state machine must be rejected.
    models = tmp_path / "models"
    models.mkdir()
    shutil.copy(SM_EXAMPLE, models / "contracts.json")
    manifest = gen.generate_artifacts(models, tmp_path / "out")
    result = _result(manifest, "contracts.json")
    assert result.status == "invalid"
    assert any("expected contracts schema" in e for e in result.errors)
    assert manifest.ok is False


def test_valid_ui_spec_with_no_test_artifacts_is_empty_not_failure(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    shutil.copy(EXAMPLES / "kanban-app-ui-spec.json", models / "ui-spec.json")
    manifest = gen.generate_artifacts(models, tmp_path / "out")
    result = _result(manifest, "ui-spec.json")
    assert result.status == "empty"
    assert result.files == []
    assert manifest.ok is True  # empty is not a failure


def test_rerun_clears_stale_artifacts_when_model_removed(tmp_path):
    models = _models_dir(tmp_path, state_machine=True)
    out = tmp_path / "out"
    first = gen.generate_artifacts(models, out)
    assert first.generated_files
    # Remove the model and rerun; previously-generated files must be cleared.
    (models / "state-machines.json").unlink()
    gen.generate_artifacts(models, out)
    for stale in first.generated_files:
        assert not Path(stale).exists()


def test_invalid_model_reports_errors_and_not_ok(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    # Schema-detectable but invalid (state-machine missing required fields).
    (models / "state-machines.json").write_text(json.dumps({"states": {}}))
    manifest = gen.generate_artifacts(models, tmp_path / "out")
    result = _result(manifest, "state-machines.json")
    assert result.status == "invalid"
    assert result.errors
    assert manifest.ok is False


def test_malformed_json_is_reported(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "contracts.json").write_text("{not json")
    manifest = gen.generate_artifacts(models, tmp_path / "out")
    assert _result(manifest, "contracts.json").status == "malformed"
    assert manifest.ok is False


# --- idempotency / overwrite ------------------------------------------------


def test_rerun_overwrites_and_is_stable(tmp_path):
    models = _models_dir(tmp_path, state_machine=True)
    out = tmp_path / "out"
    first = gen.generate_artifacts(models, out)
    second = gen.generate_artifacts(models, out)
    assert sorted(first.generated_files) == sorted(second.generated_files)


# --- manifest + CLI ---------------------------------------------------------


def test_manifest_file_written_and_serializable(tmp_path):
    models = _models_dir(tmp_path, state_machine=True)
    out = tmp_path / "out"
    gen.generate_artifacts(models, out)
    manifest_path = out / "formal-artifacts-manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["ok"] is True
    assert "generated_files" in data


def test_cli_markdown(tmp_path, capsys):
    models = _models_dir(tmp_path, contracts=True)
    rc = gen.main([str(models), "--output", str(tmp_path / "out"), "--markdown"])
    assert rc == 0
    assert "# Formal Test Artifacts" in capsys.readouterr().out


def test_cli_exit_nonzero_on_invalid(tmp_path, capsys):
    models = tmp_path / "models"
    models.mkdir()
    (models / "contracts.json").write_text("{not json")
    rc = gen.main([str(models), "--output", str(tmp_path / "out")])
    assert rc == 1

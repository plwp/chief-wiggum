"""An artifact type with no schema is UNSUPPORTED, not malformed (#289).

`formal_models.py validate` on `models/test-paths.json` used to die with

    ValueError: Cannot detect schema type from document structure

and exit 1 — indistinguishable from a genuinely broken document. `test-paths.json`
is a legitimate artifact `render_models.py` writes; it simply has no schema under
`templates/formal-models/`.

Two different claims shared one output, which is exactly the conflation #289
separated everywhere else:

    this document is malformed        -> a real problem
    this type has no schema           -> expected, and NOT a pass either

The second is now its own state with its own exit code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "formal_models.py"
sys.path.insert(0, str(REPO / "scripts"))

from formal_models import UNSCHEMAD_PREFIX, detect_schema_type  # noqa: E402

UNSUPPORTED_EXIT = 3


def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "validate", str(path)],
                          capture_output=True, text=True)


def test_test_paths_is_recognised_rather_than_unknown():
    kind = detect_schema_type({"paths": [], "summary": {}})
    assert kind.startswith(UNSCHEMAD_PREFIX)
    assert kind.endswith("test-paths")


def test_a_genuinely_unrecognisable_document_still_raises():
    """The real failure must stay a failure. Widening detection to swallow
    everything would trade one conflation for a worse one."""
    with pytest.raises(ValueError):
        detect_schema_type({"nothing": "familiar"})


def test_unsupported_exits_three_not_zero(tmp_path):
    """Never 0. `nothing was checked` is not a pass — the whole point."""
    artifact = tmp_path / "test-paths.json"
    artifact.write_text(json.dumps({"paths": [], "summary": {}}))
    result = _run(artifact)
    assert result.returncode == UNSUPPORTED_EXIT
    assert "UNSUPPORTED" in result.stdout
    assert "NOT a pass" in result.stdout


def test_unsupported_exits_three_not_one(tmp_path):
    """Never 1 either. Exit 1 is `this document is invalid`, which is a claim
    about the artifact rather than about the checker's coverage."""
    artifact = tmp_path / "test-paths.json"
    artifact.write_text(json.dumps({"paths": [], "summary": {}}))
    assert _run(artifact).returncode != 1


def test_the_message_names_the_type(tmp_path):
    artifact = tmp_path / "test-paths.json"
    artifact.write_text(json.dumps({"paths": [], "summary": {}}))
    assert "test-paths" in _run(artifact).stdout


def test_a_malformed_document_is_still_reported_as_invalid(tmp_path):
    """A schema'd type with real errors must not be swept into UNSUPPORTED."""
    artifact = tmp_path / "contracts.json"
    artifact.write_text(json.dumps({"entities": [{"no_name": True}]}))
    result = _run(artifact)
    assert result.returncode == 1
    assert "INVALID" in result.stdout


def test_a_valid_document_still_passes(tmp_path):
    artifact = tmp_path / "contracts.json"
    artifact.write_text(json.dumps({
        "entities": [{"name": "Order", "fields": [
            {"name": "id", "type": "string"}]}]}))
    result = _run(artifact)
    assert result.returncode == 0
    assert "VALID" in result.stdout


@pytest.mark.parametrize(
    "artifact",
    sorted((REPO / "docs" / "epics").glob("*/models/*.json")),
    ids=lambda p: f"{p.parent.parent.name}/{p.name}",
)
def test_every_committed_model_reports_a_meaningful_state(artifact):
    """No model may answer with a traceback. Before this, both epics'
    test-paths.json did exactly that."""
    result = _run(artifact)
    assert result.returncode in (0, 1, UNSUPPORTED_EXIT), result.stderr[:200]
    assert "Traceback" not in result.stderr, (
        f"{artifact.name} answers with a traceback rather than a stated outcome")


def test_there_are_committed_models_to_check():
    models = list((REPO / "docs" / "epics").glob("*/models/*.json"))
    assert len(models) >= 8, f"expected committed models, found {len(models)}"

import json
import subprocess
import sys
from pathlib import Path

import formal_models as fm
from chief_wiggum import trace_ids

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "formal-methods" / "examples"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def test_state_machine_example_validates_and_analyzes():
    model = load_example("order-lifecycle.state-machine.json")

    assert fm.detect_schema_type(model) == "state-machine"
    assert fm.validate(model) == []

    analysis = fm.analyze_graph(model)
    assert analysis.initial == "draft"
    assert not analysis.has_unreachable_states
    assert analysis.all_terminals_reachable
    assert analysis.transition_count > 0


def test_contract_example_validates():
    contracts = load_example("order-lifecycle.contracts.json")

    assert fm.detect_schema_type(contracts) == "contracts"
    assert fm.validate(contracts) == []


def test_xstate_conversion_preserves_initial_and_final_states():
    model = load_example("order-lifecycle.state-machine.json")
    xstate = fm.to_xstate(model)

    assert xstate["initial"] == model["initial"]
    terminal_states = {
        state for state, definition in model["states"].items() if definition.get("type") == "terminal"
    }
    assert terminal_states
    for state in terminal_states:
        assert xstate["states"][state]["type"] == "final"


# --- #293: malformed stable ids caught at model-authoring time --------------
#
# A malformed id (two-segment KIND-NNN, no slug) in a formal model is
# currently VALID per jsonschema (neither state-machine-schema.json nor
# contracts-schema.json patterns their id fields) yet invisible to
# check_traceability.py's DEFINE_RE/ID_RE (chief-wiggum#281). find_malformed_ids
# reuses chief_wiggum.trace_ids.near_miss_ids() (the same declaration-position
# grammar complement check_traceability.py uses) so this is a single detector,
# not a second regex.


def test_find_malformed_ids_reuses_trace_ids_near_miss_ids():
    # Identity, not equality: this must be the SAME function object
    # check_traceability.py uses, never a second copy.
    assert fm.find_malformed_ids is trace_ids.near_miss_ids


def test_find_malformed_ids_flags_two_segment_declaration():
    raw = json.dumps({"invariants": [{"id": "INV-001", "description": "x"}]})
    assert fm.find_malformed_ids(raw) == ["INV-001"]


def test_find_malformed_ids_ignores_well_formed_ids():
    raw = json.dumps({"invariants": [{"id": "INV-order-001", "description": "x"}]})
    assert fm.find_malformed_ids(raw) == []


def test_real_examples_have_no_malformed_ids():
    # Report-only dry-run evidence (docs/gate-rollout.md item 2) against this
    # repo's own real, already-shipped formal models: zero malformed ids.
    for name in ("order-lifecycle.state-machine.json", "order-lifecycle.contracts.json"):
        raw = (EXAMPLES / name).read_text()
        assert fm.find_malformed_ids(raw) == [], name


def _write_model(tmp_path: Path, model: dict) -> Path:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    return path


def _malformed_state_machine() -> dict:
    # A schema-VALID (no pattern to violate) state machine whose invariant id
    # is the two-segment brownfield shape #293 exists to surface.
    return {
        "name": "m",
        "states": {"a": {"type": "initial"}, "b": {"type": "terminal"}},
        "initial": "a",
        "transitions": [{"from": "a", "to": "b", "event": "go"}],
        "invariants": [{"id": "INV-001", "description": "malformed two-segment id"}],
    }


def test_malformed_id_does_not_change_schema_validity():
    model = _malformed_state_machine()
    assert fm.validate(model) == []  # #293: no schema `pattern` added to $defs.invariant.id
    assert fm.find_malformed_ids(json.dumps(model)) == ["INV-001"]


def test_cli_validate_default_is_report_only_does_not_hard_fail(tmp_path):
    # AC: default behavior does NOT hard-fail an adopted repo with
    # pre-existing two-segment models.
    path = _write_model(tmp_path, _malformed_state_machine())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "formal_models.py"), "validate", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INV-001" in proc.stdout
    assert "WARNING" in proc.stdout or "warning" in proc.stdout.lower()


def test_cli_validate_strict_ids_blocks(tmp_path):
    path = _write_model(tmp_path, _malformed_state_machine())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "formal_models.py"), "validate", "--strict-ids", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "INV-001" in proc.stdout


def test_cli_validate_clean_model_has_no_warning(tmp_path):
    model = _malformed_state_machine()
    model["invariants"][0]["id"] = "INV-order-001"
    path = _write_model(tmp_path, model)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "formal_models.py"), "validate", "--strict-ids", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "malformed" not in proc.stdout.lower()

import json
from copy import deepcopy
from pathlib import Path

import pytest
from chief_wiggum.dag import (
    ErrorCode,
    canonical_json_bytes,
    load_authority_matrix,
    validate_canonical_bytes,
    validate_mutation,
    validate_snapshot,
    validate_transition,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dag" / "v1"
TRANSITIONS = json.loads((FIXTURES / "transitions.json").read_text())
ALL_PAIRS = [(before, after) for before in TRANSITIONS["states"] for after in TRANSITIONS["states"]]
LEGAL = {tuple(pair) for pair in TRANSITIONS["legal"]}
TERMINAL = set(TRANSITIONS["terminal"])


def _snapshot() -> dict:
    return json.loads((FIXTURES / "valid" / "full-snapshot.json").read_text())


@pytest.mark.parametrize("before,after", ALL_PAIRS, ids=lambda value: value)
def test_every_lifecycle_pair_is_named_and_enforced(before: str, after: str):
    errors = validate_transition(before, after)
    if (before, after) in LEGAL:
        assert errors == ()
    elif before in TERMINAL:
        assert [error.code for error in errors] == [ErrorCode.TERMINAL_STATE_IMMUTABLE]
    else:
        assert [error.code for error in errors] == [ErrorCode.STATE_TRANSITION_INVALID]


def test_schedulable_cycle_is_invalid_but_non_scheduling_cycle_is_valid():
    snapshot = _snapshot()
    cycle = json.loads((FIXTURES / "invalid" / "schedulable-cycle.json").read_text())
    snapshot["schedulable_edges"] = [
        {**edge, "record_type": "schedulable_edge", "schema_version": "1.0.0", "derived_from": ["EVD-dag-001"]}
        for edge in cycle["edges"]
    ]
    assert ErrorCode.SCHEDULABLE_CYCLE in {error.code for error in validate_snapshot(snapshot)}

    snapshot = _snapshot()
    relations = json.loads((FIXTURES / "valid" / "non-scheduling-cycle.json").read_text())
    snapshot["relations"] = [
        {
            **relation,
            "record_type": "relation",
            "relation_id": f"REL-dag-{index:03d}",
            "schema_version": "1.0.0",
        }
        for index, relation in enumerate(relations["relations"], 1)
    ]
    assert ErrorCode.SCHEDULABLE_CYCLE not in {error.code for error in validate_snapshot(snapshot)}


def test_dangling_node_and_evidence_references_are_typed():
    snapshot = _snapshot()
    snapshot["schedulable_edges"][0]["target"] = "EXN-dag-999"
    fixture = json.loads((FIXTURES / "invalid" / "dangling-evidence.json").read_text())
    snapshot["schedulable_edges"][0]["derived_from"] = [fixture["patch"]["value"]]
    codes = {error.code for error in validate_snapshot(snapshot)}
    assert {ErrorCode.DANGLING_NODE_REFERENCE, ErrorCode.DANGLING_EVIDENCE_REFERENCE} <= codes


def _envelope(
    operation: dict,
    *,
    authority: str = "automatic",
    mutation_id: str = "MUT-dag-001",
    idempotency_key: str = "idem:dag-001",
) -> dict:
    operation = deepcopy(operation)
    samples = _snapshot()
    operation_type = operation["operation_type"]
    values = {
        "add_intent_node": samples["intent_nodes"][0],
        "add_intent_edge": samples["intent_edges"][0],
        "add_execution_node": samples["execution_nodes"][0],
        "add_schedulable_edge": samples["schedulable_edges"][0],
        "record_evidence": samples["evidence_records"][0],
        "approve_mutation": samples["approval_records"][0],
        "acquire_lease": samples["lease_records"][0],
        "set_control": samples["control_records"][0],
        "promote_candidate": {"candidate_group_id": "CND-dag-001", "execution_node_id": "EXN-dag-002"},
    }
    if operation_type in values and "value" not in operation:
        operation["value"] = values[operation_type]
    if operation_type == "compensate":
        operation.setdefault("compensates_mutation_id", "MUT-dag-000")
        operation.setdefault("replacement_ref", "EXN-dag-002")
    return {
        "actor": "actor:collector",
        "authority_class": authority,
        "base_revision": 1,
        "budget_delta": {"unit": "tokens", "value": 0},
        "evidence_refs": ["EVD-dag-001"],
        "expected_effect": "graph_revision:+1",
        "graph_id": "GRF-dag-001",
        "idempotency_key": idempotency_key,
        "mutation_id": mutation_id,
        "operations": [operation],
        "reason_code": "EV_HUMAN_DECISION" if authority == "human" else "EV_DEP_DISCOVERED",
        "record_type": "mutation_envelope",
        "requires_approval": authority == "human",
        "schema_version": "1.0.0",
    }


@pytest.mark.parametrize(
    "operation_type",
    [row["operation_type"] for row in load_authority_matrix()["operations"] if row["approval_required"]],
)
def test_each_approval_required_operation_rejects_automatic_authority(operation_type: str):
    operation = {"op_id": "OPR-dag-001", "operation_type": operation_type, "target_ref": "EXN-dag-002"}
    errors = validate_mutation(_snapshot(), _envelope(operation))
    assert ErrorCode.AUTHORITY_APPROVAL_REQUIRED in {error.code for error in errors}


def test_representative_automatic_authority_fixture_is_rejected():
    fixture = json.loads((FIXTURES / "invalid" / "automatic-approval.json").read_text())
    errors = validate_mutation(_snapshot(), _envelope(fixture["operation"]))
    assert ErrorCode(fixture["expected_code"]) in {error.code for error in errors}


def test_terminal_mutation_is_invalid_and_compensating_event_is_valid():
    invalid = json.loads((FIXTURES / "invalid" / "terminal-mutation.json").read_text())
    terminal_transition = invalid["operation"]
    errors = validate_mutation(_snapshot(), _envelope(terminal_transition))
    assert ErrorCode(invalid["expected_code"]) in {error.code for error in errors}

    prior = _envelope({"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": "EVD-dag-001"}, mutation_id="MUT-dag-000")
    valid = json.loads((FIXTURES / "valid" / "compensating-event.json").read_text())
    compensation = valid["operation"]
    envelope = _envelope(compensation, authority="human", idempotency_key="idem:dag-compensation")
    assert validate_mutation(_snapshot(), envelope, history=[prior]) == ()


def test_duplicate_idempotency_key_with_different_payload_is_invalid():
    fixture = json.loads((FIXTURES / "invalid" / "idempotency-divergence.json").read_text())
    prior = _envelope(
        {"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": fixture["prior_target"]},
        idempotency_key=fixture["idempotency_key"],
    )
    incoming = deepcopy(prior)
    incoming["mutation_id"] = "MUT-dag-002"
    incoming["operations"][0]["target_ref"] = fixture["incoming_target"]
    errors = validate_mutation(_snapshot(), incoming, history=[prior])
    assert ErrorCode(fixture["expected_code"]) in {error.code for error in errors}


def test_attempt_outcome_and_candidate_disposition_are_orthogonal():
    """@cw-trace verifies INV-dag-011"""
    snapshot = _snapshot()
    snapshot["execution_nodes"][0]["candidate"] = {"disposition": "superseded", "group_id": "CND-dag-001"}
    assert snapshot["execution_nodes"][0]["attempt"]["outcome"] == "succeeded"
    assert validate_snapshot(snapshot) == ()


def test_canonical_encoding_is_nfc_integer_only_sorted_and_lf_terminated():
    """@cw-trace verifies INV-dag-004"""
    record = {"schema_version": "1.0.0", "record_type": "evidence_record", "label": "e\u0301", "count": 1}
    encoded = canonical_json_bytes(record)
    assert encoded == '{"count":1,"label":"é","record_type":"evidence_record","schema_version":"1.0.0"}\n'.encode()
    assert validate_canonical_bytes(encoded) == ()
    cases = json.loads((FIXTURES / "canonical-cases.json").read_text())
    for case in cases["invalid"]:
        codes = {error.code for error in validate_canonical_bytes(bytes.fromhex(case["hex"]))}
        assert ErrorCode(case["expected_code"]) in codes, case["name"]


def test_malformed_snapshot_and_operation_return_typed_errors_without_crashing():
    snapshot = _snapshot()
    snapshot["execution_nodes"] = [None]
    assert validate_snapshot(snapshot)[0].code is ErrorCode.SCHEMA_INVALID

    envelope = _envelope({"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": "EVD-dag-001"})
    envelope["operations"] = [None]
    assert validate_mutation(_snapshot(), envelope)[0].code is ErrorCode.SCHEMA_INVALID


def test_mutation_must_match_graph_and_current_base_revision():
    envelope = _envelope({"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": "EVD-dag-001"})
    envelope["graph_id"] = "GRF-other"
    envelope["base_revision"] = 999
    codes = {error.code for error in validate_mutation(_snapshot(), envelope)}
    assert {ErrorCode.GRAPH_ID_MISMATCH, ErrorCode.BASE_REVISION_MISMATCH} <= codes


def test_operation_payload_is_discriminated_and_schema_validated():
    bad_node = deepcopy(_snapshot()["execution_nodes"][1])
    bad_node["lifecycle_state"] = "banana"
    envelope = _envelope(
        {"op_id": "OPR-dag-001", "operation_type": "add_execution_node", "target_ref": "GRF-dag-001", "value": bad_node}
    )
    assert ErrorCode.SCHEMA_INVALID in {error.code for error in validate_mutation(_snapshot(), envelope)}


def test_snapshot_rejects_duplicate_ids_mixed_graphs_and_inconsistent_compilation():
    snapshot = _snapshot()
    duplicate = deepcopy(snapshot["execution_nodes"][0])
    duplicate["lifecycle_state"] = "failed"
    snapshot["execution_nodes"].append(duplicate)
    snapshot["control_records"][0]["graph_id"] = "GRF-other"
    snapshot["execution_nodes"][1]["compiled_from"]["intent_node_id"] = "INN-dag-001"
    codes = {error.code for error in validate_snapshot(snapshot)}
    assert {
        ErrorCode.DUPLICATE_RECORD_ID,
        ErrorCode.GRAPH_ID_MISMATCH,
        ErrorCode.COMPILATION_REFERENCE_INVALID,
    } <= codes


def test_large_acyclic_graph_does_not_depend_on_python_recursion_limit():
    snapshot = _snapshot()
    snapshot["intent_edges"] = []
    snapshot["execution_nodes"] = []
    snapshot["intent_nodes"] = []
    snapshot["schedulable_edges"] = []
    snapshot["lease_records"] = []
    for index in range(1, 1201):
        intent_id = f"INN-scale-{index:04d}"
        execution_id = f"EXN-scale-{index:04d}"
        intent = deepcopy(_snapshot()["intent_nodes"][0])
        intent["intent_node_id"] = intent_id
        execution = deepcopy(_snapshot()["execution_nodes"][1])
        execution["execution_node_id"] = execution_id
        execution["intent_node_id"] = intent_id
        execution["compiled_from"]["intent_node_id"] = intent_id
        snapshot["intent_nodes"].append(intent)
        snapshot["execution_nodes"].append(execution)
        if index > 1:
            snapshot["schedulable_edges"].append(
                {
                    "derived_from": ["EVD-dag-001"],
                    "edge_id": f"SED-scale-{index:04d}",
                    "kind": "depends_on",
                    "record_type": "schedulable_edge",
                    "schema_version": "1.0.0",
                    "source": execution_id,
                    "target": f"EXN-scale-{index - 1:04d}",
                }
            )
    assert validate_snapshot(snapshot) == ()

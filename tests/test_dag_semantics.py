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
    snapshot["schedulable_edges"][0]["derived_from"] = ["EVD-dag-999"]
    codes = {error.code for error in validate_snapshot(snapshot)}
    assert {ErrorCode.DANGLING_NODE_REFERENCE, ErrorCode.DANGLING_EVIDENCE_REFERENCE} <= codes


def _envelope(operation: dict, *, authority: str = "automatic", mutation_id: str = "MUT-dag-001") -> dict:
    return {
        "actor": "actor:collector",
        "authority_class": authority,
        "base_revision": 1,
        "budget_delta": {"unit": "tokens", "value": 0},
        "evidence_refs": ["EVD-dag-001"],
        "expected_effect": "graph_revision:+1",
        "graph_id": "GRF-dag-001",
        "idempotency_key": "idem:dag-001",
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


def test_terminal_mutation_is_invalid_and_compensating_event_is_valid():
    terminal_transition = {
        "from_state": "succeeded",
        "op_id": "OPR-dag-001",
        "operation_type": "transition_node",
        "target_ref": "EXN-dag-001",
        "to_state": "ready",
    }
    errors = validate_mutation(_snapshot(), _envelope(terminal_transition))
    assert ErrorCode.TERMINAL_STATE_IMMUTABLE in {error.code for error in errors}

    prior = _envelope({"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": "EVD-dag-001"}, mutation_id="MUT-dag-000")
    compensation = {
        "compensates_mutation_id": "MUT-dag-000",
        "op_id": "OPR-dag-002",
        "operation_type": "compensate",
        "replacement_ref": "EXN-dag-002",
        "target_ref": "EXN-dag-001",
    }
    assert validate_mutation(_snapshot(), _envelope(compensation, authority="human"), history=[prior]) == ()


def test_duplicate_idempotency_key_with_different_payload_is_invalid():
    prior = _envelope({"op_id": "OPR-dag-001", "operation_type": "record_evidence", "target_ref": "EVD-dag-001"})
    incoming = deepcopy(prior)
    incoming["mutation_id"] = "MUT-dag-002"
    incoming["operations"][0]["target_ref"] = "EVD-dag-999"
    errors = validate_mutation(_snapshot(), incoming, history=[prior])
    assert ErrorCode.IDEMPOTENCY_KEY_DIVERGENT in {error.code for error in errors}


def test_attempt_outcome_and_candidate_disposition_are_orthogonal():
    snapshot = _snapshot()
    snapshot["execution_nodes"][0]["candidate"] = {"disposition": "superseded", "group_id": "CND-dag-001"}
    assert snapshot["execution_nodes"][0]["attempt"]["outcome"] == "succeeded"
    assert validate_snapshot(snapshot) == ()


def test_canonical_encoding_is_nfc_integer_only_sorted_and_lf_terminated():
    record = {"schema_version": "1.0.0", "record_type": "evidence_record", "label": "e\u0301", "count": 1}
    encoded = canonical_json_bytes(record)
    assert encoded == '{"count":1,"label":"é","record_type":"evidence_record","schema_version":"1.0.0"}\n'.encode()
    assert validate_canonical_bytes(encoded) == ()
    assert ErrorCode.CANONICAL_ENCODING_VIOLATION in {error.code for error in validate_canonical_bytes(b'{"b":1,"a":2}\r\n')}
    assert ErrorCode.CANONICAL_ENCODING_VIOLATION in {error.code for error in validate_canonical_bytes(b'{"a":1.5}\n')}
    assert ErrorCode.DUPLICATE_JSON_KEY in {error.code for error in validate_canonical_bytes(b'{"a":1,"a":1}\n')}

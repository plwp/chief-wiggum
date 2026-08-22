"""Proposers: evidence in, mutation envelopes out.

Deterministic proposers come first because most replanning needs no model. A
failing gate, a proven path overlap, an expired lease and a rate-limited
provider all map to a mutation by rule.

The model path exists for genuinely ambiguous cases and produces the SAME
envelope, built field by field from an allowlist. Nothing a model emits is ever
executed, evaluated, or used to select a code path: unknown keys are dropped,
typed fields are range-checked, prose is carried as inert data, and `actor` and
`authority_class` are set by this module rather than read from the model.

@cw-trace guards INV-dag-006
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .evidence import DEGRADED_STATES, EvidenceClass, Observation
from .schemas import SCHEMA_VERSION, load_authority_matrix

# One reason code per evidence class, from #384's closed vocabulary.
REASON_CODES: dict[EvidenceClass, str] = {
    EvidenceClass.DEPENDENCY_READINESS: "EV_DEP_DECLARED",
    EvidenceClass.UNRESOLVED_MARKER: "EV_DEP_DISCOVERED",
    EvidenceClass.PATH_OVERLAP: "EV_PATH_CONFLICT",
    EvidenceClass.GATE_OUTCOME: "EV_GATE_FAILED",
    EvidenceClass.PROVIDER_HEALTH: "EV_PROVIDER_UNAVAILABLE",
    EvidenceClass.WORKER_LEASE: "EV_LEASE_EXPIRED",
    EvidenceClass.BUDGET_CONSUMPTION: "EV_BUDGET_EXCEEDED",
    EvidenceClass.HUMAN_DECISION: "EV_HUMAN_DECISION",
}

# Envelope keys a model may influence at all. Everything else is set here.
_MODEL_ALLOWED_KEYS = frozenset({"operations", "expected_effect", "evidence_refs", "budget_delta"})
# Operation keys a model may influence. `value` is allowed but never executed;
# it is validated against the record schemas by the engine like any other input.
_MODEL_ALLOWED_OP_KEYS = frozenset(
    {"op_id", "operation_type", "target_ref", "from_state", "to_state", "value",
     "replacement_ref", "compensates_mutation_id"}
)
_MAX_PROSE = 4096


def _envelope(
    *,
    graph_id: str,
    base_revision: int,
    mutation_id: str,
    idempotency_key: str,
    actor: str,
    authority_class: str,
    operations: Sequence[Mapping[str, Any]],
    reason_code: str,
    evidence_refs: Sequence[str],
    expected_effect: str,
    budget_delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    matrix = {row["operation_type"]: row for row in load_authority_matrix()["operations"]}
    requires_approval = any(
        matrix.get(str(op.get("operation_type")), {}).get("approval_required")
        for op in operations
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "mutation_envelope",
        "graph_id": graph_id,
        "base_revision": base_revision,
        "mutation_id": mutation_id,
        "idempotency_key": idempotency_key,
        "actor": actor,
        "authority_class": authority_class,
        "operations": [dict(op) for op in operations],
        "reason_code": reason_code,
        "evidence_refs": list(evidence_refs),
        "expected_effect": expected_effect[:_MAX_PROSE] or "no effect described",
        "budget_delta": dict(budget_delta or {"unit": "tokens", "value": 0}),
        "requires_approval": requires_approval,
    }


def _sequence_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:03d}"


def evidence_envelope(
    observations: Sequence[Observation],
    *,
    graph_id: str,
    base_revision: int,
    sequence: int = 1,
    actor: str = "actor:collector",
) -> dict[str, Any] | None:
    """One envelope recording every collected evidence record.

    Degraded records are recorded too. A source that could not be read leaves a
    durable trace in the graph rather than vanishing.
    """
    records = [record for observation in observations for record in observation.records]
    if not records:
        return None
    operations = [
        {
            "op_id": _sequence_id("OPS-evidence", index),
            "operation_type": "record_evidence",
            "target_ref": record["evidence_id"],
            "value": record,
        }
        for index, record in enumerate(records, start=1)
    ]
    degraded = sum(1 for record in records if record["status"] in DEGRADED_STATES)
    return _envelope(
        graph_id=graph_id,
        base_revision=base_revision,
        mutation_id=_sequence_id("MUT-evidence", sequence),
        idempotency_key=f"evidence-{graph_id}-{sequence}",
        actor=actor,
        authority_class="automatic",
        operations=operations,
        reason_code="EV_DEP_DISCOVERED",
        evidence_refs=[],
        expected_effect=f"records {len(records)} evidence records ({degraded} degraded)",
    )


def _blocked_transition(node_id: str, current_state: str, op_id: str) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "operation_type": "transition_node",
        "target_ref": node_id,
        "from_state": current_state,
        "to_state": "blocked",
    }


def propose_path_serialisation(
    observation: Observation,
    *,
    graph_id: str,
    base_revision: int,
    evidence_index: Mapping[str, str] | None = None,
    sequence: int = 1,
) -> list[dict[str, Any]]:
    """A proven overlap becomes an ordering edge, not a hope.

    Direction follows docs/dag-contract.md: `source` runs AFTER `target`, so the
    later node sources the edge.
    """
    envelopes = []
    for index, record in enumerate(observation.records, start=1):
        if record["status"] in DEGRADED_STATES:
            continue
        nodes = sorted((evidence_index or {}).get(record["evidence_id"], "").split(",")) or []
        nodes = [node for node in nodes if node]
        if len(nodes) < 2:
            continue
        later, earlier = nodes[1], nodes[0]
        operations = [
            {
                "op_id": _sequence_id("OPS-overlap", index),
                "operation_type": "add_schedulable_edge",
                "target_ref": f"SED-overlap-{index:03d}",
                "value": {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "schedulable_edge",
                    "edge_id": f"SED-overlap-{index:03d}",
                    "source": later,
                    "target": earlier,
                    "kind": "ordered_after",
                    "derived_from": [record["evidence_id"]],
                },
            }
        ]
        envelopes.append(
            _envelope(
                graph_id=graph_id,
                base_revision=base_revision,
                mutation_id=_sequence_id("MUT-overlap", sequence + index - 1),
                idempotency_key=f"overlap-{record['evidence_id']}",
                actor="actor:proposer",
                authority_class="automatic",
                operations=operations,
                reason_code=REASON_CODES[EvidenceClass.PATH_OVERLAP],
                evidence_refs=[record["evidence_id"]],
                expected_effect="serialises two nodes that write the same path",
            )
        )
    return envelopes


def propose_block_on_failure(
    observation: Observation,
    *,
    graph_id: str,
    base_revision: int,
    node_states: Mapping[str, str],
    subject_of: Mapping[str, str],
    sequence: int = 1,
) -> list[dict[str, Any]]:
    """A failing gate, an expired lease or an unresolved marker blocks its node."""
    reason_code = REASON_CODES[observation.evidence_class]
    envelopes = []
    for index, record in enumerate(observation.records, start=1):
        if record["status"] in DEGRADED_STATES:
            continue
        node_id = subject_of.get(record["evidence_id"], "")
        current = node_states.get(node_id)
        if not node_id or current is None or current == "blocked":
            continue
        envelopes.append(
            _envelope(
                graph_id=graph_id,
                base_revision=base_revision,
                mutation_id=_sequence_id("MUT-block", sequence + index - 1),
                idempotency_key=f"block-{record['evidence_id']}",
                actor="actor:proposer",
                authority_class="automatic",
                operations=[
                    _blocked_transition(node_id, current, _sequence_id("OPS-block", index))
                ],
                reason_code=reason_code,
                evidence_refs=[record["evidence_id"]],
                expected_effect=f"blocks {node_id} on {observation.evidence_class}",
            )
        )
    return envelopes


def propose_pause_on_budget(
    observation: Observation,
    *,
    graph_id: str,
    base_revision: int,
    sequence: int = 1,
) -> list[dict[str, Any]]:
    """Budget exhaustion asks to pause the graph. set_control is approval-required,
    so this is a request for a human, never something a rule performs."""
    envelopes = []
    for index, record in enumerate(observation.records, start=1):
        if record["status"] in DEGRADED_STATES:
            continue
        control = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "control_record",
            "control_id": f"CTL-budget-{index:03d}",
            "graph_id": graph_id,
            "state": "pause_requested",
            "actor": "actor:proposer",
            "reason_code": REASON_CODES[EvidenceClass.BUDGET_CONSUMPTION],
        }
        envelopes.append(
            _envelope(
                graph_id=graph_id,
                base_revision=base_revision,
                mutation_id=_sequence_id("MUT-budget", sequence + index - 1),
                idempotency_key=f"budget-{record['evidence_id']}",
                actor="actor:proposer",
                authority_class="automatic",
                operations=[
                    {
                        "op_id": _sequence_id("OPS-budget", index),
                        "operation_type": "set_control",
                        "target_ref": control["control_id"],
                        "value": control,
                    }
                ],
                reason_code=REASON_CODES[EvidenceClass.BUDGET_CONSUMPTION],
                evidence_refs=[record["evidence_id"]],
                expected_effect="requests a pause because the budget envelope is spent",
            )
        )
    return envelopes


# ------------------------------------------------------------- model path


class ModelProposalRejected(ValueError):
    """The model output could not be reduced to a well-formed envelope."""


def parse_model_proposal(
    raw: Any,
    *,
    model_name: str,
    graph_id: str,
    base_revision: int,
    mutation_id: str,
    idempotency_key: str,
    evidence_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Reduce untrusted model output to an envelope, treating it purely as data.

    Every field is either copied under an allowlist with a type check, or set by
    this function. `actor` and `authority_class` are NOT read from the model, so
    a model cannot widen its own authority by asserting anything. Prose is
    truncated and carried inert.
    """
    if not isinstance(raw, Mapping):
        raise ModelProposalRejected("model proposal must be a JSON object")

    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, Sequence) or isinstance(operations_raw, (str, bytes)):
        raise ModelProposalRejected("model proposal must carry an operations array")
    if not operations_raw:
        raise ModelProposalRejected("model proposal carries no operations")

    operations: list[dict[str, Any]] = []
    for index, operation in enumerate(operations_raw, start=1):
        if not isinstance(operation, Mapping):
            raise ModelProposalRejected(f"operation {index} is not an object")
        cleaned = {
            key: value
            for key, value in operation.items()
            if key in _MODEL_ALLOWED_OP_KEYS
        }
        if "operation_type" not in cleaned:
            raise ModelProposalRejected(f"operation {index} has no operation_type")
        cleaned.setdefault("op_id", _sequence_id("OPS-model", index))
        cleaned.setdefault("target_ref", "")
        operations.append(cleaned)

    prose = raw.get("expected_effect")
    expected_effect = prose if isinstance(prose, str) else "model proposal"

    delta_raw = raw.get("budget_delta")
    budget_delta: dict[str, Any] = {"unit": "tokens", "value": 0}
    if isinstance(delta_raw, Mapping):
        unit = delta_raw.get("unit")
        value = delta_raw.get("value")
        if isinstance(unit, str) and isinstance(value, int) and not isinstance(value, bool):
            budget_delta = {"unit": unit, "value": value}

    refs = list(evidence_refs)
    model_refs = raw.get("evidence_refs")
    if isinstance(model_refs, Sequence) and not isinstance(model_refs, (str, bytes)):
        # A model may only CITE evidence. Admission resolves every id against
        # the journal, so citing something invented is a rejection, not a hole.
        refs.extend(str(ref) for ref in model_refs if isinstance(ref, str))

    return _envelope(
        graph_id=graph_id,
        base_revision=base_revision,
        mutation_id=mutation_id,
        idempotency_key=idempotency_key,
        actor=f"actor:model.{model_name}",
        authority_class="automatic",
        operations=operations,
        reason_code="EV_DEP_DISCOVERED",
        evidence_refs=sorted(set(refs)),
        expected_effect=expected_effect,
        budget_delta=budget_delta,
    )


def model_influenced_fields() -> frozenset[str]:
    """The complete set of envelope keys a model can affect. Asserted by tests."""
    return _MODEL_ALLOWED_KEYS

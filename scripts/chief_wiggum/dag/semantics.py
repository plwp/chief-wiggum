"""Pure semantic validation for DAG records; no persistence or scheduling."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_json_bytes
from .errors import ContractViolation, ErrorCode
from .schemas import load_authority_matrix, validate_record

STATES = ("proposed", "admitted", "ready", "running", "succeeded", "failed", "blocked", "superseded", "cancelled")
TERMINAL_STATES = frozenset({"succeeded", "failed", "superseded", "cancelled"})
LEGAL_TRANSITIONS = frozenset(
    {
        ("proposed", "admitted"),
        ("admitted", "ready"),
        ("ready", "running"),
        ("ready", "blocked"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "blocked"),
        ("running", "superseded"),
        ("running", "cancelled"),
        ("blocked", "ready"),
    }
)


def validate_transition(before: str, after: str) -> tuple[ContractViolation, ...]:
    if (before, after) in LEGAL_TRANSITIONS:
        return ()
    if before in TERMINAL_STATES:
        return (
            ContractViolation(
                ErrorCode.TERMINAL_STATE_IMMUTABLE,
                f"terminal state {before!r} cannot transition to {after!r}",
                "/to_state",
            ),
        )
    return (
        ContractViolation(
            ErrorCode.STATE_TRANSITION_INVALID,
            f"illegal lifecycle transition {before!r} -> {after!r}",
            "/to_state",
        ),
    )


def _cycle(edges: Sequence[Mapping[str, Any]]) -> list[str] | None:
    adjacency: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        adjacency.setdefault(source, []).append(target)
        nodes.update((source, target))
    white, gray, black = 0, 1, 2
    colors = {node: white for node in nodes}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colors[node] = gray
        stack.append(node)
        for target in sorted(adjacency.get(node, [])):
            if colors[target] == gray:
                start = stack.index(target)
                return stack[start:] + [target]
            if colors[target] == white:
                found = visit(target)
                if found:
                    return found
        stack.pop()
        colors[node] = black
        return None

    for node in sorted(nodes):
        if colors[node] == white and (found := visit(node)):
            return found
    return None


def validate_snapshot(snapshot: Mapping[str, Any]) -> tuple[ContractViolation, ...]:
    errors = list(validate_record(snapshot, "graph_snapshot"))
    execution_ids = {node.get("execution_node_id") for node in snapshot.get("execution_nodes", [])}
    intent_ids = {node.get("intent_node_id") for node in snapshot.get("intent_nodes", [])}
    evidence_ids = {record.get("evidence_id") for record in snapshot.get("evidence_records", [])}

    for index, edge in enumerate(snapshot.get("intent_edges", [])):
        for endpoint in ("source", "target"):
            if edge.get(endpoint) not in intent_ids:
                errors.append(ContractViolation(ErrorCode.DANGLING_NODE_REFERENCE, f"intent edge {endpoint} does not exist", f"/intent_edges/{index}/{endpoint}"))
    for index, edge in enumerate(snapshot.get("schedulable_edges", [])):
        for endpoint in ("source", "target"):
            if edge.get(endpoint) not in execution_ids:
                errors.append(ContractViolation(ErrorCode.DANGLING_NODE_REFERENCE, f"schedulable edge {endpoint} does not exist", f"/schedulable_edges/{index}/{endpoint}"))
        for evidence_index, evidence_ref in enumerate(edge.get("derived_from", [])):
            if evidence_ref not in evidence_ids:
                errors.append(ContractViolation(ErrorCode.DANGLING_EVIDENCE_REFERENCE, "schedulable edge evidence does not exist", f"/schedulable_edges/{index}/derived_from/{evidence_index}"))
    for index, relation in enumerate(snapshot.get("relations", [])):
        for endpoint in ("source", "target"):
            if relation.get(endpoint) not in execution_ids:
                errors.append(ContractViolation(ErrorCode.DANGLING_NODE_REFERENCE, f"relation {endpoint} does not exist", f"/relations/{index}/{endpoint}"))

    if cycle := _cycle(snapshot.get("schedulable_edges", [])):
        errors.append(ContractViolation(ErrorCode.SCHEDULABLE_CYCLE, "schedulable subgraph contains a cycle", "/schedulable_edges", details={"cycle": cycle}))
    return tuple(sorted(errors, key=lambda error: (error.path, error.code.value)))


def _digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(record)).hexdigest()


def validate_mutation(
    snapshot: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    history: Sequence[Mapping[str, Any]] = (),
) -> tuple[ContractViolation, ...]:
    errors = list(validate_record(envelope, "mutation_envelope"))
    matrix = {row["operation_type"]: row for row in load_authority_matrix()["operations"]}
    evidence_ids = {record.get("evidence_id") for record in snapshot.get("evidence_records", [])}
    node_by_id = {node.get("execution_node_id"): node for node in snapshot.get("execution_nodes", [])}
    history_by_id = {item.get("mutation_id"): item for item in history}

    for prior in history:
        if prior.get("graph_id") == envelope.get("graph_id") and prior.get("idempotency_key") == envelope.get("idempotency_key") and _digest(prior) != _digest(envelope):
            errors.append(ContractViolation(ErrorCode.IDEMPOTENCY_KEY_DIVERGENT, "idempotency key was previously used with a different canonical envelope", "/idempotency_key"))
            break

    for index, evidence_ref in enumerate(envelope.get("evidence_refs", [])):
        if evidence_ref not in evidence_ids:
            errors.append(ContractViolation(ErrorCode.DANGLING_EVIDENCE_REFERENCE, "mutation evidence does not exist", f"/evidence_refs/{index}"))

    approval_required = False
    for index, operation in enumerate(envelope.get("operations", [])):
        operation_type = operation.get("operation_type")
        row = matrix.get(operation_type)
        if row and row["approval_required"]:
            approval_required = True
            if envelope.get("authority_class") == "automatic":
                errors.append(ContractViolation(ErrorCode.AUTHORITY_APPROVAL_REQUIRED, f"{operation_type} requires human approval", f"/operations/{index}/operation_type"))

        if operation_type == "transition_node":
            node = node_by_id.get(operation.get("target_ref"))
            if node is None:
                errors.append(ContractViolation(ErrorCode.DANGLING_NODE_REFERENCE, "transition target does not exist", f"/operations/{index}/target_ref"))
                continue
            before = operation.get("from_state")
            after = operation.get("to_state")
            if before != node.get("lifecycle_state"):
                errors.append(ContractViolation(ErrorCode.STATE_TRANSITION_INVALID, "from_state does not match current node lifecycle", f"/operations/{index}/from_state"))
            errors.extend(validate_transition(str(before), str(after)))
            if after == "ready":
                errors.append(ContractViolation(ErrorCode.READINESS_DERIVED_NOT_MUTABLE, "ready is a derived projection and cannot be set by mutation", f"/operations/{index}/to_state"))
        elif operation_type == "compensate":
            prior_id = operation.get("compensates_mutation_id")
            target = node_by_id.get(operation.get("target_ref"))
            replacement = node_by_id.get(operation.get("replacement_ref"))
            if prior_id not in history_by_id:
                errors.append(ContractViolation(ErrorCode.COMPENSATION_TARGET_MISSING, "compensated mutation does not exist in prior history", f"/operations/{index}/compensates_mutation_id"))
            if target is None or target.get("lifecycle_state") not in TERMINAL_STATES or replacement is None or replacement is target:
                errors.append(ContractViolation(ErrorCode.COMPENSATION_TARGET_INVALID, "compensation must preserve a terminal target and name a distinct existing replacement", f"/operations/{index}"))

    if bool(envelope.get("requires_approval")) != approval_required:
        errors.append(ContractViolation(ErrorCode.REQUIRES_APPROVAL_MISMATCH, "requires_approval does not match the authority matrix", "/requires_approval"))
    return tuple(sorted(errors, key=lambda error: (error.path, error.code.value)))

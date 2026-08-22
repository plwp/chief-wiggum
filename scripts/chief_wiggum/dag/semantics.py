"""Pure semantic validation for DAG records; no persistence or scheduling."""

from __future__ import annotations

import hashlib
from collections import deque
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
    indegree: dict[str, int] = {}
    nodes: set[str] = set()
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        adjacency.setdefault(source, []).append(target)
        nodes.update((source, target))
        indegree.setdefault(source, 0)
        indegree[target] = indegree.get(target, 0) + 1
    ready = deque(sorted(node for node in nodes if indegree.get(node, 0) == 0))
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for target in sorted(adjacency.get(node, [])):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited == len(nodes):
        return None
    return sorted(node for node in nodes if indegree.get(node, 0) > 0)


def _duplicates(records: Sequence[Mapping[str, Any]], key: str) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for record in records:
        value = str(record[key])
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def validate_snapshot(snapshot: Mapping[str, Any]) -> tuple[ContractViolation, ...]:
    errors = list(validate_record(snapshot, "graph_snapshot"))
    if errors:
        return tuple(errors)
    execution_ids = {node.get("execution_node_id") for node in snapshot.get("execution_nodes", [])}
    intent_ids = {node.get("intent_node_id") for node in snapshot.get("intent_nodes", [])}
    evidence_ids = {record.get("evidence_id") for record in snapshot.get("evidence_records", [])}
    mutation_ids = {record.get("mutation_id") for record in snapshot.get("mutations", [])}
    graph_id = snapshot["graph_id"]

    identity_fields = {
        "intent_nodes": "intent_node_id",
        "intent_edges": "edge_id",
        "execution_nodes": "execution_node_id",
        "schedulable_edges": "edge_id",
        "relations": "relation_id",
        "evidence_records": "evidence_id",
        "approval_records": "approval_id",
        "lease_records": "lease_id",
        "control_records": "control_id",
        "mutations": "mutation_id",
    }
    for collection, key in identity_fields.items():
        for duplicate in sorted(_duplicates(snapshot[collection], key)):
            errors.append(ContractViolation(ErrorCode.DUPLICATE_RECORD_ID, f"duplicate {key} {duplicate}", f"/{collection}"))

    for index, node in enumerate(snapshot["execution_nodes"]):
        intent_node_id = node["intent_node_id"]
        if intent_node_id not in intent_ids or node["compiled_from"]["intent_node_id"] != intent_node_id:
            errors.append(ContractViolation(ErrorCode.COMPILATION_REFERENCE_INVALID, "execution node compilation reference is missing or inconsistent", f"/execution_nodes/{index}/compiled_from"))

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

    for index, control in enumerate(snapshot["control_records"]):
        if control["graph_id"] != graph_id:
            errors.append(ContractViolation(ErrorCode.GRAPH_ID_MISMATCH, "control record belongs to another graph", f"/control_records/{index}/graph_id"))
    for index, mutation in enumerate(snapshot["mutations"]):
        if mutation["graph_id"] != graph_id:
            errors.append(ContractViolation(ErrorCode.GRAPH_ID_MISMATCH, "mutation belongs to another graph", f"/mutations/{index}/graph_id"))
        if mutation["base_revision"] > snapshot["graph_revision"]:
            errors.append(ContractViolation(ErrorCode.BASE_REVISION_MISMATCH, "historical mutation base revision is newer than the snapshot", f"/mutations/{index}/base_revision"))
        for evidence_index, evidence_ref in enumerate(mutation["evidence_refs"]):
            if evidence_ref not in evidence_ids:
                errors.append(ContractViolation(ErrorCode.DANGLING_EVIDENCE_REFERENCE, "mutation evidence does not exist", f"/mutations/{index}/evidence_refs/{evidence_index}"))
    for index, approval in enumerate(snapshot["approval_records"]):
        if approval["mutation_id"] not in mutation_ids:
            errors.append(ContractViolation(ErrorCode.COMPENSATION_TARGET_MISSING, "approval mutation does not exist", f"/approval_records/{index}/mutation_id"))
    for index, lease in enumerate(snapshot["lease_records"]):
        node = next((item for item in snapshot["execution_nodes"] if item["execution_node_id"] == lease["execution_node_id"]), None)
        if node is None:
            errors.append(ContractViolation(ErrorCode.DANGLING_NODE_REFERENCE, "lease execution node does not exist", f"/lease_records/{index}/execution_node_id"))
        elif node["attempt"]["attempt_id"] != lease["attempt_id"]:
            errors.append(ContractViolation(ErrorCode.COMPILATION_REFERENCE_INVALID, "lease attempt does not match its execution node", f"/lease_records/{index}/attempt_id"))

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
    snapshot_errors = validate_snapshot(snapshot)
    if snapshot_errors:
        return snapshot_errors
    errors = list(validate_record(envelope, "mutation_envelope"))
    if errors:
        return tuple(errors)
    matrix = {row["operation_type"]: row for row in load_authority_matrix()["operations"]}
    evidence_ids = {record.get("evidence_id") for record in snapshot.get("evidence_records", [])}
    node_by_id = {node.get("execution_node_id"): node for node in snapshot.get("execution_nodes", [])}
    history_by_id = {item.get("mutation_id"): item for item in history}

    if envelope["graph_id"] != snapshot["graph_id"]:
        errors.append(ContractViolation(ErrorCode.GRAPH_ID_MISMATCH, "mutation belongs to another graph", "/graph_id"))
    if envelope["base_revision"] != snapshot["graph_revision"]:
        errors.append(ContractViolation(ErrorCode.BASE_REVISION_MISMATCH, "mutation base revision is not current", "/base_revision", details={"actual": envelope["base_revision"], "current": snapshot["graph_revision"]}))

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

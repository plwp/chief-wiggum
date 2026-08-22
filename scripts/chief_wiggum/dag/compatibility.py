"""One-way legacy dependency import and exact static wave projection."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from chief_wiggum import github, planning

from .schemas import validate_record


@dataclass(frozen=True)
class ProjectionResult:
    exit_code: int
    plan: dict[str, Any] | None = None
    error: str = ""


def dependency_block_to_intent_graph(
    description: str,
    *,
    graph_id: str,
    issues: Iterable[int],
    source_ref: str,
) -> dict[str, Any]:
    """Import pinned tracker intent one way. @cw-trace guards INV-dag-005"""
    metadata = github.parse_dependency_block(description)
    if not metadata.has_block:
        scan_status = "unscanned"
    elif any("malformed dependency line" in warning for warning in metadata.warnings):
        scan_status = "malformed"
    else:
        scan_status = "observed"
    in_scope = set(issues)
    tickets = in_scope | set(metadata.edges) | {dep for deps in metadata.edges.values() for dep in deps}
    nodes = [
        {
            "schema_version": "1.0.0",
            "record_type": "intent_node",
            "intent_node_id": f"INN-ticket-{ticket:03d}",
            "node_type": "implementation",
            "role": "role:implementer",
            "source_ref": f"ticket:#{ticket}",
            "source_ticket": ticket,
            "in_scope": ticket in in_scope,
        }
        for ticket in sorted(tickets)
    ]
    edges: list[dict[str, Any]] = []
    counter = 1
    for source in sorted(metadata.edges):
        for target in sorted(metadata.edges[source]):
            edges.append(
                {
                    "schema_version": "1.0.0",
                    "record_type": "intent_edge",
                    "edge_id": f"IED-ticket-{counter:03d}",
                    "source": f"INN-ticket-{source:03d}",
                    "target": f"INN-ticket-{target:03d}",
                    "source_ticket": source,
                    "target_ticket": target,
                    "kind": "depends_on",
                    "actor": "actor:tracker",
                    "reason_code": "EV_DEP_DECLARED",
                }
            )
            counter += 1
    return {
        "schema_version": "1.0.0",
        "record_type": "intent_graph",
        "graph_id": graph_id,
        "source_ref": source_ref,
        "source_digest": "sha256:" + hashlib.sha256(description.encode()).hexdigest(),
        "scan_status": scan_status,
        "has_dependency_block": metadata.has_block,
        "source_warnings": metadata.warnings,
        "nodes": nodes,
        "edges": edges,
    }


def project_legacy_waves(
    intent_graph: Mapping[str, Any],
    *,
    closed: Iterable[int] = (),
    gated: Iterable[int] = (),
) -> ProjectionResult:
    """Return the legacy oracle shape. @cw-trace guards CTR-dag-007 CTR-dag-008 INV-dag-006"""
    schema_errors = validate_record(intent_graph, "intent_graph")
    if schema_errors:
        return ProjectionResult(1, error="; ".join(error.message for error in schema_errors))
    warnings = list(intent_graph.get("source_warnings", []))
    if intent_graph["scan_status"] != "observed":
        detail = "; ".join(warnings) or "source was not observed"
        return ProjectionResult(1, error=f"intent scan status is {intent_graph['scan_status']}: {detail}")
    ticket_nodes = [node for node in intent_graph["nodes"] if node.get("in_scope", True)]
    if any(not isinstance(node.get("source_ticket"), int) for node in ticket_nodes):
        return ProjectionResult(1, error="intent graph does not define a ticket-level projection")
    if any(not isinstance(edge.get("source_ticket"), int) or not isinstance(edge.get("target_ticket"), int) for edge in intent_graph["edges"]):
        return ProjectionResult(1, error="intent edges do not define a ticket-level projection")
    issues = [node["source_ticket"] for node in ticket_nodes]
    edges: dict[int, list[int]] = {ticket: [] for ticket in issues}
    for edge in intent_graph.get("edges", []):
        edges.setdefault(edge["source_ticket"], []).append(edge["target_ticket"])
    try:
        plan = planning.plan_waves(issues, edges, closed=closed, gated=gated)
    except planning.DependencyCycleError as exc:
        return ProjectionResult(2, error=str(exc))
    plan.warnings = warnings + plan.warnings
    return ProjectionResult(0, plan=plan.to_dict())

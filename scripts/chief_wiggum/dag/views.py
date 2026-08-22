"""Live graph explainability, audit views, and operator controls.

Explanations are DERIVED, never narrated. Every answer is computed from the
graph and the journal and cites stable IDs (node, edge, evidence, mutation,
reason code). A model-written summary of a run is not an explanation; it is
another thing to verify. This is `code_query.py`'s doctrine: a locator, not a
content store.

Unscanned is not clean. A view that cannot resolve something says so in
`unresolved` and never renders absence of data as absence of a problem.

Exports are deterministic, so a diff between two exports is a real change and
not serialisation noise. Redaction is structural and applied on the way out,
because the views must not become the leak that the keyring policy prevents.

@cw-trace guards INV-dag-014 INV-dag-015
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .canonical import canonical_json_bytes

TERMINAL = frozenset({"succeeded", "failed", "superseded", "cancelled"})

# Credential-shaped strings. Allowlist-based redaction: anything matching is
# replaced, rather than trying to enumerate what is safe to print.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{8,}", re.IGNORECASE), "<redacted:key>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{16,}"), "<redacted:token>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), "<redacted:token>"),
    (re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
     "<redacted:jwt>"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), "<redacted:aws-key>"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[=:]\s*\S+"),
     r"\1=<redacted>"),
    (re.compile(r"/(?:Users|home)/[^/\s\"']+"), "<redacted:home>"),
)


class Verdict(StrEnum):
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    TERMINAL = "terminal"
    PENDING = "pending"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Reason:
    """One derived fact, carrying handles rather than prose."""

    code: str
    subject: str
    refs: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "subject": self.subject, "refs": list(self.refs),
                "detail": self.detail}


@dataclass(frozen=True)
class Explanation:
    subject: str
    verdict: Verdict
    reasons: tuple[Reason, ...] = ()
    unresolved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "verdict": str(self.verdict),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "unresolved": list(self.unresolved),
        }


def redact(value: Any) -> Any:
    """Strip credential-shaped strings and home paths from anything exported."""
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _REDACTIONS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(value, Mapping):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _nodes(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {node["execution_node_id"]: node for node in state.get("execution_nodes", [])}


def _predecessors(state: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """node -> [(predecessor, edge_id)]. source runs AFTER target."""
    result: dict[str, list[tuple[str, str]]] = {node: [] for node in _nodes(state)}
    for edge in state.get("schedulable_edges", []):
        source = str(edge.get("source"))
        if source in result:
            result[source].append((str(edge.get("target")), str(edge.get("edge_id"))))
    return result


def node_sets(state: Mapping[str, Any], *, running: Sequence[str] = ()) -> dict[str, Any]:
    """Ready / running / blocked / terminal, with counts."""
    nodes = _nodes(state)
    predecessors = _predecessors(state)
    running_set = set(running)
    buckets: dict[str, list[str]] = {"ready": [], "running": [], "blocked": [], "terminal": [],
                                     "pending": []}
    for node_id, node in sorted(nodes.items()):
        lifecycle = str(node.get("lifecycle_state"))
        if node_id in running_set:
            buckets["running"].append(node_id)
        elif lifecycle in TERMINAL:
            buckets["terminal"].append(node_id)
        elif lifecycle == "blocked":
            buckets["blocked"].append(node_id)
        elif lifecycle == "ready" and node.get("control_state") == "active":
            buckets["ready"].append(node_id)
        elif all(
            str(nodes.get(predecessor, {}).get("lifecycle_state")) == "succeeded"
            for predecessor, _ in predecessors.get(node_id, ())
        ):
            buckets["pending"].append(node_id)
        else:
            buckets["blocked"].append(node_id)
    return {
        "counts": {name: len(members) for name, members in buckets.items()},
        **{name: sorted(members) for name, members in buckets.items()},
    }


def conflict_set(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Which ordered_after edges are actively serialising work right now."""
    nodes = _nodes(state)
    conflicts = []
    for edge in state.get("schedulable_edges", []):
        if str(edge.get("kind")) != "ordered_after":
            continue
        target = nodes.get(str(edge.get("target")), {})
        if str(target.get("lifecycle_state")) not in TERMINAL:
            conflicts.append(
                {
                    "edge_id": str(edge.get("edge_id")),
                    "waiting": str(edge.get("source")),
                    "on": str(edge.get("target")),
                    "derived_from": list(edge.get("derived_from", [])),
                }
            )
    return sorted(conflicts, key=lambda entry: entry["edge_id"])


def why(state: Mapping[str, Any], node_id: str, *, running: Sequence[str] = ()) -> Explanation:
    """Why is this node ready, blocked, or neither? Derived, with handles."""
    nodes = _nodes(state)
    node = nodes.get(node_id)
    if node is None:
        # Unresolved is a real answer, and never renders as "nothing wrong".
        return Explanation(
            subject=node_id,
            verdict=Verdict.UNRESOLVED,
            unresolved=(f"{node_id} is not an execution node in this graph",),
        )

    lifecycle = str(node.get("lifecycle_state"))
    reasons: list[Reason] = []
    unresolved: list[str] = []

    for predecessor, edge_id in sorted(_predecessors(state).get(node_id, ())):
        upstream = nodes.get(predecessor)
        if upstream is None:
            unresolved.append(f"edge {edge_id} names {predecessor}, which is not in the graph")
            continue
        upstream_state = str(upstream.get("lifecycle_state"))
        if upstream_state == "succeeded":
            reasons.append(
                Reason("DEPENDENCY_SATISFIED", predecessor, (edge_id,), "predecessor succeeded")
            )
        else:
            reasons.append(
                Reason("DEPENDENCY_PENDING", predecessor, (edge_id,),
                       f"predecessor is {upstream_state}")
            )

    for record in state.get("control_records", []):
        if str(record.get("state")) in ("paused", "pause_requested", "cancelled",
                                        "cancel_requested"):
            reasons.append(
                Reason("GRAPH_CONTROL", str(record.get("control_id")), (),
                       f"graph control is {record.get('state')}")
            )

    if node.get("control_state") != "active":
        reasons.append(
            Reason("NODE_CONTROL", node_id, (), f"node control is {node.get('control_state')}")
        )

    if node_id in set(running):
        verdict = Verdict.RUNNING
    elif lifecycle in TERMINAL:
        verdict = Verdict.TERMINAL
        reasons.append(Reason("TERMINAL_STATE", node_id, (), lifecycle))
    elif lifecycle == "blocked":
        verdict = Verdict.BLOCKED
    elif lifecycle == "ready" and node.get("control_state") == "active":
        verdict = Verdict.READY
    elif any(reason.code == "DEPENDENCY_PENDING" for reason in reasons):
        verdict = Verdict.BLOCKED
    else:
        verdict = Verdict.PENDING

    return Explanation(node_id, verdict, tuple(reasons), tuple(unresolved))


def why_provider(decisions: Mapping[str, Mapping[str, Any]], node_id: str) -> Explanation:
    """Why this provider, from #388's recorded routing decision."""
    decision = decisions.get(node_id)
    if decision is None:
        return Explanation(
            subject=node_id,
            verdict=Verdict.UNRESOLVED,
            unresolved=(f"no routing decision was recorded for {node_id}",),
        )
    reasons = [
        Reason("ROUTED_TO", str(decision.get("provider")), (), "chosen provider"),
        Reason("ALTERNATIVES", node_id, tuple(str(a) for a in decision.get("alternatives", ()))),
    ]
    for factor in decision.get("factors", ()):
        reasons.append(Reason("DECIDING_FACTOR", node_id, (), str(factor)))
    if decision.get("trigger"):
        reasons.append(
            Reason("ESCALATION_TRIGGER", node_id,
                   tuple(str(e) for e in decision.get("evidence_ids", ())),
                   str(decision["trigger"]))
        )
    return Explanation(node_id, Verdict.READY, tuple(reasons))


def why_changed(state: Mapping[str, Any], mutation_id: str) -> Explanation:
    """Why did the graph change? The mutation, its actor, evidence, budget."""
    mutation = next(
        (m for m in state.get("mutations", []) if str(m.get("mutation_id")) == mutation_id), None
    )
    if mutation is None:
        return Explanation(
            subject=mutation_id,
            verdict=Verdict.UNRESOLVED,
            unresolved=(f"{mutation_id} is not an admitted mutation in this graph",),
        )
    delta = mutation.get("budget_delta") or {}
    reasons = [
        Reason("ACTOR", str(mutation.get("actor")), (), str(mutation.get("authority_class"))),
        Reason("REASON_CODE", mutation_id, (), str(mutation.get("reason_code"))),
        Reason("EVIDENCE", mutation_id, tuple(str(e) for e in mutation.get("evidence_refs", ()))),
        Reason("BUDGET_DELTA", mutation_id, (),
               f"{delta.get('value', 0)} {delta.get('unit', '')}".strip()),
        Reason("OPERATIONS", mutation_id,
               tuple(str(op.get("operation_type")) for op in mutation.get("operations", ()))),
    ]
    return Explanation(mutation_id, Verdict.TERMINAL, tuple(reasons))


def provenance(state: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    """From a node, walk back to the evidence that produced its edges and state."""
    nodes = _nodes(state)
    if node_id not in nodes:
        return {"subject": node_id, "unresolved": [f"{node_id} is not in this graph"],
                "evidence": [], "mutations": []}
    evidence_ids: set[str] = set()
    for edge in state.get("schedulable_edges", []):
        if str(edge.get("source")) == node_id or str(edge.get("target")) == node_id:
            evidence_ids.update(str(e) for e in edge.get("derived_from", ()))
    mutations = []
    for mutation in state.get("mutations", []):
        touches = any(
            str(op.get("target_ref")) == node_id for op in mutation.get("operations", ())
        )
        if touches:
            mutations.append(str(mutation.get("mutation_id")))
            evidence_ids.update(str(e) for e in mutation.get("evidence_refs", ()))
    known = {str(r.get("evidence_id")) for r in state.get("evidence_records", [])}
    return {
        "subject": node_id,
        "evidence": sorted(evidence_ids & known),
        "mutations": sorted(mutations),
        # An evidence id an edge cites but the graph does not hold is a gap that
        # must be visible, not silently dropped from the walk.
        "unresolved": sorted(f"evidence {ref} is cited but not recorded"
                             for ref in evidence_ids - known),
    }


def revision_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Structured operations between two graph revisions."""
    collections = ("intent_nodes", "intent_edges", "execution_nodes", "schedulable_edges",
                   "relations", "evidence_records", "approval_records", "lease_records",
                   "control_records")
    identity = {
        "intent_nodes": "intent_node_id", "intent_edges": "edge_id",
        "execution_nodes": "execution_node_id", "schedulable_edges": "edge_id",
        "relations": "relation_id", "evidence_records": "evidence_id",
        "approval_records": "approval_id", "lease_records": "lease_id",
        "control_records": "control_id",
    }
    diff: dict[str, Any] = {
        "from_revision": before.get("graph_revision", 0),
        "to_revision": after.get("graph_revision", 0),
        "added": {}, "removed": {}, "changed": {},
    }
    for collection in collections:
        key = identity[collection]
        old = {str(r.get(key)): r for r in before.get(collection, [])}
        new = {str(r.get(key)): r for r in after.get(collection, [])}
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(
            record_id for record_id in set(old) & set(new)
            if canonical_json_bytes(old[record_id]) != canonical_json_bytes(new[record_id])
        )
        if added:
            diff["added"][collection] = added
        if removed:
            diff["removed"][collection] = removed
        if changed:
            diff["changed"][collection] = changed
    before_mutations = {str(m.get("mutation_id")) for m in before.get("mutations", [])}
    diff["mutations"] = sorted(
        str(m.get("mutation_id")) for m in after.get("mutations", [])
        if str(m.get("mutation_id")) not in before_mutations
    )
    return diff


def export_json(state: Mapping[str, Any], *, running: Sequence[str] = ()) -> bytes:
    """Deterministic, redacted JSON export. Same revision, same bytes.

    Ordering is not imposed here: INV-dag-004's canonical encoding already
    sorts identified-record collections, so determinism holds for any input
    order without this function re-sorting.
    """
    payload = {
        "graph_id": state.get("graph_id", ""),
        "graph_revision": state.get("graph_revision", 0),
        "sets": node_sets(state, running=running),
        "conflicts": conflict_set(state),
        "nodes": [
            {
                "execution_node_id": node.get("execution_node_id"),
                "lifecycle_state": node.get("lifecycle_state"),
                "control_state": node.get("control_state"),
                "lease_state": node.get("lease_state"),
                "node_type": node.get("node_type"),
            }
            for node in state.get("execution_nodes", [])
        ],
        "edges": [
            {
                "edge_id": edge.get("edge_id"),
                "source": edge.get("source"),
                "target": edge.get("target"),
                "kind": edge.get("kind"),
            }
            for edge in state.get("schedulable_edges", [])
        ],
    }
    return canonical_json_bytes(redact(payload))


_MERMAID_SHAPE = {
    "ready": ("([", "])"),
    "running": ("[[", "]]"),
    "blocked": ("{{", "}}"),
    "terminal": ("[", "]"),
    "pending": ("(", ")"),
}


def export_mermaid(state: Mapping[str, Any], *, running: Sequence[str] = ()) -> str:
    """Deterministic Mermaid for issue and PR bodies, not a live dashboard."""
    sets = node_sets(state, running=running)
    bucket_of = {
        node_id: bucket
        for bucket in _MERMAID_SHAPE
        for node_id in sets.get(bucket, [])
    }
    lines = ["graph TD"]
    for node in sorted(state.get("execution_nodes", []),
                       key=lambda n: str(n.get("execution_node_id"))):
        node_id = str(node.get("execution_node_id"))
        open_shape, close_shape = _MERMAID_SHAPE.get(bucket_of.get(node_id, "pending"),
                                                     ("(", ")"))
        label = f"{node_id}<br/>{node.get('lifecycle_state')}"
        lines.append(f"    {node_id.replace('-', '_')}{open_shape}\"{label}\"{close_shape}")
    for edge in sorted(state.get("schedulable_edges", []), key=lambda e: str(e.get("edge_id"))):
        # source runs AFTER target, so the arrow points the way work flows.
        source = str(edge.get("target")).replace("-", "_")
        target = str(edge.get("source")).replace("-", "_")
        arrow = "-.->" if str(edge.get("kind")) == "ordered_after" else "-->"
        lines.append(f"    {source} {arrow} {target}")
    return redact("\n".join(lines) + "\n")


# ------------------------------------------------------------------ controls


CONTROL_STATES = {
    "pause": "pause_requested",
    "resume": "active",
    "cancel": "cancel_requested",
}


def control_envelope(
    action: str,
    *,
    graph_id: str,
    base_revision: int,
    operator: str,
    reason: str,
    mutation_id: str,
    idempotency_key: str,
    control_id: str,
) -> dict[str, Any]:
    """An operator control is a journaled human-actor mutation, not a side channel.

    There is no out-of-band control path: pause, resume and cancel all travel
    the same envelope as any other change, so they replay like any other change.
    """
    if action not in CONTROL_STATES:
        raise ValueError(f"unknown control action {action!r}")
    if not operator or operator.startswith("actor:model."):
        raise ValueError("operator controls require a human actor")
    return {
        "schema_version": "1.0.0",
        "record_type": "mutation_envelope",
        "graph_id": graph_id,
        "base_revision": base_revision,
        "mutation_id": mutation_id,
        "idempotency_key": idempotency_key,
        "actor": operator,
        "authority_class": "human",
        "operations": [
            {
                "op_id": "OPS-control-001",
                "operation_type": "set_control",
                "target_ref": control_id,
                "value": {
                    "schema_version": "1.0.0",
                    "record_type": "control_record",
                    "control_id": control_id,
                    "graph_id": graph_id,
                    "state": CONTROL_STATES[action],
                    "actor": operator,
                    "reason_code": "EV_HUMAN_DECISION",
                },
            }
        ],
        "reason_code": "EV_HUMAN_DECISION",
        "evidence_refs": [],
        "expected_effect": redact(f"operator {action}: {reason}")[:4096] or action,
        "budget_delta": {"unit": "tokens", "value": 0},
        "requires_approval": True,
    }

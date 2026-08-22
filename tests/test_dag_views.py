"""Explainability views, exports, and operator controls (chief-wiggum#390).

Three properties carry this file: explanations cite stable IDs rather than
narrating, an unresolvable question says so instead of rendering as clean, and
exports are byte-identical for the same revision so a diff is a real change.
"""

import pytest
from chief_wiggum.dag.views import (
    Verdict,
    conflict_set,
    control_envelope,
    export_json,
    export_mermaid,
    node_sets,
    provenance,
    redact,
    revision_diff,
    why,
    why_changed,
    why_provider,
)

GRAPH = "GRF-view001"


def execution_node(node_id, *, lifecycle="proposed", control="active", lease="unclaimed"):
    return {
        "schema_version": "1.0.0",
        "record_type": "execution_node",
        "execution_node_id": node_id,
        "intent_node_id": "INN-view-001",
        "node_type": "implementation",
        "role": "role:implementer",
        "lifecycle_state": lifecycle,
        "attempt": {"attempt_id": None, "outcome": "pending"},
        "candidate": {"group_id": None, "disposition": "pending"},
        "approval_state": "not_required",
        "lease_state": lease,
        "control_state": control,
        "compiled_from": {"intent_node_id": "INN-view-001",
                          "intent_graph_digest": "sha256:" + "b" * 64},
    }


def edge(edge_id, source, target, kind="depends_on", derived_from=("EVD-view-001",)):
    return {
        "schema_version": "1.0.0",
        "record_type": "schedulable_edge",
        "edge_id": edge_id,
        "source": source,
        "target": target,
        "kind": kind,
        "derived_from": list(derived_from),
    }


def state(nodes=(), edges=(), evidence=(), mutations=(), controls=(), revision=1):
    return {
        "schema_version": "1.0.0",
        "record_type": "graph_snapshot",
        "graph_id": GRAPH,
        "graph_revision": revision,
        "authority_matrix_version": "1.0.0",
        "intent_nodes": [],
        "intent_edges": [],
        "execution_nodes": list(nodes),
        "schedulable_edges": list(edges),
        "relations": [],
        "evidence_records": [{"schema_version": "1.0.0", "record_type": "evidence_record",
                              "evidence_id": e, "evidence_type": "gate_outcome",
                              "source_ref": "gate", "content_digest": "sha256:" + "c" * 64,
                              "observed_at": "2026-01-01T00:00:00Z", "status": "validated"}
                             for e in evidence],
        "approval_records": [],
        "lease_records": [],
        "control_records": list(controls),
        "mutations": list(mutations),
    }


# ------------------------------------------------------------------ views


class TestNodeSets:
    def test_buckets_and_counts(self):
        snapshot = state(
            nodes=[
                execution_node("EXN-a-001", lifecycle="ready"),
                execution_node("EXN-b-001", lifecycle="succeeded"),
                execution_node("EXN-c-001", lifecycle="blocked"),
                execution_node("EXN-d-001", lifecycle="ready"),
            ]
        )
        sets = node_sets(snapshot, running=["EXN-d-001"])
        assert sets["ready"] == ["EXN-a-001"]
        assert sets["running"] == ["EXN-d-001"]
        assert sets["blocked"] == ["EXN-c-001"]
        assert sets["terminal"] == ["EXN-b-001"]
        assert sets["counts"]["ready"] == 1

    def test_conflict_set_reports_active_serialisation_only(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001", lifecycle="ready"),
                   execution_node("EXN-b-001", lifecycle="running"),
                   execution_node("EXN-c-001", lifecycle="succeeded")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001", kind="ordered_after"),
                   edge("SED-2", "EXN-a-001", "EXN-c-001", kind="ordered_after"),
                   edge("SED-3", "EXN-a-001", "EXN-b-001", kind="depends_on")],
            evidence=["EVD-view-001"],
        )
        conflicts = conflict_set(snapshot)
        assert [c["edge_id"] for c in conflicts] == ["SED-1"], (
            "a finished predecessor is not an active conflict, and depends_on is not a conflict"
        )


class TestExplanations:
    def test_why_ready_names_the_satisfied_dependencies(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001", lifecycle="ready"),
                   execution_node("EXN-b-001", lifecycle="succeeded")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001")],
            evidence=["EVD-view-001"],
        )
        explanation = why(snapshot, "EXN-a-001")
        assert explanation.verdict is Verdict.READY
        satisfied = [r for r in explanation.reasons if r.code == "DEPENDENCY_SATISFIED"]
        assert satisfied[0].subject == "EXN-b-001"
        assert satisfied[0].refs == ("SED-1",), "the explanation cites the edge, not prose"

    def test_why_blocked_names_the_blocking_edge_and_state(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001", lifecycle="admitted"),
                   execution_node("EXN-b-001", lifecycle="running")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001")],
            evidence=["EVD-view-001"],
        )
        explanation = why(snapshot, "EXN-a-001")
        assert explanation.verdict is Verdict.BLOCKED
        pending = [r for r in explanation.reasons if r.code == "DEPENDENCY_PENDING"]
        assert pending[0].subject == "EXN-b-001"
        assert "running" in pending[0].detail

    def test_graph_pause_is_named_as_a_reason(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001", lifecycle="ready")],
            controls=[{"schema_version": "1.0.0", "record_type": "control_record",
                       "control_id": "CTL-1", "graph_id": GRAPH, "state": "paused",
                       "actor": "actor:human.pat", "reason_code": "EV_HUMAN_DECISION"}],
        )
        explanation = why(snapshot, "EXN-a-001")
        assert any(r.code == "GRAPH_CONTROL" for r in explanation.reasons)

    def test_unknown_node_is_unresolved_not_clean(self):
        """AC: a view that cannot resolve something says so."""
        explanation = why(state(), "EXN-ghost-001")
        assert explanation.verdict is Verdict.UNRESOLVED
        assert explanation.unresolved
        assert explanation.reasons == ()

    def test_dangling_edge_endpoint_is_reported_not_dropped(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001", lifecycle="admitted")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-missing-001")],
            evidence=["EVD-view-001"],
        )
        explanation = why(snapshot, "EXN-a-001")
        assert explanation.unresolved, "a missing endpoint must surface"
        assert "EXN-missing-001" in explanation.unresolved[0]

    def test_why_provider_reports_alternatives_and_trigger(self):
        decisions = {
            "EXN-a-001": {
                "provider": "middle", "alternatives": ["cheap", "middle"],
                "factors": ["task_type=review", "escalated to depth 1"],
                "trigger": "FAILING_GATES", "evidence_ids": ["EVD-gate-001"],
            }
        }
        explanation = why_provider(decisions, "EXN-a-001")
        assert any(r.code == "ROUTED_TO" and r.subject == "middle" for r in explanation.reasons)
        trigger = [r for r in explanation.reasons if r.code == "ESCALATION_TRIGGER"][0]
        assert trigger.detail == "FAILING_GATES"
        assert trigger.refs == ("EVD-gate-001",)

    def test_why_provider_without_a_record_is_unresolved(self):
        explanation = why_provider({}, "EXN-a-001")
        assert explanation.verdict is Verdict.UNRESOLVED
        assert explanation.unresolved

    def test_why_changed_reports_actor_evidence_and_budget(self):
        mutation = {
            "mutation_id": "MUT-view-001", "actor": "actor:human.pat",
            "authority_class": "human", "reason_code": "EV_HUMAN_DECISION",
            "evidence_refs": ["EVD-view-001"],
            "budget_delta": {"unit": "tokens", "value": 500},
            "operations": [{"operation_type": "set_control", "target_ref": "CTL-1"}],
        }
        explanation = why_changed(state(mutations=[mutation]), "MUT-view-001")
        codes = {r.code for r in explanation.reasons}
        assert {"ACTOR", "REASON_CODE", "EVIDENCE", "BUDGET_DELTA", "OPERATIONS"} <= codes
        assert [r for r in explanation.reasons if r.code == "ACTOR"][0].subject == "actor:human.pat"

    def test_why_changed_for_an_unknown_mutation_is_unresolved(self):
        explanation = why_changed(state(), "MUT-ghost-001")
        assert explanation.verdict is Verdict.UNRESOLVED


class TestProvenance:
    def test_walks_back_to_evidence_and_mutations(self):
        mutation = {
            "mutation_id": "MUT-view-001", "actor": "actor:test", "authority_class": "automatic",
            "reason_code": "EV_GATE_FAILED", "evidence_refs": ["EVD-view-001"],
            "budget_delta": {"unit": "tokens", "value": 0},
            "operations": [{"operation_type": "transition_node", "target_ref": "EXN-a-001"}],
        }
        snapshot = state(
            nodes=[execution_node("EXN-a-001"), execution_node("EXN-b-001")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001")],
            evidence=["EVD-view-001"],
            mutations=[mutation],
        )
        walk = provenance(snapshot, "EXN-a-001")
        assert walk["evidence"] == ["EVD-view-001"]
        assert walk["mutations"] == ["MUT-view-001"]
        assert walk["unresolved"] == []

    def test_cited_but_unrecorded_evidence_is_surfaced(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001"), execution_node("EXN-b-001")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001", derived_from=("EVD-missing-001",))],
        )
        walk = provenance(snapshot, "EXN-a-001")
        assert walk["unresolved"], "a cited-but-absent evidence id must not vanish from the walk"

    def test_unknown_node_is_unresolved(self):
        walk = provenance(state(), "EXN-ghost-001")
        assert walk["unresolved"]
        assert walk["evidence"] == []


class TestRevisionDiff:
    def test_reports_added_removed_and_changed(self):
        before = state(nodes=[execution_node("EXN-a-001", lifecycle="admitted")], revision=1)
        after = state(
            nodes=[execution_node("EXN-a-001", lifecycle="running"),
                   execution_node("EXN-b-001")],
            revision=2,
        )
        diff = revision_diff(before, after)
        assert diff["from_revision"] == 1 and diff["to_revision"] == 2
        assert diff["added"]["execution_nodes"] == ["EXN-b-001"]
        assert diff["changed"]["execution_nodes"] == ["EXN-a-001"]

    def test_identical_revisions_diff_to_nothing(self):
        snapshot = state(nodes=[execution_node("EXN-a-001")])
        diff = revision_diff(snapshot, snapshot)
        assert diff["added"] == {} and diff["removed"] == {} and diff["changed"] == {}

    def test_new_mutations_are_listed(self):
        mutation = {"mutation_id": "MUT-view-002", "actor": "actor:test",
                    "authority_class": "automatic", "reason_code": "EV_GATE_FAILED",
                    "evidence_refs": [], "budget_delta": {"unit": "tokens", "value": 0},
                    "operations": []}
        diff = revision_diff(state(), state(mutations=[mutation]))
        assert diff["mutations"] == ["MUT-view-002"]


class TestDeterministicExport:
    def test_json_export_is_byte_identical_for_the_same_revision(self):
        """AC: a diff between two exports is a real change, not serialisation noise."""
        nodes = [execution_node("EXN-b-001"), execution_node("EXN-a-001", lifecycle="ready")]
        first = export_json(state(nodes=nodes))
        shuffled = export_json(state(nodes=list(reversed(nodes))))
        assert first == shuffled

    def test_mermaid_export_is_deterministic(self):
        nodes = [execution_node("EXN-b-001"), execution_node("EXN-a-001", lifecycle="ready")]
        edges = [edge("SED-1", "EXN-a-001", "EXN-b-001")]
        first = export_mermaid(state(nodes=nodes, edges=edges, evidence=["EVD-view-001"]))
        again = export_mermaid(
            state(nodes=list(reversed(nodes)), edges=edges, evidence=["EVD-view-001"])
        )
        assert first == again
        assert first.startswith("graph TD")

    def test_json_determinism_comes_from_canonical_encoding(self):
        """Determinism must not depend on this module remembering to sort."""
        from chief_wiggum.dag.canonical import canonical_json_bytes

        payload = {"nodes": [{"execution_node_id": "EXN-b-001"},
                             {"execution_node_id": "EXN-a-001"}]}
        reversed_payload = {"nodes": list(reversed(payload["nodes"]))}
        assert canonical_json_bytes(payload) == canonical_json_bytes(reversed_payload)

    def test_mermaid_redacts_anything_it_renders(self):
        """Redaction is applied at the output boundary, not per field."""
        node = execution_node("EXN-a-001")
        node["lifecycle_state"] = "ghp_abcdefghijklmnopqrstuv"
        diagram = export_mermaid(state(nodes=[node]))
        assert "ghp_abcdefghijklmnopqrstuv" not in diagram
        assert "<redacted:token>" in diagram

    def test_mermaid_arrow_follows_the_direction_work_flows(self):
        snapshot = state(
            nodes=[execution_node("EXN-a-001"), execution_node("EXN-b-001")],
            edges=[edge("SED-1", "EXN-a-001", "EXN-b-001")],
            evidence=["EVD-view-001"],
        )
        diagram = export_mermaid(snapshot)
        # source runs AFTER target, so b flows into a.
        assert "EXN_b_001 --> EXN_a_001" in diagram


class TestRedaction:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "ghp_abcdefghijklmnopqrstuvwxyz1234",
            "xoxb-1234567890-abcdefghijkl",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQabcdef",
            "api_key=hunter2seekrit",
            "/Users/patwork/secret/place",
        ],
    )
    def test_seeded_secrets_never_reach_an_export(self, secret):
        """AC: tested with seeded secrets; the views must not become the leak."""
        node = execution_node("EXN-a-001")
        node["role"] = f"role:implementer {secret}"
        snapshot = state(nodes=[node])
        snapshot["graph_id"] = f"GRF-view001 {secret}"
        exported = export_json(snapshot).decode()
        assert secret not in exported, f"{secret} leaked into the export"
        assert secret not in export_mermaid(snapshot)

    def test_redaction_is_recursive_through_containers(self):
        payload = {"a": ["sk-abcdefghijklmnopqrstuvwxyz123456"], "b": {"c": "ghp_" + "a" * 20}}
        cleaned = redact(payload)
        assert "sk-abcdef" not in str(cleaned)
        assert "ghp_" not in str(cleaned)

    def test_ordinary_text_survives_redaction(self):
        assert redact("EXN-a-001 is blocked on EXN-b-001") == "EXN-a-001 is blocked on EXN-b-001"


class TestOperatorControls:
    def test_control_is_a_journaled_human_actor_mutation(self):
        """AC: every control is audited; there is no out-of-band control path."""
        envelope = control_envelope(
            "pause", graph_id=GRAPH, base_revision=3, operator="actor:human.pat",
            reason="investigating a flaky gate", mutation_id="MUT-ctl-001",
            idempotency_key="ctl-1", control_id="CTL-ctl-001",
        )
        assert envelope["actor"] == "actor:human.pat"
        assert envelope["authority_class"] == "human"
        assert envelope["requires_approval"] is True
        assert envelope["operations"][0]["value"]["state"] == "pause_requested"
        assert "investigating" in envelope["expected_effect"]

    def test_resume_and_cancel_map_to_control_states(self):
        for action, expected in (("resume", "active"), ("cancel", "cancel_requested")):
            envelope = control_envelope(
                action, graph_id=GRAPH, base_revision=1, operator="actor:human.pat",
                reason="because", mutation_id="MUT-ctl-002", idempotency_key="ctl-2",
                control_id="CTL-ctl-002",
            )
            assert envelope["operations"][0]["value"]["state"] == expected

    def test_a_model_may_not_issue_an_operator_control(self):
        with pytest.raises(ValueError, match="human actor"):
            control_envelope(
                "cancel", graph_id=GRAPH, base_revision=1, operator="actor:model.opus",
                reason="I would like to stop", mutation_id="MUT-ctl-003",
                idempotency_key="ctl-3", control_id="CTL-ctl-003",
            )

    def test_unknown_action_is_refused(self):
        with pytest.raises(ValueError, match="unknown control action"):
            control_envelope(
                "obliterate", graph_id=GRAPH, base_revision=1, operator="actor:human.pat",
                reason="", mutation_id="MUT-ctl-004", idempotency_key="ctl-4",
                control_id="CTL-ctl-004",
            )

    def test_control_reason_is_redacted(self):
        envelope = control_envelope(
            "pause", graph_id=GRAPH, base_revision=1, operator="actor:human.pat",
            reason="token ghp_abcdefghijklmnopqrstuv leaked", mutation_id="MUT-ctl-005",
            idempotency_key="ctl-5", control_id="CTL-ctl-005",
        )
        assert "ghp_abcdefghijklmnopqrstuv" not in envelope["expected_effect"]

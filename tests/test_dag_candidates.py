"""Parallel candidates with verifier-blind promotion (chief-wiggum#389).

The two tests that justify the whole feature are the verifier freeze and the
identity blinding. If either can be defeated, fan-out is just a way to spend
three times as much for a result nobody can trust.
"""

import pytest
from chief_wiggum.dag import GraphJournal
from chief_wiggum.dag.candidates import (
    CandidatePolicy,
    CandidateResult,
    FanOutRefusal,
    NodeProfile,
    Promotion,
    PromotionRefusal,
    VerifierFreeze,
    VerifierTampered,
    blind,
    decide_fan_out,
    presentation_order,
    promote,
    promotion_operations,
    rubric_key,
)
from chief_wiggum.dag.semantics import validate_snapshot

VERIFIER = "def test_contract(): assert behaviour_is_correct()"
GRAPH = "GRF-cand001"


def freeze(node_id="EXN-cand-001", artifact=VERIFIER, author=""):
    return VerifierFreeze.of(node_id, artifact, author_provider=author,
                             frozen_at="2026-01-01T00:00:00Z")


def candidate(name, *, provider, digest=None, gates=None, tests_failed=0, tests_passed=10,
              conformance=1.0, diff_lines=100, blast=1, hotspot=0, cost=1.0):
    return CandidateResult(
        candidate_id=name,
        provider=provider,
        artifact_digest=digest or f"sha256:{name}",
        hard_gates=gates if gates is not None else {"ratchet": "pass", "traceability": "pass"},
        tests_failed=tests_failed,
        tests_passed=tests_passed,
        contract_conformance=conformance,
        diff_lines=diff_lines,
        blast_radius=blast,
        hotspot_overlap=hotspot,
        cost=cost,
    )


# ------------------------------------------------------------ verifier freeze


class TestVerifierFreeze:
    def test_unmodified_verifier_passes(self):
        frozen = freeze()
        frozen.verify(VERIFIER)

    def test_verifier_modified_after_freeze_is_fatal(self):
        """AC: a verifier changed after candidate output exists fails hard."""
        frozen = freeze()
        with pytest.raises(VerifierTampered, match="changed after freeze"):
            frozen.verify(VERIFIER + "\ndef test_that_fits_the_winner(): pass")

    def test_tampering_voids_the_promotion_entirely(self):
        frozen = freeze()
        result = promote(
            [candidate("c1", provider="alpha"), candidate("c2", provider="beta")],
            verifier=frozen,
            verifier_artifact=VERIFIER + "  # weakened",
        )
        assert not result.promoted
        assert result.refusal is PromotionRefusal.VERIFIER_TAMPERED
        assert result.winner is None, "no winner may survive a tampered comparison"

    def test_fan_out_is_refused_without_a_frozen_verifier(self):
        decision = decide_fan_out(NodeProfile(risk_class="high"), verifier=None)
        assert not decision.fans_out
        assert decision.refusal is FanOutRefusal.VERIFIER_NOT_FROZEN


# ------------------------------------------------------------ identity blind


class TestIdentityBlinding:
    def test_blind_view_has_no_provider_field(self):
        """The structural guarantee: the scorer cannot see identity."""
        view = blind(candidate("c1", provider="alpha"))
        assert not hasattr(view, "provider")
        assert "alpha" not in repr(view)

    def test_swapping_providers_does_not_change_the_winner(self):
        """AC: identity-blind ordering, everything else fixed."""
        def strong(provider):
            return candidate("c1", provider=provider, digest="sha256:strong",
                             conformance=1.0, diff_lines=50)

        def weak(provider):
            return candidate("c2", provider=provider, digest="sha256:weak",
                             conformance=0.6, diff_lines=900)

        first = promote([strong("alpha"), weak("beta")],
                        verifier=freeze(), verifier_artifact=VERIFIER, seed=7)
        swapped = promote([strong("beta"), weak("alpha")],
                          verifier=freeze(), verifier_artifact=VERIFIER, seed=7)
        assert first.winner == swapped.winner == "c1"
        assert first.winner_provider == "alpha"
        assert swapped.winner_provider == "beta"

    def test_blind_label_is_derived_from_content_not_identity(self):
        same_content = candidate("c1", provider="alpha", digest="sha256:same")
        other_provider = candidate("c2", provider="omega", digest="sha256:same")
        assert same_content.blind_label == other_provider.blind_label

    def test_presentation_order_is_seeded_and_replayable(self):
        views = [blind(candidate(f"c{i}", provider=f"p{i}", digest=f"sha256:{i}"))
                 for i in range(6)]
        assert [v.label for v in presentation_order(views, 42)] == [
            v.label for v in presentation_order(views, 42)
        ]

    def test_ordering_seed_does_not_change_the_winner(self):
        """Position must not proxy for identity: the rubric decides, not order."""
        results = [
            candidate("c1", provider="alpha", digest="sha256:a", conformance=1.0),
            candidate("c2", provider="beta", digest="sha256:b", conformance=0.5),
            candidate("c3", provider="gamma", digest="sha256:c", conformance=0.7),
        ]
        winners = {
            promote(results, verifier=freeze(), verifier_artifact=VERIFIER, seed=seed).winner
            for seed in range(12)
        }
        assert winners == {"c1"}


# ------------------------------------------------------------ hard gates


class TestHardGateElimination:
    def test_seeded_defective_candidate_never_wins(self):
        """AC: with one defective and one correct candidate, the correct one wins."""
        good = candidate("good", provider="alpha", digest="sha256:good")
        bad = candidate("bad", provider="beta", digest="sha256:bad",
                        gates={"ratchet": "findings"}, tests_failed=3)
        for seed in range(10):
            result = promote([good, bad], verifier=freeze(),
                             verifier_artifact=VERIFIER, seed=seed)
            assert result.winner == "good"
            assert result.eliminated == ("bad",)

    def test_all_candidates_failing_promotes_nobody(self):
        """AC: no best-of-a-bad-lot promotion under any configuration."""
        results = [
            candidate("c1", provider="alpha", gates={"ratchet": "findings"}),
            candidate("c2", provider="beta", tests_failed=1),
            candidate("c3", provider="gamma", gates={"traceability": "error"}),
        ]
        outcome = promote(results, verifier=freeze(), verifier_artifact=VERIFIER)
        assert not outcome.promoted
        assert outcome.refusal is PromotionRefusal.ALL_CANDIDATES_FAILED
        assert set(outcome.eliminated) == {"c1", "c2", "c3"}

    def test_gate_error_is_not_treated_as_a_pass(self):
        errored = candidate("c1", provider="alpha", gates={"ratchet": "error"})
        assert errored.eliminated
        assert errored.failing_gates == ["ratchet"]

    def test_inapplicable_gate_does_not_eliminate(self):
        skipped = candidate("c1", provider="alpha", gates={"single_writer": "inapplicable"})
        assert not skipped.eliminated

    def test_no_candidates_refuses_rather_than_returning_none_silently(self):
        outcome = promote([], verifier=freeze(), verifier_artifact=VERIFIER)
        assert outcome.refusal is PromotionRefusal.NO_CANDIDATES


# ------------------------------------------------------------ rubric


class TestRubric:
    def test_rubric_is_deterministic(self):
        view = blind(candidate("c1", provider="alpha"))
        assert rubric_key(view) == rubric_key(view)

    def test_contract_conformance_outranks_diff_size(self):
        conformant = blind(candidate("c1", provider="a", digest="sha256:1",
                                     conformance=1.0, diff_lines=5000))
        tidy = blind(candidate("c2", provider="b", digest="sha256:2",
                               conformance=0.9, diff_lines=10))
        assert rubric_key(conformant) < rubric_key(tidy)

    def test_smaller_blast_radius_wins_a_tie(self):
        wide = blind(candidate("c1", provider="a", digest="sha256:1", blast=9))
        narrow = blind(candidate("c2", provider="b", digest="sha256:2", blast=1))
        assert rubric_key(narrow) < rubric_key(wide)

    def test_ties_break_on_the_content_label(self):
        left = blind(candidate("c1", provider="a", digest="sha256:aaa"))
        right = blind(candidate("c2", provider="b", digest="sha256:bbb"))
        assert rubric_key(left)[-1] != rubric_key(right)[-1]


# ------------------------------------------------------------ fan-out policy


class TestFanOutPolicy:
    def test_routine_work_does_not_fan_out(self):
        decision = decide_fan_out(NodeProfile(risk_class="standard"), verifier=freeze())
        assert decision.width == 1
        assert decision.refusal is FanOutRefusal.NOT_JUSTIFIED

    def test_high_risk_work_fans_out(self):
        decision = decide_fan_out(NodeProfile(risk_class="high"), verifier=freeze())
        assert decision.fans_out
        assert decision.width == 3

    def test_isolation_failure_refuses_rather_than_degrading(self):
        """AC: with isolation unavailable, fan-out is refused with a named reason."""
        decision = decide_fan_out(
            NodeProfile(risk_class="high"),
            verifier=freeze(),
            isolation_available=False,
            isolation_detail="refs/stash is shared across worktrees (#376)",
        )
        assert decision.width == 1
        assert decision.refusal is FanOutRefusal.ISOLATION_UNAVAILABLE
        assert "refs/stash" in decision.detail

    def test_budget_caps_the_width(self):
        decision = decide_fan_out(
            NodeProfile(risk_class="high"),
            CandidatePolicy(max_width=5, per_node_budget=3.0, candidate_cost=1.0),
            verifier=freeze(),
        )
        assert decision.width == 3

    def test_insufficient_budget_refuses_fan_out(self):
        decision = decide_fan_out(
            NodeProfile(risk_class="high"),
            CandidatePolicy(per_node_budget=4.0, candidate_cost=1.0),
            spent=3.0,
            verifier=freeze(),
        )
        assert decision.width == 1
        assert decision.refusal is FanOutRefusal.BUDGET_EXCEEDED


# ------------------------------------------- exactly one promotion, in the graph


class TestExactlyOnePromotion:
    def _snapshot(self, dispositions):
        nodes = []
        for index, disposition in enumerate(dispositions, start=1):
            nodes.append(
                {
                    "schema_version": "1.0.0",
                    "record_type": "execution_node",
                    "execution_node_id": f"EXN-cand-{index:03d}",
                    "intent_node_id": "INN-cand-001",
                    "node_type": "candidate",
                    "role": "role:implementer",
                    "lifecycle_state": "succeeded",
                    "attempt": {"attempt_id": None, "outcome": "succeeded"},
                    "candidate": {"group_id": "CND-group-001", "disposition": disposition},
                    "approval_state": "not_required",
                    "lease_state": "unclaimed",
                    "control_state": "active",
                    "compiled_from": {
                        "intent_node_id": "INN-cand-001",
                        "intent_graph_digest": "sha256:" + "b" * 64,
                    },
                }
            )
        return {
            "schema_version": "1.0.0",
            "record_type": "graph_snapshot",
            "graph_id": GRAPH,
            "graph_revision": 1,
            "authority_matrix_version": "1.0.0",
            "intent_nodes": [
                {
                    "schema_version": "1.0.0",
                    "record_type": "intent_node",
                    "intent_node_id": "INN-cand-001",
                    "node_type": "implementation",
                    "role": "role:implementer",
                    "source_ref": "ticket:#1",
                    "in_scope": True,
                }
            ],
            "intent_edges": [],
            "execution_nodes": nodes,
            "schedulable_edges": [],
            "relations": [],
            "evidence_records": [],
            "approval_records": [],
            "lease_records": [],
            "control_records": [],
            "mutations": [],
        }

    def test_one_promotion_is_valid(self):
        errors = validate_snapshot(self._snapshot(["promoted", "superseded", "eliminated"]))
        assert errors == ()

    def test_two_promotions_in_one_group_is_an_invalid_graph(self):
        """AC: a second promotion is rejected at the contract level."""
        errors = validate_snapshot(self._snapshot(["promoted", "promoted"]))
        assert errors, "a second promotion must be a contract violation"
        assert any("exactly one promotion" in error.message for error in errors)

    def test_the_engine_refuses_a_second_promotion(self, tmp_path):
        """Not merely avoided by convention: the journal will not admit it."""
        from chief_wiggum.dag.schemas import load_authority_matrix

        matrix = {r["operation_type"]: r for r in load_authority_matrix()["operations"]}
        journal = GraphJournal(tmp_path / "graph.db")
        journal.init_graph(GRAPH)

        def apply(operations, base_revision, index):
            needs = any(
                matrix.get(op["operation_type"], {}).get("approval_required") for op in operations
            )
            return journal.propose(
                {
                    "schema_version": "1.0.0",
                    "record_type": "mutation_envelope",
                    "graph_id": GRAPH,
                    "base_revision": base_revision,
                    "mutation_id": f"MUT-cand-{index:03d}",
                    "idempotency_key": f"cand-{index}",
                    "actor": "actor:test",
                    "authority_class": "human" if needs else "automatic",
                    "operations": operations,
                    "reason_code": "EV_CANDIDATE_PROMOTED",
                    "evidence_refs": [],
                    "expected_effect": "candidate fixture",
                    "budget_delta": {"unit": "tokens", "value": 0},
                    "requires_approval": needs,
                }
            )

        snapshot = self._snapshot(["pending", "pending"])
        revision = 0
        decision = apply(
            [{"op_id": "OPS-cand-001", "operation_type": "add_intent_node",
              "target_ref": "INN-cand-001", "value": snapshot["intent_nodes"][0]}],
            revision, 1,
        )
        assert decision.accepted, decision.violations
        revision = decision.graph_revision
        for index, node in enumerate(snapshot["execution_nodes"], start=2):
            decision = apply(
                [{"op_id": f"OPS-cand-{index:03d}", "operation_type": "add_execution_node",
                  "target_ref": node["execution_node_id"], "value": node}],
                revision, index,
            )
            assert decision.accepted, decision.violations
            revision = decision.graph_revision

        first = apply(
            [{"op_id": "OPS-cand-010", "operation_type": "promote_candidate",
              "target_ref": "EXN-cand-001",
              "value": {"candidate_group_id": "CND-group-001",
                        "execution_node_id": "EXN-cand-001"}}],
            revision, 10,
        )
        assert first.accepted, first.violations

        second = apply(
            [{"op_id": "OPS-cand-011", "operation_type": "promote_candidate",
              "target_ref": "EXN-cand-002",
              "value": {"candidate_group_id": "CND-group-001",
                        "execution_node_id": "EXN-cand-002"}}],
            first.graph_revision, 11,
        )
        assert not second.accepted, "the engine must refuse a second promotion"
        assert any("exactly one promotion" in v.message for v in second.violations)
        journal.close()


# ------------------------------------------------------------ lineage


class TestLoserRetention:
    def test_losers_are_superseded_not_deleted(self):
        results = [
            candidate("winner", provider="alpha", digest="sha256:w", conformance=1.0),
            candidate("loser", provider="beta", digest="sha256:l", conformance=0.5),
        ]
        outcome = promote(results, verifier=freeze(), verifier_artifact=VERIFIER)
        assert outcome.winner == "winner"
        assert outcome.superseded == ("loser",)

        operations = promotion_operations(
            "CND-group-001", outcome,
            {"winner": "EXN-cand-001", "loser": "EXN-cand-002"},
        )
        assert operations[0]["operation_type"] == "promote_candidate"
        supersedes = [op for op in operations if op["operation_type"] == "add_relation"]
        assert len(supersedes) == 1
        assert supersedes[0]["value"]["kind"] == "supersedes"
        assert supersedes[0]["value"]["target"] == "EXN-cand-002"

    def test_no_operations_are_emitted_without_a_winner(self):
        assert promotion_operations("CND-group-001", Promotion(winner=None), {}) == []

"""Evidence-based cheap-first routing with bounded escalation (chief-wiggum#388).

The load-bearing test is `TestSelfReportIsAdvisoryOnly`: a model saying it is
struggling must not be able to spend more money. Everything else is ordinary
policy that happens to be worth pinning down.
"""

import json
import re
from pathlib import Path

from chief_wiggum.dag.routing import (
    OBJECTIVE_EVIDENCE_CLASSES,
    Capability,
    Ceilings,
    EscalationRule,
    EscalationTrigger,
    ObjectiveSignals,
    Router,
    RoutingRefusal,
    TaskDemand,
    capable,
    escalation_triggers,
)

ROOT = Path(__file__).resolve().parents[1]

# A deliberately synthetic config. Real vendor names live in providers.json.
PROVIDERS = {
    "cheap": {"enabled": True, "cost_tier": 1, "vendor_family": "alpha",
              "reads_repo": False, "accepts_images": False, "supports_tools": False,
              "context_window": 100_000},
    "middle": {"enabled": True, "cost_tier": 3, "vendor_family": "beta",
               "reads_repo": True, "accepts_images": True, "supports_tools": True,
               "context_window": 200_000},
    "dear": {"enabled": True, "cost_tier": 5, "vendor_family": "gamma",
             "reads_repo": True, "accepts_images": True, "supports_tools": True,
             "context_window": 400_000},
}
ROLES = {
    "reviewer": {"required": ["cheap", "middle"], "optional": ["dear"]},
    "verifier": {"required": ["cheap", "middle", "dear"], "optional": []},
    "lonely": {"required": ["cheap"], "optional": []},
    "empty": {"required": [], "optional": []},
}


def router(**kwargs):
    return Router(PROVIDERS, ROLES, **kwargs)


class TestInitialRouting:
    def test_routine_task_takes_the_cheapest_capable_provider(self):
        decision = router().route("reviewer")
        assert decision.provider == "cheap"
        assert decision.trigger is None
        assert decision.depth == 0
        assert "cheapest capable provider" in decision.factors

    def test_capability_filters_out_the_cheap_provider_when_the_work_needs_more(self):
        decision = router().route("reviewer", TaskDemand(needs_repo_read=True))
        assert decision.provider == "middle", "cheap cannot read the repo"
        assert any("cannot read the repo" in factor for factor in decision.factors)

    def test_context_window_is_respected(self):
        decision = router().route("reviewer", TaskDemand(context_tokens=150_000))
        assert decision.provider == "middle"

    def test_calibration_can_outrank_a_peer_at_the_same_cost_tier(self):
        providers = dict(PROVIDERS)
        providers["cheap_two"] = dict(PROVIDERS["cheap"], vendor_family="delta")
        roles = {"reviewer": {"required": ["cheap", "cheap_two"], "optional": []}}
        calibrated = Router(providers, roles, calibration={"review:cheap_two": 0.9})
        assert calibrated.route("reviewer").provider == "cheap_two"

    def test_unknown_role_refuses_rather_than_guessing(self):
        decision = router().route("empty")
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.NO_CAPABLE_PROVIDER


class TestEscalation:
    def test_two_failing_gate_attempts_escalate_and_name_the_trigger(self):
        signals = ObjectiveSignals(failing_gate_attempts=2, evidence_ids=("EVD-gate-001",))
        decision = router().route("reviewer", signals=signals)
        assert decision.provider == "middle", "escalated one step up the cost ladder"
        assert decision.trigger is EscalationTrigger.FAILING_GATES
        assert decision.depth == 1
        assert decision.evidence_ids == ("EVD-gate-001",)
        assert decision.estimated_cost_tier == 3

    def test_escalation_depth_is_capped(self):
        signals = ObjectiveSignals(
            failing_gate_attempts=9, tool_errors=9, stalled_heartbeats=9,
            replan_proposals=9, retry_budget_remaining=0,
        )
        capped = router(rule=EscalationRule(max_depth=1))
        decision = capped.route("reviewer", signals=signals)
        assert decision.depth == 1, "the cap bounds spend even with every trigger firing"
        assert decision.provider == "middle"

    def test_every_trigger_is_objective(self):
        """Each enumerated trigger maps to something the run did."""
        assert set(EscalationTrigger) == {
            EscalationTrigger.FAILING_GATES,
            EscalationTrigger.REPEATED_TOOL_ERRORS,
            EscalationTrigger.STALLED_PROGRESS,
            EscalationTrigger.PLAN_CHURN,
            EscalationTrigger.RETRY_BUDGET_EXHAUSTED,
        }

    def test_healthy_signals_do_not_escalate(self):
        assert escalation_triggers(ObjectiveSignals()) == []

    def test_only_objective_evidence_classes_are_consumed(self):
        records = [
            {"evidence_type": "gate_outcome", "evidence_id": "EVD-a-001"},
            {"evidence_type": "model_self_report", "evidence_id": "EVD-b-002"},
        ]
        signals = ObjectiveSignals.from_evidence(records)
        assert signals.evidence_ids == ("EVD-a-001",)
        assert "model_self_report" not in OBJECTIVE_EVIDENCE_CLASSES


class TestSelfReportIsAdvisoryOnly:
    def test_objective_signals_have_no_confidence_field(self):
        """The structural guarantee: there is nowhere to put it."""
        assert not any(
            "confidence" in name for name in ObjectiveSignals.__dataclass_fields__
        )

    def test_despair_does_not_escalate_when_every_objective_signal_is_healthy(self):
        """AC: low self-reported confidence plus healthy signals must not escalate."""
        baseline = router().route("reviewer")
        for confidence in (0.0, 0.01, 0.5, 1.0, -99.0):
            decision = router().route(
                "reviewer", self_reported_confidence=confidence
            )
            assert decision.provider == baseline.provider == "cheap"
            assert decision.trigger is None
            assert decision.explanation() == baseline.explanation()

    def test_confidence_cannot_suppress_a_real_escalation_either(self):
        """The guarantee runs both ways: bravado must not save money."""
        signals = ObjectiveSignals(failing_gate_attempts=2)
        confident = router().route("reviewer", signals=signals, self_reported_confidence=1.0)
        assert confident.provider == "middle"
        assert confident.trigger is EscalationTrigger.FAILING_GATES


class TestIndependence:
    def test_author_may_never_verify_its_own_artifact(self):
        demand = TaskDemand(
            author_provider="middle", requires_independent_verifier=True, needs_repo_read=True
        )
        decision = router().route("verifier", demand)
        assert decision.provider == "dear"
        assert decision.provider != "middle"

    def test_router_fails_closed_when_no_independent_verifier_exists(self):
        """AC: there is no configuration in which an artifact's author verifies it."""
        roles = {"verifier": {"required": ["middle"], "optional": []}}
        decision = Router(PROVIDERS, roles).route(
            "verifier",
            TaskDemand(author_provider="middle", requires_independent_verifier=True),
        )
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.NO_INDEPENDENT_VERIFIER
        assert decision.detail

    def test_same_vendor_family_is_not_independent(self):
        providers = dict(PROVIDERS)
        providers["sibling"] = dict(PROVIDERS["dear"], vendor_family="gamma")
        roles = {"verifier": {"required": ["sibling"], "optional": []}}
        decision = Router(providers, roles).route(
            "verifier",
            TaskDemand(author_provider="dear", requires_independent_verifier=True),
        )
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.NO_INDEPENDENT_VERIFIER

    def test_unknown_author_cannot_be_proven_independent(self):
        decision = router().route(
            "verifier",
            TaskDemand(author_provider="ghost", requires_independent_verifier=True),
        )
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.NO_INDEPENDENT_VERIFIER


class TestCeilingsAndOutage:
    def test_a_node_near_its_ceiling_stops_rather_than_escalating_into_it(self):
        constrained = router(ceilings=Ceilings(per_node_cost=4))
        decision = constrained.route(
            "reviewer", signals=ObjectiveSignals(failing_gate_attempts=2), spent_cost=2
        )
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.COST_CEILING_REACHED
        assert decision.trigger is EscalationTrigger.FAILING_GATES
        assert "ceiling" in decision.detail

    def test_fallback_chain_is_followed_in_order(self):
        assert router().route("reviewer", unavailable=["cheap"]).provider == "middle"
        assert router().route("reviewer", unavailable=["cheap", "middle"]).provider == "dear"

    def test_exhausted_chain_blocks_with_a_reason_rather_than_downgrading(self):
        decision = router().route("reviewer", unavailable=["cheap", "middle", "dear"])
        assert not decision.routed
        assert decision.refusal is RoutingRefusal.FALLBACK_CHAIN_EXHAUSTED
        assert decision.detail

    def test_a_role_with_one_provider_does_not_pretend_to_escalate(self):
        decision = router().route(
            "lonely", signals=ObjectiveSignals(failing_gate_attempts=5)
        )
        assert decision.provider == "cheap"
        assert decision.depth == 0
        assert any("no more expensive" in factor for factor in decision.factors)


class TestDeterminismAndTelemetry:
    def test_identical_inputs_produce_identical_decisions(self):
        signals = ObjectiveSignals(failing_gate_attempts=2, evidence_ids=("EVD-x-001",))
        first = router().route("reviewer", TaskDemand(context_tokens=10), signals=signals)
        second = router().route("reviewer", TaskDemand(context_tokens=10), signals=signals)
        assert first.explanation() == second.explanation()

    def test_ties_break_on_provider_name(self):
        providers = {
            "zulu": dict(PROVIDERS["cheap"], vendor_family="z"),
            "alpha": dict(PROVIDERS["cheap"], vendor_family="a"),
        }
        roles = {"reviewer": {"required": ["zulu", "alpha"], "optional": []}}
        assert Router(providers, roles).route("reviewer").provider == "alpha"

    def test_every_decision_carries_an_explanation(self):
        """AC: a decision with no explanation is a test failure."""
        cases = [
            router().route("reviewer"),
            router().route("reviewer", signals=ObjectiveSignals(failing_gate_attempts=2)),
            router().route("reviewer", unavailable=["cheap", "middle", "dear"]),
            router(ceilings=Ceilings(per_node_cost=1)).route(
                "reviewer", signals=ObjectiveSignals(failing_gate_attempts=2), spent_cost=1
            ),
        ]
        for decision in cases:
            explanation = decision.explanation()
            assert explanation["role"]
            assert explanation["factors"], f"no deciding factors recorded: {explanation}"
            assert explanation["alternatives"] or explanation["refusal"]
            if not decision.routed:
                assert explanation["refusal"], "a refusal must name its reason"


class TestStaticRouting:
    def test_static_reproduces_fixed_role_membership(self):
        decision = router(static=True).route("reviewer")
        assert decision.provider == "cheap"
        assert decision.factors == ("static-routing",)

    def test_static_ignores_escalation_entirely(self):
        decision = router(static=True).route(
            "reviewer", signals=ObjectiveSignals(failing_gate_attempts=9)
        )
        assert decision.provider == "cheap", "static routing does not escalate"
        assert decision.trigger is None

    def test_shadow_reports_both_choices_and_whether_they_differ(self):
        report = router().shadow(
            "reviewer", TaskDemand(), signals=ObjectiveSignals(failing_gate_attempts=2)
        )
        assert report["routed"]["provider"] == "middle"
        assert report["static"]["provider"] == "cheap"
        assert report["differs"] is True


class TestConfigContract:
    def test_shipped_config_carries_capability_metadata(self):
        config = json.loads((ROOT / "config" / "providers.json").read_text())
        for name, entry in config["providers"].items():
            capability = Capability.from_config(name, entry)
            assert capability.cost_tier >= 1, f"{name} has no usable cost tier"
            assert capability.vendor_family, f"{name} has no vendor family"

    def test_existing_configs_without_metadata_remain_valid(self):
        capability = Capability.from_config("legacy", {"enabled": True})
        assert capability.cost_tier > 0
        assert capable(capability, TaskDemand())[0]

    def test_routing_code_hard_codes_no_vendor_names(self):
        """AC: no model or vendor string outside config/providers.json."""
        config = json.loads((ROOT / "config" / "providers.json").read_text())
        vendor_words = set()
        for name, entry in config["providers"].items():
            vendor_words.add(name.split("-")[0].lower())
            model = str(entry.get("model", ""))
            if model:
                vendor_words.update(re.split(r"[/\-.]", model.lower()))
        vendor_words -= {"", "claude", "preview", "worker", "openrouter", "v4", "k3", "5", "2"}

        source = (ROOT / "scripts" / "chief_wiggum" / "dag" / "routing.py").read_text().lower()
        offenders = sorted(
            word for word in vendor_words
            if len(word) > 3 and re.search(rf"\b{re.escape(word)}\b", source)
        )
        assert offenders == [], f"routing logic names vendors: {offenders}"

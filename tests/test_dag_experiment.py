"""Analysis substrate for the dynamic-DAG ablation (chief-wiggum#391).

The degenerate gap-closure cases carry this file. Each one is a way the
experiment could quietly report a flattering number, and each is decided in
code rather than left to whoever runs the analysis.
"""

import re
from pathlib import Path

import pytest
from chief_wiggum.dag.experiment import (
    ArmResult,
    GapClosureStatus,
    NonInferiority,
    ProtocolViolation,
    RunManifest,
    cost_quality_frontier,
    gap_closure,
    negative_findings,
    protocol_violations,
    readme_claim_allowed,
    reporting_failures,
    wilson_interval,
)

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "experiments" / "dynamic-dag" / "pre-registration.md"


def arm(name, accepted, attempted, **kwargs):
    return ArmResult(
        arm=name,
        model_tier=kwargs.pop("tier", "open-tier"),
        process=kwargs.pop("process", "dynamic"),
        accepted=accepted,
        attempted=attempted,
        **kwargs,
    )


# ------------------------------------------------------------ gap closure


class TestGapClosureDegenerateCases:
    def test_ordinary_case_is_defined(self):
        closure = gap_closure(frontier_quality=0.80, static_open_quality=0.60,
                              adaptive_open_quality=0.70)
        assert closure.status is GapClosureStatus.DEFINED
        assert closure.value == pytest.approx(0.5)
        assert closure.publishable

    def test_no_measurable_gap_is_undefined_and_unpublishable(self):
        """AC: denominator near zero means the corpus was not discriminative."""
        closure = gap_closure(frontier_quality=0.70, static_open_quality=0.70,
                              adaptive_open_quality=0.75)
        assert closure.status is GapClosureStatus.UNDEFINED_NO_GAP
        assert closure.value is None
        assert not closure.publishable
        assert "not discriminative" in closure.detail

    def test_inverted_gap_is_meaningless_not_a_big_number(self):
        """Open beating frontier under the static process must not yield a ratio."""
        closure = gap_closure(frontier_quality=0.60, static_open_quality=0.70,
                              adaptive_open_quality=0.75)
        assert closure.status is GapClosureStatus.MEANINGLESS_INVERTED_GAP
        assert closure.value is None
        assert not closure.publishable

    def test_regression_is_reported_negative_and_never_clipped(self):
        """AC: a dynamic process that makes the open model worse says so."""
        closure = gap_closure(frontier_quality=0.80, static_open_quality=0.60,
                              adaptive_open_quality=0.50)
        assert closure.status is GapClosureStatus.NEGATIVE_REGRESSION
        assert closure.value is not None and closure.value < 0
        assert not closure.publishable, "a regression is not a gap-closure headline"

    def test_tiny_positive_denominator_is_still_degenerate(self):
        closure = gap_closure(frontier_quality=0.70 + 1e-12, static_open_quality=0.70,
                              adaptive_open_quality=0.90)
        assert closure.status is GapClosureStatus.UNDEFINED_NO_GAP


# ------------------------------------------------------------ uncertainty


class TestUncertainty:
    def test_wilson_interval_stays_inside_zero_and_one_at_the_extremes(self):
        """The reason for Wilson over the normal approximation."""
        perfect = wilson_interval(20, 20)
        assert perfect.low > 0.0 and perfect.high <= 1.0
        assert perfect.width > 0, "a perfect score still carries uncertainty"
        zero = wilson_interval(0, 20)
        assert zero.low >= 0.0 and zero.high < 1.0
        assert zero.width > 0

    def test_small_n_produces_a_wide_interval(self):
        """The N=20 caveat the README already carries, made mechanical."""
        small = wilson_interval(15, 20)
        assert small.width > 0.30

    def test_larger_n_narrows_the_interval(self):
        assert wilson_interval(150, 200).width < wilson_interval(15, 20).width

    def test_no_data_is_not_a_zero_score(self):
        empty = wilson_interval(0, 0)
        assert empty.n == 0
        assert "no data" in empty.render()

    def test_render_always_carries_n_and_interval(self):
        rendered = wilson_interval(15, 20).render()
        assert "N=20" in rendered and "95% CI" in rendered

    def test_impossible_counts_are_refused(self):
        with pytest.raises(ValueError, match="out of range"):
            wilson_interval(21, 20)


# ------------------------------------------------------- non-inferiority


class TestNonInferiority:
    def test_margin_assessment_is_conservative(self):
        """Comparing point estimates would call this non-inferior; the interval
        overlap says otherwise, and the conservative reading must win."""
        margin = NonInferiority(margin=0.05, justification="below suite noise")
        treatment = wilson_interval(150, 200)   # 75.0%
        control = wilson_interval(158, 200)     # 79.0%, a 4pp point gap
        assessment = margin.assess(treatment, control)
        assert assessment["difference"] > -margin.margin, (
            "the point estimates alone sit inside the margin"
        )
        assert assessment["worst_case_difference"] < assessment["difference"]
        assert not assessment["non_inferior"], (
            "uncertainty this wide must not be reported as non-inferiority"
        )

    def test_clearly_worse_treatment_is_not_non_inferior(self):
        margin = NonInferiority(margin=0.05, justification="below suite noise")
        assessment = margin.assess(wilson_interval(100, 200), wilson_interval(180, 200))
        assert not assessment["non_inferior"]

    def test_margin_carries_its_justification(self):
        margin = NonInferiority(margin=0.05, justification="below suite noise")
        assert margin.assess(wilson_interval(1, 10), wilson_interval(1, 10))["justification"]


# ------------------------------------------------------- protocol control


class TestProtocolViolations:
    def _manifest(self, **overrides):
        base = {
            "arm": "arm-1",
            "corpus_version": "corpus-v1",
            "provider_roster": {"reviewer": "cheap"},
            "seeds": {"ordering": 7},
            "budgets": {"per_task_cents": 1000},
            "verifier_hash": "sha256:" + "a" * 64,
            "environment": {"python": "3.14"},
        }
        base.update(overrides)
        return RunManifest(**base)

    def test_identical_manifests_have_no_violations(self):
        assert protocol_violations(self._manifest(), self._manifest()) == []

    def test_a_changed_verifier_is_a_violation(self):
        violations = protocol_violations(
            self._manifest(), self._manifest(verifier_hash="sha256:" + "b" * 64)
        )
        assert ProtocolViolation.VERIFIER_CHANGED in violations

    def test_changed_corpus_budget_and_environment_are_violations(self):
        violations = protocol_violations(
            self._manifest(),
            self._manifest(corpus_version="corpus-v2", budgets={"per_task_cents": 9900},
                           environment={"python": "3.13"}),
        )
        assert ProtocolViolation.CORPUS_CHANGED in violations
        assert ProtocolViolation.BUDGET_CHANGED in violations
        assert ProtocolViolation.ENVIRONMENT_CHANGED in violations

    def test_manifest_digest_is_stable_and_order_independent(self):
        left = self._manifest(provider_roster={"a": "x", "b": "y"})
        right = self._manifest(provider_roster={"b": "y", "a": "x"})
        assert left.digest() == right.digest()

    def test_manifest_digest_changes_when_a_seed_changes(self):
        assert self._manifest().digest() != self._manifest(seeds={"ordering": 8}).digest()

    def test_a_float_in_a_manifest_is_refused_with_a_usable_message(self):
        """INV-dag-004 forbids floats in canonical bytes so hashes stay replay
        stable. A manifest must say so rather than raising an opaque error."""
        with pytest.raises(ValueError, match="integer"):
            self._manifest(budgets={"per_task": 10.5}).digest()


# ------------------------------------------------------------- reporting


class TestReporting:
    def _five_arms(self, **overrides):
        defaults = {
            "arm-1": (170, 200),
            "arm-2": (140, 200),
            "arm-3": (150, 200),
            "arm-4": (155, 200),
            "arm-5": (168, 200),
        }
        defaults.update(overrides)
        return [arm(name, accepted, attempted)
                for name, (accepted, attempted) in defaults.items()]

    def test_negative_findings_name_arms_that_went_backwards(self):
        arms = self._five_arms(**{"arm-3": (100, 200)})
        findings = negative_findings(arms, baseline_arm="arm-2")
        assert any("arm-3 scored below arm-2" in finding for finding in findings)

    def test_escaped_defects_and_operator_time_are_negative_findings(self):
        arms = [
            arm("arm-2", 140, 200, escaped_defects=1, operator_minutes=10.0),
            arm("arm-3", 150, 200, escaped_defects=4, operator_minutes=30.0),
        ]
        findings = negative_findings(arms, baseline_arm="arm-2")
        assert any("defects escape" in finding for finding in findings)
        assert any("operator time" in finding for finding in findings)

    def test_wide_intervals_are_flagged_as_indecisive(self):
        findings = negative_findings(
            [arm("arm-2", 7, 10), arm("arm-3", 8, 10)], baseline_arm="arm-2"
        )
        assert any("too wide to be decisive" in finding for finding in findings)

    def test_a_clean_sweep_across_five_arms_is_a_reporting_failure(self):
        """AC: no negative findings across five arms is reviewed, not celebrated."""
        arms = self._five_arms()
        failures = reporting_failures(arms, findings=[])
        assert any("reporting failure" in failure for failure in failures)

    def test_a_report_with_findings_is_not_flagged(self):
        assert reporting_failures(self._five_arms(), findings=["arm-3 regressed"]) == []

    def test_an_arm_that_attempted_nothing_is_flagged(self):
        failures = reporting_failures([arm("arm-9", 0, 0)], findings=["something"])
        assert any("attempted no tasks" in failure for failure in failures)

    def test_missing_baseline_is_reported_rather_than_ignored(self):
        findings = negative_findings([arm("arm-3", 10, 20)], baseline_arm="arm-2")
        assert any("missing" in finding for finding in findings)


class TestCostQualityFrontier:
    def test_quality_is_never_reported_without_cost(self):
        arms = [
            arm("arm-2", 140, 200, model_cost=10.0),
            arm("arm-5", 168, 200, model_cost=40.0),
            arm("arm-1", 170, 200, model_cost=100.0, tier="frontier-tier"),
        ]
        frontier = cost_quality_frontier(arms)
        assert all("cost" in point and "quality_ci" in point for point in frontier)
        assert [point["arm"] for point in frontier] == ["arm-2", "arm-5", "arm-1"]
        assert all(point["on_frontier"] for point in frontier)

    def test_a_dominated_arm_is_off_the_frontier(self):
        arms = [
            arm("cheap-good", 170, 200, model_cost=10.0),
            arm("dear-worse", 120, 200, model_cost=90.0),
        ]
        frontier = cost_quality_frontier(arms)
        assert frontier[0]["on_frontier"] is True
        assert frontier[1]["on_frontier"] is False


class TestPublicClaimGate:
    def _arms(self, n=200):
        return [arm(f"arm-{i}", int(n * 0.8), n) for i in range(1, 6)]

    def test_small_sample_may_not_become_a_readme_claim(self):
        """AC: no README claim from a small sample."""
        closure = gap_closure(frontier_quality=0.8, static_open_quality=0.6,
                              adaptive_open_quality=0.7)
        allowed, reason = readme_claim_allowed(self._arms(n=20), closure)
        assert not allowed
        assert "below the pre-registered minimum" in reason

    def test_undefined_gap_closure_may_not_become_a_claim(self):
        closure = gap_closure(frontier_quality=0.7, static_open_quality=0.7,
                              adaptive_open_quality=0.9)
        allowed, reason = readme_claim_allowed(self._arms(), closure)
        assert not allowed
        assert "UNDEFINED_NO_GAP" in reason

    def test_a_qualifying_claim_still_must_carry_its_caveats(self):
        closure = gap_closure(frontier_quality=0.8, static_open_quality=0.6,
                              adaptive_open_quality=0.7)
        allowed, reason = readme_claim_allowed(self._arms(n=1000), closure)
        assert allowed
        assert "N" in reason and "interval" in reason and "caveats" in reason


# --------------------------------------------------- the pre-registration


class TestPreRegistration:
    def test_document_exists_and_is_marked_unrun(self):
        """AC: pre-registration committed BEFORE the first arm runs."""
        assert PREREG.exists(), "the pre-registration must be committed first"
        text = PREREG.read_text()
        assert "no arm has run" in text.lower()

    def test_every_required_section_is_registered(self):
        text = PREREG.read_text().lower()
        for required in ("arms", "gap closure", "non-inferiority margin",
                         "stopping rules", "corpus and strata", "rollback",
                         "rollout ladder", "metrics"):
            assert required in text, f"pre-registration is missing {required!r}"

    def test_the_margin_is_stated_with_a_number_and_a_justification(self):
        text = PREREG.read_text()
        assert re.search(r"\*\*Margin: .*\d+.*\*\*", text), "the margin must be explicit"
        assert "Justification" in text

    def test_all_five_arms_are_registered_by_tier_not_model_name(self):
        text = PREREG.read_text()
        for arm_row in ("| 1 |", "| 2 |", "| 3 |", "| 4 |", "| 5 |"):
            assert arm_row in text
        assert "frontier-tier" in text and "open-tier" in text

    def test_degenerate_cases_are_registered_in_advance(self):
        text = PREREG.read_text().lower()
        assert "undefined" in text
        assert "meaningless" in text
        assert "never clipped to zero" in text

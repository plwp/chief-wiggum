"""Analysis substrate for the dynamic-DAG ablation (chief-wiggum#391).

This module exists to make the experiment's conclusions hard to fudge after the
fact. Three things are mechanised rather than left to discipline:

1. The gap-closure ratio's degenerate cases are handled in advance and named.
   A denominator near zero means the corpus was not discriminative, and no
   ratio is published. A negative numerator means the dynamic process made the
   open model worse, and it is reported as such rather than clipped to zero.
2. Uncertainty is not optional. Point estimates carry a Wilson interval and an
   N, because the existing README already had to caveat an N=20 row at plus or
   minus nineteen points, and a headline without an interval invites exactly
   that mistake again.
3. "No negative findings across five arms" is treated as a reporting failure
   and flagged, not accepted as good news.

Arms are specified by capability tier, never by model name, so the experiment
survives the roster changing under it.

@cw-trace guards INV-dag-016
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .canonical import canonical_json_bytes

# Below this, the frontier/open gap is treated as unmeasurable on this corpus.
DEGENERATE_DENOMINATOR = 1e-9


class GapClosureStatus(StrEnum):
    DEFINED = "DEFINED"
    UNDEFINED_NO_GAP = "UNDEFINED_NO_GAP"
    MEANINGLESS_INVERTED_GAP = "MEANINGLESS_INVERTED_GAP"
    NEGATIVE_REGRESSION = "NEGATIVE_REGRESSION"


class ProtocolViolation(StrEnum):
    VERIFIER_CHANGED = "VERIFIER_CHANGED"
    CORPUS_CHANGED = "CORPUS_CHANGED"
    BUDGET_CHANGED = "BUDGET_CHANGED"
    ENVIRONMENT_CHANGED = "ENVIRONMENT_CHANGED"
    TUNED_ON_CORPUS = "TUNED_ON_CORPUS"


@dataclass(frozen=True)
class Interval:
    """A proportion with its uncertainty. Never report the point alone."""

    successes: int
    n: int
    low: float
    high: float

    @property
    def point(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def width(self) -> float:
        return self.high - self.low

    def render(self) -> str:
        if not self.n:
            return "no data (N=0)"
        return (f"{self.point:.1%} (N={self.n}, 95% CI "
                f"{self.low:.1%} to {self.high:.1%})")

    def to_dict(self) -> dict[str, Any]:
        return {"successes": self.successes, "n": self.n, "point": self.point,
                "ci_low": self.low, "ci_high": self.high}


def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> Interval:
    """Wilson score interval.

    Chosen over the normal approximation because the corpus strata are small,
    and the normal approximation misbehaves badly exactly there: it can produce
    bounds outside [0, 1] and a zero-width interval at 0 or 100 percent.
    """
    if n <= 0:
        return Interval(successes=0, n=0, low=0.0, high=0.0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes {successes} out of range for n={n}")
    proportion = successes / n
    denominator = 1 + z * z / n
    centre = (proportion + z * z / (2 * n)) / denominator
    margin = (z / denominator) * math.sqrt(
        proportion * (1 - proportion) / n + z * z / (4 * n * n)
    )
    return Interval(
        successes=successes,
        n=n,
        low=max(0.0, centre - margin),
        high=min(1.0, centre + margin),
    )


@dataclass(frozen=True)
class GapClosure:
    """The pre-registered ratio, with its degenerate cases named up front."""

    status: GapClosureStatus
    value: float | None
    numerator: float
    denominator: float
    detail: str

    @property
    def publishable(self) -> bool:
        """Only a DEFINED ratio may be quoted as a gap-closure percentage."""
        return self.status is GapClosureStatus.DEFINED

    def to_dict(self) -> dict[str, Any]:
        return {"status": str(self.status), "value": self.value,
                "numerator": self.numerator, "denominator": self.denominator,
                "detail": self.detail}


def gap_closure(
    *,
    frontier_quality: float,
    static_open_quality: float,
    adaptive_open_quality: float,
    epsilon: float = DEGENERATE_DENOMINATOR,
) -> GapClosure:
    """gap_closure = (adaptive_open - static_open) / (frontier - static_open).

    Every degenerate case is decided here, before any data exists, so the
    analysis cannot quietly pick the flattering reading later.
    """
    numerator = adaptive_open_quality - static_open_quality
    denominator = frontier_quality - static_open_quality

    if abs(denominator) <= epsilon:
        return GapClosure(
            status=GapClosureStatus.UNDEFINED_NO_GAP,
            value=None,
            numerator=numerator,
            denominator=denominator,
            detail=("no measurable frontier/open gap on this corpus;"
                    " the corpus was not discriminative and no ratio is published"),
        )
    if denominator < 0:
        return GapClosure(
            status=GapClosureStatus.MEANINGLESS_INVERTED_GAP,
            value=None,
            numerator=numerator,
            denominator=denominator,
            detail=("open-tier beat frontier-tier under the static process;"
                    " the ratio is meaningless, report raw arm differences"),
        )
    if numerator < 0:
        return GapClosure(
            status=GapClosureStatus.NEGATIVE_REGRESSION,
            value=numerator / denominator,
            numerator=numerator,
            denominator=denominator,
            detail=("the dynamic process made the open model worse;"
                    " reported as a negative result and never clipped to zero"),
        )
    return GapClosure(
        status=GapClosureStatus.DEFINED,
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        detail="",
    )


@dataclass(frozen=True)
class NonInferiority:
    """Pre-registered margin. A margin chosen after seeing results is not a margin."""

    margin: float
    justification: str
    registered_at: str = ""

    def assess(self, treatment: Interval, control: Interval) -> dict[str, Any]:
        """Non-inferior when the CI lower bound of the difference clears -margin."""
        difference = treatment.point - control.point
        # Conservative: worst case for the treatment against best for the control.
        worst_case = treatment.low - control.high
        return {
            "margin": self.margin,
            "difference": difference,
            "worst_case_difference": worst_case,
            "non_inferior": worst_case >= -self.margin,
            "justification": self.justification,
        }


@dataclass(frozen=True)
class ArmResult:
    """One pre-registered arm's outcome. Tiers, not model names."""

    arm: str
    model_tier: str
    process: str
    accepted: int
    attempted: int
    escaped_defects: int = 0
    operator_minutes: float = 0.0
    model_cost: float = 0.0
    wall_clock_seconds: float = 0.0
    retries: int = 0
    graph_mutations: int = 0
    escalations: int = 0
    strata: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def quality(self) -> Interval:
        return wilson_interval(self.accepted, self.attempted)

    def stratum_intervals(self) -> dict[str, Interval]:
        return {
            name: wilson_interval(accepted, attempted)
            for name, (accepted, attempted) in sorted(self.strata.items())
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "model_tier": self.model_tier,
            "process": self.process,
            "quality": self.quality.to_dict(),
            "escaped_defects": self.escaped_defects,
            "operator_minutes": self.operator_minutes,
            "model_cost": self.model_cost,
            "wall_clock_seconds": self.wall_clock_seconds,
            "retries": self.retries,
            "graph_mutations": self.graph_mutations,
            "escalations": self.escalations,
            "strata": {name: interval.to_dict()
                       for name, interval in self.stratum_intervals().items()},
        }


@dataclass(frozen=True)
class RunManifest:
    """Everything needed to reproduce one arm. Hashed so it cannot drift."""

    arm: str
    corpus_version: str
    provider_roster: Mapping[str, str]
    seeds: Mapping[str, int]
    budgets: Mapping[str, float]
    verifier_hash: str
    environment: Mapping[str, str]

    def digest(self) -> str:
        """Stable hash of the manifest.

        Values must be integers or strings: INV-dag-004's canonical encoding
        rejects floats so that a hash is replay stable, and a budget expressed
        as 10.5 would otherwise fail with an opaque encoder error deep in the
        call stack. Express money in cents and time in whole seconds.
        """
        try:
            payload = canonical_json_bytes(self.to_dict())
        except ValueError as exc:
            raise ValueError(
                "manifest values must be integer or string; canonical encoding"
                f" rejects floats so hashes stay replay stable (got {exc})"
            ) from exc
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "corpus_version": self.corpus_version,
            "provider_roster": dict(self.provider_roster),
            "seeds": dict(self.seeds),
            "budgets": dict(self.budgets),
            "verifier_hash": self.verifier_hash,
            "environment": dict(self.environment),
        }


def protocol_violations(
    baseline: RunManifest, other: RunManifest
) -> list[ProtocolViolation]:
    """Everything except the varied factor must be fixed. Deviations are recorded."""
    violations = []
    if baseline.verifier_hash != other.verifier_hash:
        violations.append(ProtocolViolation.VERIFIER_CHANGED)
    if baseline.corpus_version != other.corpus_version:
        violations.append(ProtocolViolation.CORPUS_CHANGED)
    if dict(baseline.budgets) != dict(other.budgets):
        violations.append(ProtocolViolation.BUDGET_CHANGED)
    if dict(baseline.environment) != dict(other.environment):
        violations.append(ProtocolViolation.ENVIRONMENT_CHANGED)
    return violations


def cost_quality_frontier(arms: Sequence[ArmResult]) -> list[dict[str, Any]]:
    """The frontier across arms, so quality is never reported without its cost."""
    points = [
        {
            "arm": arm.arm,
            "model_tier": arm.model_tier,
            "process": arm.process,
            "quality": arm.quality.point,
            "quality_ci": [arm.quality.low, arm.quality.high],
            "n": arm.quality.n,
            "cost": arm.model_cost,
        }
        for arm in arms
    ]
    points.sort(key=lambda point: (point["cost"], point["arm"]))
    best_quality = -1.0
    for point in points:
        # On the frontier when nothing cheaper achieved as much quality.
        point["on_frontier"] = point["quality"] > best_quality
        best_quality = max(best_quality, point["quality"])
    return points


def negative_findings(arms: Sequence[ArmResult], baseline_arm: str) -> list[str]:
    """Anything that went the wrong way, stated plainly."""
    baseline = next((arm for arm in arms if arm.arm == baseline_arm), None)
    findings: list[str] = []
    if baseline is None:
        return [f"baseline arm {baseline_arm} is missing from the results"]
    for arm in arms:
        if arm.arm == baseline_arm:
            continue
        if arm.quality.point < baseline.quality.point:
            findings.append(
                f"{arm.arm} scored below {baseline_arm}"
                f" ({arm.quality.render()} vs {baseline.quality.render()})"
            )
        if arm.escaped_defects > baseline.escaped_defects:
            findings.append(
                f"{arm.arm} let more defects escape than {baseline_arm}"
                f" ({arm.escaped_defects} vs {baseline.escaped_defects})"
            )
        if arm.operator_minutes > baseline.operator_minutes:
            findings.append(
                f"{arm.arm} needed more operator time than {baseline_arm}"
                f" ({arm.operator_minutes} vs {baseline.operator_minutes} minutes)"
            )
    for arm in arms:
        if arm.quality.n and arm.quality.width > 0.30:
            findings.append(
                f"{arm.arm} interval is too wide to be decisive ({arm.quality.render()})"
            )
    return findings


def reporting_failures(arms: Sequence[ArmResult], findings: Sequence[str]) -> list[str]:
    """A clean sweep across five arms is a reporting failure, not good news."""
    failures = []
    if len(arms) >= 5 and not findings:
        failures.append(
            "no negative findings across five arms; treat as a reporting failure"
            " and review the analysis before publishing"
        )
    for arm in arms:
        if arm.attempted == 0:
            failures.append(f"{arm.arm} attempted no tasks")
    return failures


def readme_claim_allowed(
    arms: Sequence[ArmResult], closure: GapClosure, *, min_n: int = 100
) -> tuple[bool, str]:
    """No public claim from a small sample, and never without its caveats."""
    if not closure.publishable:
        return False, f"gap closure is {closure.status}: {closure.detail}"
    smallest = min((arm.quality.n for arm in arms), default=0)
    if smallest < min_n:
        return False, (
            f"smallest arm has N={smallest}, below the pre-registered minimum {min_n};"
            " directional only, not a public claim"
        )
    widest = max((arm.quality.width for arm in arms), default=1.0)
    if widest > 0.20:
        return False, f"widest interval is {widest:.1%}; too wide for a headline"
    return True, "claim must carry its N, its interval, and its caveats in the same sentence"

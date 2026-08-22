"""Parallel candidate implementations with verifier-blind promotion.

Two properties make fan-out worth its cost rather than merely expensive:

1. The verifier cannot be written to fit the winner. Its artifact is hashed
   before any candidate output exists, and a hash that moves afterwards is a
   hard failure, not a warning. This is TDD's ordering discipline applied to a
   competitive setting, and the same instinct as the ratchet's rule that a
   contract cannot pass by weakening its definition.
2. The scorer is not told who produced what. Candidate labels are derived from
   artifact content, so swapping which provider produced which candidate cannot
   move the winner, and presentation order is shuffled under a journaled seed so
   position cannot proxy for identity.

Fan-out is policy-gated and defaults to one implementation. Fan-out on every
ticket multiplies cost for no gain on routine work.

@cw-trace guards INV-dag-012 INV-dag-013
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Gate outcomes that do not disqualify a candidate. `inapplicable` is a real
# answer; `error` is not a pass (chief-wiggum#289).
PASSING_GATE_OUTCOMES = frozenset({"pass", "inapplicable"})


class FanOutRefusal(StrEnum):
    NOT_JUSTIFIED = "NOT_JUSTIFIED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"
    VERIFIER_NOT_FROZEN = "VERIFIER_NOT_FROZEN"
    NO_INDEPENDENT_VERIFIER = "NO_INDEPENDENT_VERIFIER"


class PromotionRefusal(StrEnum):
    ALL_CANDIDATES_FAILED = "ALL_CANDIDATES_FAILED"
    VERIFIER_TAMPERED = "VERIFIER_TAMPERED"
    NO_CANDIDATES = "NO_CANDIDATES"


class VerifierTampered(Exception):
    """The verifier changed after candidate output existed. Never recoverable."""


@dataclass(frozen=True)
class CandidatePolicy:
    """When fan-out is justified, how wide, and under what cap."""

    default_width: int = 1
    max_width: int = 3
    risk_classes: tuple[str, ...] = ("high", "critical")
    min_blast_radius: int = 0
    min_contract_density: float = 0.0
    min_historical_failure_rate: float = 0.0
    per_node_budget: float | None = None
    candidate_cost: float = 1.0


@dataclass(frozen=True)
class NodeProfile:
    """Mechanical inputs to the fan-out decision. No model opinion here."""

    risk_class: str = "standard"
    blast_radius: int = 0
    contract_density: float = 0.0
    historical_failure_rate: float = 0.0


@dataclass(frozen=True)
class VerifierFreeze:
    """A content hash of the verifier, taken before candidates are produced."""

    node_id: str
    digest: str
    author_provider: str = ""
    frozen_at: str = ""

    @staticmethod
    def of(node_id: str, artifact: bytes | str, *, author_provider: str = "",
           frozen_at: str = "") -> VerifierFreeze:
        payload = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
        return VerifierFreeze(
            node_id=node_id,
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            author_provider=author_provider,
            frozen_at=frozen_at,
        )

    def verify(self, artifact: bytes | str) -> None:
        """Re-check the artifact. A moved hash invalidates the whole comparison."""
        current = VerifierFreeze.of(self.node_id, artifact)
        if current.digest != self.digest:
            raise VerifierTampered(
                f"verifier for {self.node_id} changed after freeze:"
                f" expected {self.digest}, found {current.digest}"
            )


@dataclass(frozen=True)
class CandidateResult:
    """One candidate's mechanical outcome. `provider` is never given to scoring."""

    candidate_id: str
    provider: str
    artifact_digest: str
    hard_gates: Mapping[str, str] = field(default_factory=dict)
    tests_failed: int = 0
    tests_passed: int = 0
    contract_conformance: float = 0.0
    diff_lines: int = 0
    blast_radius: int = 0
    hotspot_overlap: int = 0
    cost: float = 0.0

    @property
    def blind_label(self) -> str:
        """Derived from CONTENT, so relabelling providers cannot move the winner."""
        return hashlib.sha256(self.artifact_digest.encode("utf-8")).hexdigest()[:12]

    @property
    def failing_gates(self) -> list[str]:
        return sorted(
            name for name, outcome in self.hard_gates.items()
            if str(outcome) not in PASSING_GATE_OUTCOMES
        )

    @property
    def eliminated(self) -> bool:
        """Hard gates run before any qualitative judgement."""
        return bool(self.failing_gates) or self.tests_failed > 0


@dataclass(frozen=True)
class BlindView:
    """What the scorer sees. There is no provider field, by construction."""

    label: str
    tests_failed: int
    tests_passed: int
    contract_conformance: float
    diff_lines: int
    blast_radius: int
    hotspot_overlap: int
    cost: float
    failing_gates: tuple[str, ...]


@dataclass(frozen=True)
class FanOutDecision:
    width: int
    refusal: FanOutRefusal | None = None
    detail: str = ""
    factors: tuple[str, ...] = ()

    @property
    def fans_out(self) -> bool:
        return self.width > 1


@dataclass(frozen=True)
class Promotion:
    winner: str | None
    winner_provider: str | None = None
    superseded: tuple[str, ...] = ()
    eliminated: tuple[str, ...] = ()
    refusal: PromotionRefusal | None = None
    detail: str = ""
    seed: int = 0
    order: tuple[str, ...] = ()

    @property
    def promoted(self) -> bool:
        return self.winner is not None


def decide_fan_out(
    profile: NodeProfile,
    policy: CandidatePolicy | None = None,
    *,
    spent: float = 0.0,
    isolation_available: bool = True,
    isolation_detail: str = "",
    verifier: VerifierFreeze | None = None,
) -> FanOutDecision:
    """Decide width. Default is one; fan-out must be earned and affordable."""
    policy = policy or CandidatePolicy()
    factors = [f"risk={profile.risk_class}", f"blast_radius={profile.blast_radius}"]

    if verifier is None:
        return FanOutDecision(
            width=1, refusal=FanOutRefusal.VERIFIER_NOT_FROZEN,
            detail="the verifier must be frozen before any candidate runs",
            factors=tuple(factors),
        )

    justified = (
        profile.risk_class in policy.risk_classes
        or profile.blast_radius >= policy.min_blast_radius > 0
        or profile.contract_density >= policy.min_contract_density > 0
        or profile.historical_failure_rate >= policy.min_historical_failure_rate > 0
    )
    if not justified:
        return FanOutDecision(
            width=policy.default_width, refusal=FanOutRefusal.NOT_JUSTIFIED,
            detail="routine work does not earn fan-out", factors=tuple(factors),
        )

    if not isolation_available:
        # Cross-contaminated candidates are worse than no candidates, because
        # the contamination can pass the floor (chief-wiggum#376).
        return FanOutDecision(
            width=1, refusal=FanOutRefusal.ISOLATION_UNAVAILABLE,
            detail=isolation_detail or "candidate isolation could not be guaranteed",
            factors=tuple(factors),
        )

    width = policy.max_width
    if policy.per_node_budget is not None:
        affordable = int((policy.per_node_budget - spent) // max(policy.candidate_cost, 1e-9))
        if affordable < 2:
            return FanOutDecision(
                width=1, refusal=FanOutRefusal.BUDGET_EXCEEDED,
                detail=f"budget allows {max(affordable, 0)} candidates",
                factors=tuple(factors + [f"spent={spent}"]),
            )
        width = min(width, affordable)
    return FanOutDecision(width=width, factors=tuple(factors + [f"width={width}"]))


def blind(result: CandidateResult) -> BlindView:
    """Strip identity. The scorer never receives a provider name."""
    return BlindView(
        label=result.blind_label,
        tests_failed=result.tests_failed,
        tests_passed=result.tests_passed,
        contract_conformance=result.contract_conformance,
        diff_lines=result.diff_lines,
        blast_radius=result.blast_radius,
        hotspot_overlap=result.hotspot_overlap,
        cost=result.cost,
        failing_gates=tuple(result.failing_gates),
    )


def presentation_order(views: Sequence[BlindView], seed: int) -> list[BlindView]:
    """Seeded shuffle so position cannot proxy for identity. Replayable."""
    ordered = sorted(views, key=lambda view: view.label)
    random.Random(seed).shuffle(ordered)
    return ordered


def rubric_key(view: BlindView) -> tuple:
    """Mechanical and deterministic. Lower sorts better; label breaks ties."""
    return (
        -round(view.contract_conformance, 6),
        view.tests_failed,
        len(view.failing_gates),
        -view.tests_passed,
        view.blast_radius,
        view.hotspot_overlap,
        view.diff_lines,
        round(view.cost, 6),
        view.label,
    )


def promote(
    results: Sequence[CandidateResult],
    *,
    verifier: VerifierFreeze,
    verifier_artifact: bytes | str,
    seed: int = 0,
) -> Promotion:
    """Eliminate on hard gates, then score blind, then promote exactly one."""
    if not results:
        return Promotion(winner=None, refusal=PromotionRefusal.NO_CANDIDATES,
                         detail="no candidates were produced", seed=seed)
    try:
        verifier.verify(verifier_artifact)
    except VerifierTampered as exc:
        # Fatal: every comparison made against this verifier is void.
        return Promotion(winner=None, refusal=PromotionRefusal.VERIFIER_TAMPERED,
                         detail=str(exc), seed=seed)

    eliminated = [result for result in results if result.eliminated]
    survivors = [result for result in results if not result.eliminated]
    if not survivors:
        # A field of all-failing candidates does not get a best-of-a-bad-lot win.
        return Promotion(
            winner=None,
            refusal=PromotionRefusal.ALL_CANDIDATES_FAILED,
            eliminated=tuple(sorted(result.candidate_id for result in eliminated)),
            detail="every candidate failed a hard gate or a test",
            seed=seed,
        )

    by_label = {result.blind_label: result for result in survivors}
    views = presentation_order([blind(result) for result in survivors], seed)
    ranked = sorted(views, key=rubric_key)
    winner = by_label[ranked[0].label]
    return Promotion(
        winner=winner.candidate_id,
        winner_provider=winner.provider,
        superseded=tuple(
            sorted(result.candidate_id for result in survivors if result is not winner)
        ),
        eliminated=tuple(sorted(result.candidate_id for result in eliminated)),
        seed=seed,
        order=tuple(view.label for view in views),
    )


def promotion_operations(
    node_group: str,
    promotion: Promotion,
    node_of: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Graph operations for a promotion: one winner, losers superseded.

    There is no chimera merge. Taking one file from each candidate produces a
    tree no candidate's tests ever ran against.
    """
    if not promotion.promoted or promotion.winner is None:
        return []
    operations: list[dict[str, Any]] = [
        {
            "op_id": "OPS-promote-001",
            "operation_type": "promote_candidate",
            "target_ref": node_of[promotion.winner],
            "value": {
                "candidate_group_id": node_group,
                "execution_node_id": node_of[promotion.winner],
            },
        }
    ]
    for index, loser in enumerate(promotion.superseded, start=1):
        operations.append(
            {
                "op_id": f"OPS-supersede-{index:03d}",
                "operation_type": "add_relation",
                "target_ref": f"REL-supersede-{index:03d}",
                "value": {
                    "schema_version": "1.0.0",
                    "record_type": "relation",
                    "relation_id": f"REL-supersede-{index:03d}",
                    "source": node_of[promotion.winner],
                    "target": node_of[loser],
                    "kind": "supersedes",
                },
            }
        )
    return operations

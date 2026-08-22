"""Evidence-based cheap-first provider routing with bounded escalation.

Roles stay the interface. A workflow asks for `reviewer` or `implementer`; the
router picks within what the role already permits, so no workflow gains a model
name. Every vendor string lives in config/providers.json, never here.

The property that makes escalation safe is structural, not a convention:
`escalation_triggers` accepts an ObjectiveSignals record that has no field for
self-reported confidence. A model's opinion of its own work cannot reach the
function that decides whether to spend more money, because there is nowhere to
put it. It may still be carried as advisory metadata and studied later.

Determinism: same evidence, same config, same decision. Ranking ends in the
provider name so ties cannot resolve differently between replays.

@cw-trace guards INV-dag-010 INV-dag-011
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Evidence classes the router is allowed to reason over. Every one is something
# the RUN did, never something a model said about itself.
OBJECTIVE_EVIDENCE_CLASSES = frozenset(
    {
        "gate_outcome",
        "worker_lease",
        "budget_consumption",
        "provider_health",
        "path_overlap",
        "unresolved_marker",
        "dependency_readiness",
        "human_decision",
    }
)

DEFAULT_COST_TIER = 5
DEFAULT_CONTEXT_WINDOW = 128_000


class EscalationTrigger(StrEnum):
    """The complete, closed set of reasons to spend more."""

    REPEATED_TOOL_ERRORS = "REPEATED_TOOL_ERRORS"
    FAILING_GATES = "FAILING_GATES"
    STALLED_PROGRESS = "STALLED_PROGRESS"
    PLAN_CHURN = "PLAN_CHURN"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"


class RoutingRefusal(StrEnum):
    NO_CAPABLE_PROVIDER = "NO_CAPABLE_PROVIDER"
    NO_INDEPENDENT_VERIFIER = "NO_INDEPENDENT_VERIFIER"
    FALLBACK_CHAIN_EXHAUSTED = "FALLBACK_CHAIN_EXHAUSTED"
    COST_CEILING_REACHED = "COST_CEILING_REACHED"
    ESCALATION_DEPTH_EXCEEDED = "ESCALATION_DEPTH_EXCEEDED"


@dataclass(frozen=True)
class Capability:
    """Routing metadata for one provider. Additive to config/providers.json."""

    name: str
    enabled: bool = True
    cost_tier: int = DEFAULT_COST_TIER
    context_window: int = DEFAULT_CONTEXT_WINDOW
    latency_class: str = "standard"
    reads_repo: bool = True
    accepts_images: bool = True
    needs_inline_diff: bool = True
    supports_tools: bool = False
    vendor_family: str = ""
    domains: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, name: str, entry: Mapping[str, Any]) -> Capability:
        """Read capability metadata, defaulting so existing configs stay valid."""
        return cls(
            name=name,
            enabled=bool(entry.get("enabled", True)),
            cost_tier=int(entry.get("cost_tier", DEFAULT_COST_TIER)),
            context_window=int(entry.get("context_window", DEFAULT_CONTEXT_WINDOW)),
            latency_class=str(entry.get("latency_class", "standard")),
            reads_repo=bool(entry.get("reads_repo", True)),
            accepts_images=bool(entry.get("accepts_images", True)),
            needs_inline_diff=bool(entry.get("needs_inline_diff", True)),
            supports_tools=bool(entry.get("supports_tools", entry.get("reads_repo", True))),
            vendor_family=str(entry.get("vendor_family", "")) or f"unknown:{name}",
            domains=tuple(entry.get("domains", ()) or ()),
        )


@dataclass(frozen=True)
class TaskDemand:
    """What the work needs. Derived from the node, never from a model."""

    task_type: str = "review"
    risk_class: str = "standard"
    context_tokens: int = 0
    needs_repo_read: bool = False
    needs_images: bool = False
    needs_tools: bool = False
    domain: str = ""
    author_provider: str = ""
    requires_independent_verifier: bool = False


@dataclass(frozen=True)
class ObjectiveSignals:
    """Trajectory facts. There is deliberately no confidence field here.

    Adding one would be the whole vulnerability: it is what lets a model argue
    itself into a more expensive tier.
    """

    failing_gate_attempts: int = 0
    tool_errors: int = 0
    stalled_heartbeats: int = 0
    replan_proposals: int = 0
    retry_budget_remaining: int = 1
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_evidence(cls, records: Iterable[Mapping[str, Any]], **counters: Any) -> ObjectiveSignals:
        """Build from #386 evidence records, ignoring anything not objective."""
        accepted = [
            record
            for record in records
            if str(record.get("evidence_type", "")) in OBJECTIVE_EVIDENCE_CLASSES
        ]
        return cls(evidence_ids=tuple(sorted(str(r.get("evidence_id", "")) for r in accepted)),
                   **counters)


@dataclass(frozen=True)
class EscalationRule:
    failing_gate_attempts: int = 2
    tool_errors: int = 3
    stalled_heartbeats: int = 2
    replan_proposals: int = 3
    max_depth: int = 2


@dataclass(frozen=True)
class Ceilings:
    per_node_cost: float | None = None
    per_graph_cost: float | None = None
    per_node_seconds: float | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """Telemetry is part of the decision. A decision with no explanation is a bug."""

    provider: str | None
    role: str
    alternatives: tuple[str, ...] = ()
    factors: tuple[str, ...] = ()
    trigger: EscalationTrigger | None = None
    depth: int = 0
    refusal: RoutingRefusal | None = None
    detail: str = ""
    evidence_ids: tuple[str, ...] = ()
    estimated_cost_tier: int | None = None

    @property
    def routed(self) -> bool:
        return self.provider is not None

    def explanation(self) -> dict[str, Any]:
        """The record that goes to the factory ledger."""
        return {
            "provider": self.provider,
            "role": self.role,
            "alternatives": list(self.alternatives),
            "factors": list(self.factors),
            "trigger": str(self.trigger) if self.trigger else None,
            "depth": self.depth,
            "refusal": str(self.refusal) if self.refusal else None,
            "detail": self.detail,
            "evidence_ids": list(self.evidence_ids),
            "estimated_cost_tier": self.estimated_cost_tier,
        }


def escalation_triggers(
    signals: ObjectiveSignals, rule: EscalationRule | None = None
) -> list[EscalationTrigger]:
    """Which objective triggers have fired. Order is stable for replay."""
    rule = rule or EscalationRule()
    fired: list[EscalationTrigger] = []
    if signals.failing_gate_attempts >= rule.failing_gate_attempts:
        fired.append(EscalationTrigger.FAILING_GATES)
    if signals.tool_errors >= rule.tool_errors:
        fired.append(EscalationTrigger.REPEATED_TOOL_ERRORS)
    if signals.stalled_heartbeats >= rule.stalled_heartbeats:
        fired.append(EscalationTrigger.STALLED_PROGRESS)
    if signals.replan_proposals >= rule.replan_proposals:
        fired.append(EscalationTrigger.PLAN_CHURN)
    if signals.retry_budget_remaining <= 0:
        fired.append(EscalationTrigger.RETRY_BUDGET_EXHAUSTED)
    return fired


def capable(capability: Capability, demand: TaskDemand) -> tuple[bool, str]:
    """Can this provider do the work at all? Returns the disqualifying reason."""
    if not capability.enabled:
        return False, "disabled"
    if demand.needs_repo_read and not capability.reads_repo:
        return False, "cannot read the repo"
    if demand.needs_images and not capability.accepts_images:
        return False, "cannot accept images"
    if demand.needs_tools and not capability.supports_tools:
        return False, "has no tool loop"
    if demand.context_tokens > capability.context_window:
        return False, f"context {demand.context_tokens} exceeds window {capability.context_window}"
    if demand.domain and capability.domains and demand.domain not in capability.domains:
        return False, f"no declared competence in {demand.domain}"
    return True, ""


def _rank(capability: Capability, demand: TaskDemand, calibration: Mapping[str, float]) -> tuple:
    """Cheap first, then measured record, then name. The name makes it replayable."""
    key = f"{demand.task_type}:{capability.name}"
    return (
        capability.cost_tier,
        -float(calibration.get(key, 0.0)),
        capability.name,
    )


class Router:
    """Picks one provider per attempt, within what the role already permits."""

    def __init__(
        self,
        providers: Mapping[str, Mapping[str, Any]],
        roles: Mapping[str, Mapping[str, Any]],
        *,
        rule: EscalationRule | None = None,
        ceilings: Ceilings | None = None,
        calibration: Mapping[str, float] | None = None,
        static: bool = False,
    ) -> None:
        self._capabilities = {
            name: Capability.from_config(name, entry) for name, entry in providers.items()
        }
        self._roles = roles
        self._rule = rule or EscalationRule()
        self._ceilings = ceilings or Ceilings()
        self._calibration = dict(calibration or {})
        self._static = static

    @property
    def static(self) -> bool:
        return self._static

    def _role_members(self, role: str) -> list[str]:
        entry = self._roles.get(role, {})
        return list(entry.get("required", [])) + list(entry.get("optional", []))

    def _independence_ok(self, candidate: Capability, demand: TaskDemand) -> bool:
        if not demand.requires_independent_verifier or not demand.author_provider:
            return True
        author = self._capabilities.get(demand.author_provider)
        if author is None:
            # An unknown author cannot be proven independent of anything.
            return False
        # Identity is subsumed by family: one provider always has one family,
        # and an unnamed family defaults to a value unique to that provider. A
        # separate `candidate is author` check here would be unreachable, which
        # reads as defence in depth while testing nothing.
        return candidate.vendor_family != author.vendor_family

    def route(
        self,
        role: str,
        demand: TaskDemand | None = None,
        *,
        signals: ObjectiveSignals | None = None,
        spent_cost: float = 0.0,
        unavailable: Sequence[str] = (),
        self_reported_confidence: float | None = None,
    ) -> RoutingDecision:
        """Choose a provider.

        `self_reported_confidence` is accepted so callers may record it, and is
        never consulted. It is not passed to escalation, ranking, or filtering.
        """
        demand = demand or TaskDemand()
        signals = signals or ObjectiveSignals()
        factors: list[str] = [f"task_type={demand.task_type}", f"risk={demand.risk_class}"]
        members = self._role_members(role)
        if not members:
            return RoutingDecision(
                provider=None, role=role, refusal=RoutingRefusal.NO_CAPABLE_PROVIDER,
                detail=f"role {role!r} declares no providers", factors=tuple(factors),
            )

        if self._static:
            # Reproduce today's fixed role membership, provider for provider.
            required = list(self._roles.get(role, {}).get("required", []))
            chosen = next((name for name in required if name not in unavailable), None)
            return RoutingDecision(
                provider=chosen,
                role=role,
                alternatives=tuple(required),
                factors=("static-routing",),
                refusal=None if chosen else RoutingRefusal.FALLBACK_CHAIN_EXHAUSTED,
                detail="" if chosen else "every required provider is unavailable",
                estimated_cost_tier=(
                    self._capabilities[chosen].cost_tier if chosen in self._capabilities else None
                ),
            )

        eligible: list[Capability] = []
        rejected: list[str] = []
        for name in members:
            capability = self._capabilities.get(name)
            if capability is None:
                rejected.append(f"{name}: not declared")
                continue
            if name in unavailable:
                rejected.append(f"{name}: unavailable")
                continue
            ok, why = capable(capability, demand)
            if not ok:
                rejected.append(f"{name}: {why}")
                continue
            if not self._independence_ok(capability, demand):
                rejected.append(f"{name}: not independent of the author")
                continue
            eligible.append(capability)

        if not eligible:
            refusal = RoutingRefusal.NO_CAPABLE_PROVIDER
            if demand.requires_independent_verifier:
                # Failing closed matters most here: the alternative is letting
                # an artifact's author grade its own work.
                refusal = RoutingRefusal.NO_INDEPENDENT_VERIFIER
            elif unavailable:
                refusal = RoutingRefusal.FALLBACK_CHAIN_EXHAUSTED
            return RoutingDecision(
                provider=None, role=role, refusal=refusal,
                alternatives=tuple(members), factors=tuple(factors + rejected),
                detail="; ".join(rejected), evidence_ids=signals.evidence_ids,
            )

        eligible.sort(key=lambda capability: _rank(capability, demand, self._calibration))
        triggers = escalation_triggers(signals, self._rule)
        depth = min(len(triggers), self._rule.max_depth)

        if triggers and len(triggers) > self._rule.max_depth:
            factors.append(f"escalation capped at depth {self._rule.max_depth}")

        index = min(depth, len(eligible) - 1)
        if depth > 0 and index == 0 and len(eligible) == 1:
            return RoutingDecision(
                provider=eligible[0].name, role=role,
                alternatives=tuple(c.name for c in eligible),
                factors=tuple(factors + ["no more expensive provider is available"]),
                trigger=triggers[0], depth=0, evidence_ids=signals.evidence_ids,
                estimated_cost_tier=eligible[0].cost_tier,
            )
        chosen = eligible[index]

        ceiling = self._ceilings.per_node_cost
        if ceiling is not None and spent_cost + chosen.cost_tier > ceiling:
            # Stop rather than escalating into the ceiling.
            return RoutingDecision(
                provider=None, role=role, refusal=RoutingRefusal.COST_CEILING_REACHED,
                alternatives=tuple(c.name for c in eligible),
                factors=tuple(factors + [f"spent={spent_cost}", f"ceiling={ceiling}"]),
                detail=f"{spent_cost} + {chosen.cost_tier} exceeds ceiling {ceiling}",
                trigger=triggers[0] if triggers else None,
                evidence_ids=signals.evidence_ids,
            )

        if depth > 0:
            factors.append(f"escalated to depth {depth}")
        else:
            factors.append("cheapest capable provider")
        return RoutingDecision(
            provider=chosen.name,
            role=role,
            alternatives=tuple(capability.name for capability in eligible),
            factors=tuple(factors + rejected),
            trigger=triggers[0] if triggers else None,
            depth=depth,
            evidence_ids=signals.evidence_ids,
            estimated_cost_tier=chosen.cost_tier,
        )

    def shadow(self, role: str, demand: TaskDemand | None = None, **kwargs: Any) -> dict[str, Any]:
        """What the router WOULD choose, next to what static routing runs."""
        live = self.route(role, demand, **kwargs)
        static_router = Router(
            {name: {"enabled": c.enabled, "cost_tier": c.cost_tier}
             for name, c in self._capabilities.items()},
            self._roles,
            static=True,
        )
        chosen_static = static_router.route(role, demand, **kwargs)
        return {
            "routed": live.explanation(),
            "static": chosen_static.explanation(),
            "differs": live.provider != chosen_static.provider,
        }

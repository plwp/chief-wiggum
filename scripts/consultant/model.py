"""model.py — the mechanical cost-model deriver: pure arithmetic over
cost-inputs.json + a tiered-subscription matrix. No network calls, no AI
consultation, no live pricing lookup (that's the documented step-3 seam,
chief-wiggum#122) — every number here is deterministic given its inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DEFAULT_PRICE_FIELD = "price_monthly_usd"
DEFAULT_TYPICAL_FRACTION = 0.3  # documented assumption: "typical" = 30% of a tier's worst-case cap


@dataclass
class CostShape:
    """The flat nut + per-tenant variable shape (issue #122, deriver bullet a)."""

    flat_monthly: float
    active_tier: str
    tier_fixed_amount: float
    flat_nut: float
    meters: list[dict] = field(default_factory=list)
    largest_uncapped_meter: dict | None = None
    first_step_jump: dict | None = None


@dataclass
class TierEconomics:
    """Worst-case + typical per-tenant variable cost for one pricing tier
    (deriver bullet b). `price` is None if the matrix entry carries no
    recognizable price field — the underwater check is then also None
    (cannot flag underwater without a price to compare against)."""

    tier: str
    price: float | None
    worst_case_cost: float
    worst_case_excluded_meters: list[str]
    typical_cost: float
    typical_excluded_meters: list[str]
    underwater: bool | None


@dataclass
class Breakeven:
    """Break-even paying-tenant count + gross margin for one paying tier
    (deriver bullet c). `breakeven_tenants` is None when the tier never
    recovers its typical cost (margin <= 0 — priced underwater at typical
    usage, not just worst case)."""

    tier: str
    price: float
    typical_cost: float
    gross_margin_per_tenant: float
    gross_margin_pct: float
    breakeven_tenants: int | None


def first_fixed_step_jump(stack_manifest: dict, cost_inputs: dict) -> dict | None:
    """The first (in the stack's own graduation_triggers order) tier transition
    that carries a nonzero tier_fixed dollar amount in the supplied cost-inputs —
    i.e. the first real fixed-cost jump, not just a prose trigger description."""
    tier_fixed = cost_inputs.get("tier_fixed", {}) or {}
    for trig in stack_manifest.get("graduation_triggers", []) or []:
        to_tier = trig.get("to")
        amount = tier_fixed.get(to_tier, 0)
        if amount and amount > 0:
            return {
                "from": trig.get("from"),
                "to": to_tier,
                "trigger": trig.get("trigger"),
                "add": trig.get("add"),
                "monthly_usd": amount,
            }
    return None


def largest_uncapped_meter(meters: list[dict]) -> dict | None:
    """Name the single largest-rate meter with no plan cap (`capped_by: null`) —
    the dangerous line item a plan-limit matrix cannot bound (issue #122's
    "the single uncapped variable cost" callout)."""
    uncapped = [m for m in meters if m.get("capped_by") is None]
    if not uncapped:
        return None
    return max(uncapped, key=lambda m: m.get("rate", 0))


def derive_cost_shape(
    cost_inputs: dict, active_tier: str, stack_manifest: dict | None = None
) -> CostShape:
    flat_monthly = float(cost_inputs.get("flat_monthly", 0))
    tier_fixed = cost_inputs.get("tier_fixed", {}) or {}
    tier_fixed_amount = float(tier_fixed.get(active_tier, 0) or 0)
    meters = cost_inputs.get("meters", []) or []
    return CostShape(
        flat_monthly=flat_monthly,
        active_tier=active_tier,
        tier_fixed_amount=tier_fixed_amount,
        flat_nut=round(flat_monthly + tier_fixed_amount, 4),
        meters=meters,
        largest_uncapped_meter=largest_uncapped_meter(meters),
        first_step_jump=first_fixed_step_jump(stack_manifest, cost_inputs) if stack_manifest else None,
    )


def derive_unit_economics(
    tiers: list[str],
    matrix: dict,
    meters: list[dict],
    price_field: str = DEFAULT_PRICE_FIELD,
    typical_fraction: float = DEFAULT_TYPICAL_FRACTION,
) -> list[TierEconomics]:
    """Worst-case (matrix cap x rate, summed over every capped meter) + typical
    (a documented `typical_fraction` of that same worst case) cost per tier.

    A meter is excluded from BOTH worst-case and typical for a tier when:
      - it's globally uncapped (`capped_by: null` — no plan field bounds it at
        all), or
      - the tier's matrix entry has no such cap field (the meter doesn't apply
        to this product), or
      - the tier's cap for that field is the `-1` unlimited sentinel.
    Excluding rather than guessing keeps every number here honest: an excluded
    meter needs a real usage number (production telemetry) to bound, not a
    fabricated one.
    """
    out: list[TierEconomics] = []
    for tier in tiers:
        caps = matrix.get(tier, {}) or {}
        price = caps.get(price_field)
        if price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None
        worst = 0.0
        typical = 0.0
        worst_excluded: list[str] = []
        typical_excluded: list[str] = []
        for m in meters:
            cap_key = m.get("capped_by")
            rate = float(m.get("rate", 0))
            mid = m.get("id", "?")
            if cap_key is None:
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            if cap_key not in caps:
                continue  # this meter doesn't apply to this product's matrix
            cap = caps[cap_key]
            if cap == -1:
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            worst += float(cap) * rate
            typical += float(cap) * rate * typical_fraction
        underwater = (price < worst) if price is not None else None
        out.append(
            TierEconomics(
                tier=tier,
                price=price,
                worst_case_cost=round(worst, 4),
                worst_case_excluded_meters=worst_excluded,
                typical_cost=round(typical, 4),
                typical_excluded_meters=typical_excluded,
                underwater=underwater,
            )
        )
    return out


def derive_breakeven(flat_nut: float, economics: list[TierEconomics]) -> list[Breakeven]:
    """Break-even paying-tenant count (# of that tier alone needed to cover the
    flat nut) + gross margin, per paying (price > 0) tier. Free ($0 or unpriced)
    tiers contribute no break-even coverage and are skipped — they can't recover
    a flat cost on zero revenue."""
    out: list[Breakeven] = []
    for e in economics:
        if e.price is None or e.price <= 0:
            continue
        margin = round(e.price - e.typical_cost, 4)
        margin_pct = round((margin / e.price) * 100, 2)
        breakeven = math.ceil(flat_nut / margin) if margin > 0 else None
        out.append(
            Breakeven(
                tier=e.tier,
                price=e.price,
                typical_cost=e.typical_cost,
                gross_margin_per_tenant=margin,
                gross_margin_pct=margin_pct,
                breakeven_tenants=breakeven,
            )
        )
    return out

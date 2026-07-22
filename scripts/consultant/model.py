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


def _cap_number(cap: object) -> float | None:
    """Normalize a matrix cap to a number so the unlimited sentinel is caught
    regardless of representation. Accepts int/float and numeric strings (so a
    hand-authored ``"-1"`` reads as unlimited, not ``float("-1")*rate`` negative
    cost); returns None for a non-numeric value the caller must not coerce."""
    if isinstance(cap, bool):  # bool is an int subclass — never a cap
        return None
    if isinstance(cap, (int, float)):
        return float(cap)
    if isinstance(cap, str):
        try:
            return float(cap.strip())
        except ValueError:
            return None
    return None


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
    (deriver bullet b).

    `price` is None if the matrix entry carries no recognizable price field —
    the underwater check is then also None (cannot flag underwater without a
    price to compare against).

    A tier whose matrix leaves a metered line UNCAPPED — either an unlimited
    (`-1`) sentinel on that field, or a globally-uncapped meter (`capped_by:
    null`) — has an UNBOUNDED worst case: a single heavy tenant can cost an
    arbitrary amount. In that case `worst_case_unbounded` is True and
    `worst_case_cost` is None (NOT $0). Presenting an uncapped tier as $0
    worst-case / 100%-margin is exactly the founder-misleads-himself failure
    this deriver exists to prevent, so the unbounded state is explicit and
    never collapses to a definitive-looking number. `typical_cost` is still
    estimated from the BOUNDED meters (labeled typical-not-worst by the
    renderer) but excludes the unbounded meters (no cap to estimate against).
    """

    tier: str
    price: float | None
    worst_case_cost: float | None
    worst_case_excluded_meters: list[str]
    typical_cost: float
    typical_excluded_meters: list[str]
    underwater: bool | None
    worst_case_unbounded: bool = False
    unbounded_meters: list[str] = field(default_factory=list)   # -1 sentinel OR globally-uncapped, on this tier
    no_cap_declared_meters: list[str] = field(default_factory=list)  # declared meter, cap key absent from this tier's matrix


@dataclass
class Breakeven:
    """Break-even paying-tenant count + gross margin for one paying tier
    (deriver bullet c). `breakeven_tenants` is None when the tier never
    recovers its typical cost (margin <= 0 — priced underwater at typical
    usage, not just worst case).

    When the tier's worst case is unbounded (an uncapped metered line), margin
    and break-even are UNCOMPUTABLE, not a definitive number: `unbounded` is
    True and `gross_margin_per_tenant` / `gross_margin_pct` / `breakeven_tenants`
    are all None. A tenant on an uncapped tier can cost arbitrarily much, so no
    finite break-even or margin can be guaranteed."""

    tier: str
    price: float
    typical_cost: float
    gross_margin_per_tenant: float | None
    gross_margin_pct: float | None
    breakeven_tenants: int | None
    unbounded: bool = False


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

    A metered line leaves a tier's worst case UNBOUNDED (not $0) when its plan
    matrix cap is the `-1` unlimited sentinel. This is the dangerous, INVISIBLE
    case: the field IS in the matrix (so the tier looks capped) but the cap is
    unlimited, so the old behavior collapsed it to a $0 line and the tier read as
    underwater=False / 100% margin / a finite break-even — the precise
    founder-misleads-himself failure this deriver exists to prevent. Such meters
    go in `unbounded_meters` and force `worst_case_unbounded=True` with
    `worst_case_cost=None`, surfaced explicitly, never as a safe-looking number.

    A globally-uncapped meter (`capped_by: null`) is excluded from the per-tier
    worst case as before — it is uncapped for EVERY tier equally and is already
    headlined in the cost-shape section as "the largest uncapped meter", so it's
    a named, visible risk rather than an invisible one; it's still listed in each
    tier's excluded-meters note.

    A declared meter whose cap FIELD is simply absent from a tier's matrix goes
    in `no_cap_declared_meters` — it may genuinely not apply to this tier, but a
    missing cap must be shown (via the renderer), never silently omitted in a way
    that improves the economics.

    `typical_cost` is still estimated from the BOUNDED meters (the renderer
    labels it typical-not-worst); unbounded / uncapped / no-cap-declared meters
    are excluded from it too, since there's no cap to base an estimate on.
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
        unbounded_meters: list[str] = []
        no_cap_declared: list[str] = []
        for m in meters:
            cap_key = m.get("capped_by")
            rate = float(m.get("rate", 0))
            mid = m.get("id", "?")
            if cap_key is None:
                # Globally uncapped meter (capped_by: null): the worst case for
                # this meter is literally unbounded — no matrix key limits it — so
                # it forces the same unbounded state as a -1 sentinel, not a silent
                # exclusion that leaves a finite-looking worst-case subtotal.
                unbounded_meters.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            if cap_key not in caps:
                # Cap field not declared for this tier: surface it, never omit silently.
                no_cap_declared.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            cap = _cap_number(caps[cap_key])
            if cap is None:
                # Unparseable cap (e.g. a non-numeric string): cannot bound the
                # meter, so surface it as undeclared rather than coercing it into
                # a definitive (possibly negative) cost.
                no_cap_declared.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            if cap < 0:
                # -1 (or any negative sentinel), incl. the string "-1" -> unlimited.
                unbounded_meters.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            worst += cap * rate
            typical += cap * rate * typical_fraction
        unbounded = bool(unbounded_meters)
        if unbounded:
            worst_case_cost: float | None = None
            underwater: bool | None = None  # cannot flag underwater against an unbounded cost
        else:
            worst_case_cost = round(worst, 4)
            underwater = (price < worst) if price is not None else None
        out.append(
            TierEconomics(
                tier=tier,
                price=price,
                worst_case_cost=worst_case_cost,
                worst_case_excluded_meters=worst_excluded,
                typical_cost=round(typical, 4),
                typical_excluded_meters=typical_excluded,
                underwater=underwater,
                worst_case_unbounded=unbounded,
                unbounded_meters=unbounded_meters,
                no_cap_declared_meters=no_cap_declared,
            )
        )
    return out


def derive_breakeven(flat_nut: float, economics: list[TierEconomics]) -> list[Breakeven]:
    """Break-even paying-tenant count (# of that tier alone needed to cover the
    flat nut) + gross margin, per paying (price > 0) tier. Free ($0 or unpriced)
    tiers contribute no break-even coverage and are skipped — they can't recover
    a flat cost on zero revenue.

    A tier with an unbounded worst case (an uncapped metered line) has no
    guaranteeable margin or break-even: a single heavy tenant can cost an
    arbitrary amount. Such a tier is emitted with `unbounded=True` and
    None margin/break-even, never a definitive number computed from the
    (bounded) typical cost alone."""
    out: list[Breakeven] = []
    for e in economics:
        if e.price is None or e.price <= 0:
            continue
        if e.worst_case_unbounded:
            out.append(
                Breakeven(
                    tier=e.tier,
                    price=e.price,
                    typical_cost=e.typical_cost,
                    gross_margin_per_tenant=None,
                    gross_margin_pct=None,
                    breakeven_tenants=None,
                    unbounded=True,
                )
            )
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

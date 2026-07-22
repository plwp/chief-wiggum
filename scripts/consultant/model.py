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

    A tier's worst case is NOT definitively computable — so `worst_case_cost`,
    `underwater`, and (in derive_breakeven) margin/break-even are ALL suppressed
    to None — whenever ANY meter's cost on that tier cannot be bounded. There are
    two reasons, rendered differently but with the SAME economic effect (never a
    finite worst-case that silently omits a cost we couldn't bound):

    - **unbounded (uncapped by design):** an unlimited (`-1` / `"-1"`) sentinel,
      or a globally-uncapped meter (`capped_by: null`). A single heavy tenant can
      cost an arbitrary amount. Flagged `worst_case_unbounded`, meters in
      `unbounded_meters`.
    - **indeterminate (data gap):** a meter's cap field is absent from this
      tier's matrix, or its cap value is unparseable (e.g. `"lots"`). The cost
      *might* be bounded, but the cost inputs don't say — so we can't compute a
      worst case and must not pretend the meter is free. Flagged
      `worst_case_indeterminate`, meters in `no_cap_declared_meters`.

    Presenting either as a finite $0-inclusive worst-case / 100%-margin is the
    founder-misleads-himself failure this deriver exists to prevent. `typical_cost`
    is still estimated from the BOUNDED meters (labeled typical-not-worst by the
    renderer) but excludes the unbounded/indeterminate meters (no cap to estimate
    against). `price` is None if the matrix carries no recognizable price field —
    the underwater check is then also None regardless of the cost side.
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
    worst_case_indeterminate: bool = False
    no_cap_declared_meters: list[str] = field(default_factory=list)  # cap field absent OR unparseable for this tier

    @property
    def worst_case_computable(self) -> bool:
        """True only when the tier's worst case is a definitive number — neither
        unbounded (uncapped by design) nor indeterminate (data gap)."""
        return not (self.worst_case_unbounded or self.worst_case_indeterminate)


@dataclass
class Breakeven:
    """Break-even paying-tenant count + gross margin for one paying tier
    (deriver bullet c). `breakeven_tenants` is None when the tier never
    recovers its typical cost (margin <= 0 — priced underwater at typical
    usage, not just worst case).

    When the tier's worst case is not definitively computable — unbounded (an
    uncapped metered line) OR indeterminate (a missing/unparseable cap) — margin
    and break-even are UNCOMPUTABLE, not a definitive number:
    `gross_margin_per_tenant` / `gross_margin_pct` / `breakeven_tenants` are all
    None. `unbounded` and `indeterminate` mirror the tier's economics so the
    renderer can distinguish the two while both suppress definitive numbers."""

    tier: str
    price: float
    typical_cost: float
    gross_margin_per_tenant: float | None
    gross_margin_pct: float | None
    breakeven_tenants: int | None
    unbounded: bool = False
    indeterminate: bool = False


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

    If a tier has ANY meter whose cost cannot be bounded, its worst case is NOT
    definitively computable and `worst_case_cost` / `underwater` are suppressed to
    None (and derive_breakeven suppresses margin/break-even) — never a finite
    subtotal from the OTHER meters that silently omits a cost we couldn't bound.
    Two reasons, rendered differently but with the SAME economic suppression:

    - **unbounded (uncapped by design):** the matrix cap is a `-1`/`"-1"` unlimited
      sentinel, or the meter is globally uncapped (`capped_by: null`). A single
      heavy tenant can cost an arbitrary amount. -> `unbounded_meters`,
      `worst_case_unbounded=True`.
    - **indeterminate (data gap):** the meter's cap field is absent from this
      tier's matrix, or its cap value is unparseable (e.g. `"lots"`). The cost
      might be bounded, but the inputs don't say — so we can't compute a worst
      case and must not pretend the meter is free. -> `no_cap_declared_meters`,
      `worst_case_indeterminate=True`.

    `typical_cost` is still estimated from the BOUNDED meters (the renderer labels
    it typical-not-worst); unbounded / indeterminate meters are excluded from it
    too, since there's no cap to base an estimate on.
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
                # it forces the unbounded state, not a silent exclusion that leaves
                # a finite-looking worst-case subtotal.
                unbounded_meters.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            if cap_key not in caps:
                # Cap field not declared for this tier: a data gap. Surface it AND
                # suppress definitive economics (indeterminate) — never omit it in
                # a way that leaves a finite worst-case from the other meters.
                no_cap_declared.append(mid)
                worst_excluded.append(mid)
                typical_excluded.append(mid)
                continue
            cap = _cap_number(caps[cap_key])
            if cap is None:
                # Unparseable cap (e.g. a non-numeric string): cannot bound the
                # meter, so it's a data gap (indeterminate), not a coerced cost and
                # not an unlimited sentinel.
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
        indeterminate = bool(no_cap_declared)
        if unbounded or indeterminate:
            # Either reason makes the worst case non-computable -> suppress the
            # definitive worst-case cost and underwater flag alike.
            worst_case_cost: float | None = None
            underwater: bool | None = None
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
                worst_case_indeterminate=indeterminate,
                no_cap_declared_meters=no_cap_declared,
            )
        )
    return out


def derive_breakeven(flat_nut: float, economics: list[TierEconomics]) -> list[Breakeven]:
    """Break-even paying-tenant count (# of that tier alone needed to cover the
    flat nut) + gross margin, per paying (price > 0) tier. Free ($0 or unpriced)
    tiers contribute no break-even coverage and are skipped — they can't recover
    a flat cost on zero revenue.

    A tier whose worst case is not definitively computable — unbounded (an
    uncapped metered line) OR indeterminate (a missing/unparseable cap) — has no
    guaranteeable margin or break-even, since a cost we couldn't bound might
    swamp the (bounded) typical estimate. Such a tier is emitted with the matching
    flag set and None margin/break-even, never a definitive number."""
    out: list[Breakeven] = []
    for e in economics:
        if e.price is None or e.price <= 0:
            continue
        if not e.worst_case_computable:
            out.append(
                Breakeven(
                    tier=e.tier,
                    price=e.price,
                    typical_cost=e.typical_cost,
                    gross_margin_per_tenant=None,
                    gross_margin_pct=None,
                    breakeven_tenants=None,
                    unbounded=e.worst_case_unbounded,
                    indeterminate=e.worst_case_indeterminate,
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

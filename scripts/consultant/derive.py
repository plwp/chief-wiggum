"""derive.py — orchestrates inputs -> model -> pricing_fit into one JSON-
serializable result dict, consumed by both `--format json` and the
docs/pricing.md renderer. The only non-pure step is reading files; every
number after that is deterministic arithmetic (model.py) or a table lookup
(pricing_fit.py) — no network calls, no AI consultation.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import inputs, model, pricing_fit


def run(
    target_dir: str | Path,
    cost_inputs_path: str | Path | None = None,
    stack_id: str = inputs.DEFAULT_STACK,
    price_field: str = model.DEFAULT_PRICE_FIELD,
    typical_fraction: float = model.DEFAULT_TYPICAL_FRACTION,
    marketplace: bool = False,
    now: str | None = None,
    base: Path = inputs.ROOT,
) -> dict:
    adopted = inputs.load_adopted(target_dir)
    cost_inputs, used_illustrative, source = inputs.load_cost_inputs(cost_inputs_path, stack_id, base)

    try:
        stack_manifest = inputs.load_stack_manifest(stack_id, base)
    except inputs.ConsultantInputError:
        stack_manifest = {}

    active_tier = inputs.active_cost_tier(adopted, stack_manifest) if stack_manifest else "T0"
    cost_shape = model.derive_cost_shape(cost_inputs, active_tier, stack_manifest or None)

    tiers, matrix = inputs.tiered_subscription_binding(adopted)
    economics = model.derive_unit_economics(tiers, matrix, cost_inputs.get("meters", []), price_field, typical_fraction)
    breakeven = model.derive_breakeven(cost_shape.flat_nut, economics)

    table = pricing_fit.load_decision_table()
    shape_label = pricing_fit.classify_cost_shape(cost_inputs.get("meters", []), marketplace=marketplace)
    fit_row = pricing_fit.fit(shape_label, table)
    tactics = pricing_fit.applicable_tactics(table)

    analysis_date = now or cost_inputs.get("as_of") or date.today().isoformat()

    return {
        "analysis_date": analysis_date,
        "stack_id": stack_id,
        "cost_inputs_source": source,
        "used_illustrative_seed": used_illustrative,
        "caveat": cost_inputs.get("$caveat", "") if used_illustrative else "",
        "adopted_patterns": sorted(adopted.keys()),
        "cost_shape": asdict(cost_shape),
        "economics": [asdict(e) for e in economics],
        "breakeven": [asdict(b) for b in breakeven],
        "typical_fraction": typical_fraction,
        "pricing_fit": fit_row,
        "tactics": tactics,
    }

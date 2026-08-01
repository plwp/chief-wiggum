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

    # With no explicit --cost-inputs, prefer the target's OWN operator-authored
    # docs/cost-inputs.json over the stack's illustrative seed (the seed is a
    # last-resort fallback, per its own $comment + the skill workflow).
    if not cost_inputs_path:
        own = Path(target_dir) / inputs.TARGET_COST_INPUTS_REL
        if own.is_file():
            cost_inputs_path = str(own)

    cost_inputs, seed_fallback, source = inputs.load_cost_inputs(cost_inputs_path, stack_id, base)

    # The illustrative caveat is a property of the DATA, not the code path: it
    # surfaces whenever the loaded cost-inputs is illustrative (its own $caveat,
    # or any meter marked provenance:"illustrative") — including when an operator
    # explicitly passes the seed via --cost-inputs. Never let an illustrative
    # rate render as if it were a verified quote just because of how it arrived.
    is_illustrative = seed_fallback or inputs.is_illustrative(cost_inputs)
    caveat = cost_inputs.get("$caveat") or (inputs.DEFAULT_ILLUSTRATIVE_CAVEAT if is_illustrative else "")

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
        "used_illustrative_seed": is_illustrative,
        "caveat": caveat,
        "adopted_patterns": sorted(adopted.keys()),
        "cost_shape": asdict(cost_shape),
        "economics": [asdict(e) for e in economics],
        "breakeven": [asdict(b) for b in breakeven],
        "typical_fraction": typical_fraction,
        "pricing_fit": fit_row,
        "tactics": tactics,
    }

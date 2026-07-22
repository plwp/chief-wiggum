"""pricing_fit.py — cost-shape -> pricing-MODEL-family lookup.

Reads patterns/pricing-models/models.json (see reference.md for the human
spec) and answers "which pricing model family fits this cost shape" — a
family (subscription-or-seat / usage-based-or-subscription / take-rate), never
a specific price point. A specific number needs market-comparable data (the
step-3 live-lookup seam, chief-wiggum#122).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_JSON = ROOT / "patterns" / "pricing-models" / "models.json"

FLAT_COST = "flat-cost"
PER_UNIT_RECURRING = "per-unit-recurring"
MARKETPLACE = "marketplace"


class PricingFitError(Exception):
    pass


def load_decision_table(path: Path = MODELS_JSON) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise PricingFitError(f"pricing-models decision table not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PricingFitError(f"malformed decision table {path}: {exc}") from exc


def classify_cost_shape(meters: list[dict], marketplace: bool = False) -> str:
    """Deterministic cost-shape classification (issue #122 bullet e input).

    `marketplace` is an explicit operator declaration (no adopted pattern in
    the registry yet signals take-rate revenue, so this is never inferred from
    meter shape alone — see patterns/pricing-models/reference.md). Otherwise:
    no nonzero per-tenant meter -> flat-cost; any nonzero meter -> per-unit-recurring.
    """
    if marketplace:
        return MARKETPLACE
    if not meters or all(float(m.get("rate", 0)) == 0 for m in meters):
        return FLAT_COST
    return PER_UNIT_RECURRING


def fit(cost_shape: str, table: dict | None = None) -> dict:
    table = table or load_decision_table()
    for row in table.get("cost_shape_to_model", []):
        if row.get("cost_shape") == cost_shape:
            return row
    raise PricingFitError(f"no decision-table row for cost shape {cost_shape!r}")


def applicable_tactics(table: dict | None = None) -> list[dict]:
    table = table or load_decision_table()
    return table.get("tactics", []) or []

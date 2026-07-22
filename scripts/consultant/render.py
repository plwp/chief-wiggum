"""render.py — renders the deriver's output into docs/pricing.md's 5-section
contract (issue #122 bullet 5): cost shape / unit economics per tier /
break-even+margin / market-comparable floor (the documented UNRESOLVED seam) /
pricing-model fit.
"""

from __future__ import annotations

AUTHORITY_LINE = (
    "Unit economics are derived mechanically from the supplied cost inputs; "
    "illustrative seed numbers are unverified and dated, not a quote."
)

MARKET_SEAM_NOTE = (
    "UNRESOLVED: market-comparable pricing floor needs a live lookup of what "
    "comparable products charge (chief-wiggum#122, rollout step 3 — not built "
    "yet). Do not fabricate competitor prices; this section stays an open "
    "question, gated by scripts/check_unresolved.py, until the live-lookup "
    "step ships."
)


def _fmt_usd(value) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def render_pricing_md(result: dict) -> str:
    lines: list[str] = []
    lines.append("# Pricing — unit economics & pricing-model fit")
    lines.append("")
    lines.append(f"> Analysis date: **{result['analysis_date']}**  ")
    lines.append(f"> Stack: `{result['stack_id']}`, active cost tier: `{result['cost_shape']['active_tier']}`  ")
    lines.append(f"> Cost inputs source: `{result['cost_inputs_source']}`")
    lines.append("")
    if result["used_illustrative_seed"]:
        lines.append(f"> **{result['caveat']}**")
        lines.append("")
    lines.append(f"_{AUTHORITY_LINE}_")
    lines.append("")

    # --- 1. Cost shape --------------------------------------------------
    lines.append("## 1. Cost shape")
    lines.append("")
    cs = result["cost_shape"]
    lines.append(
        f"**Flat nut**: {_fmt_usd(cs['flat_nut'])}/mo "
        f"({_fmt_usd(cs['flat_monthly'])} baseline + {_fmt_usd(cs['tier_fixed_amount'])} "
        f"active-tier fixed cost at `{cs['active_tier']}`) + a per-tenant variable cost "
        "summed over the meters below."
    )
    lines.append("")
    if cs["largest_uncapped_meter"]:
        m = cs["largest_uncapped_meter"]
        lines.append(
            f"**Largest uncapped meter**: `{m['id']}` at {_fmt_usd(m['rate'])}/{m['unit']} "
            f"({m['unit_desc']}) — no plan-limit `matrix` field bounds this meter's usage. "
            "Not the only metered line; put a budget alert on every meter, not just this one."
        )
    else:
        lines.append("**Largest uncapped meter**: none — every declared meter is bounded by a plan-limit `matrix` field.")
    lines.append("")
    if cs["first_step_jump"]:
        j = cs["first_step_jump"]
        lines.append(
            f"**First fixed step-jump**: `{j['from']}` -> `{j['to']}` "
            f"(+{_fmt_usd(j['monthly_usd'])}/mo) — triggered by: {j['trigger']}"
            + (f"; adds {j['add']}" if j.get("add") else "")
            + "."
        )
    else:
        lines.append("**First fixed step-jump**: none identified (no graduation trigger in the stack manifest carries a nonzero fixed cost at the active tier, or no stack manifest was available).")
    lines.append("")
    lines.append("| Meter | Unit | Rate | Capped by | Provenance | Verified |")
    lines.append("|--|--|--|--|--|--|")
    for m in cs["meters"]:
        capped = m.get("capped_by") or "_(uncapped)_"
        lines.append(
            f"| `{m['id']}` | {m['unit']} | {_fmt_usd(m['rate'])} | {capped} | "
            f"{m['provenance']} | {m.get('verified_date', 'n/a')} |"
        )
    lines.append("")

    # --- 2. Unit economics per tier --------------------------------------
    lines.append("## 2. Unit economics per tier")
    lines.append("")
    economics = result["economics"]
    if not economics:
        lines.append(
            "No `tiered-subscription` pattern adopted (or no tiers bound) — "
            "per-tier unit economics need the pattern's `matrix` to bound worst-case "
            "usage. Adopt `tiered-subscription` (`scripts/apply_pattern.py`) first."
        )
    else:
        lines.append(
            f"Worst-case = matrix cap x meter rate, summed over every capped meter. "
            f"Typical assumes {result['typical_fraction'] * 100:.0f}% of that worst case — "
            "a documented assumption, not measured usage; replace with real telemetry once live."
        )
        lines.append("")
        lines.append("| Tier | Price/mo | Worst-case cost | Typical cost | Underwater? |")
        lines.append("|--|--|--|--|--|")
        for e in economics:
            underwater = "n/a (no price bound)" if e["underwater"] is None else ("**YES**" if e["underwater"] else "no")
            lines.append(
                f"| `{e['tier']}` | {_fmt_usd(e['price'])} | {_fmt_usd(e['worst_case_cost'])} | "
                f"{_fmt_usd(e['typical_cost'])} | {underwater} |"
            )
        lines.append("")
        for e in economics:
            excluded = sorted(set(e["worst_case_excluded_meters"]))
            if excluded:
                lines.append(
                    f"- `{e['tier']}`: excluded from worst-case/typical (uncapped, not applicable, "
                    f"or `-1` unlimited in this tier): {', '.join(f'`{x}`' for x in excluded)}"
                )
        lines.append("")

    # --- 3. Break-even & gross margin ------------------------------------
    lines.append("## 3. Break-even & gross margin")
    lines.append("")
    breakeven = result["breakeven"]
    if not breakeven:
        lines.append("No paying (price > 0) tier available to compute break-even against.")
    else:
        lines.append(f"Tenants of that tier alone needed to cover the flat nut ({_fmt_usd(cs['flat_nut'])}/mo), at typical cost.")
        lines.append("")
        lines.append("| Tier | Price/mo | Typical cost | Margin/tenant | Margin % | Break-even tenants |")
        lines.append("|--|--|--|--|--|--|")
        for b in breakeven:
            be = "never (margin <= 0 at typical usage)" if b["breakeven_tenants"] is None else str(b["breakeven_tenants"])
            lines.append(
                f"| `{b['tier']}` | {_fmt_usd(b['price'])} | {_fmt_usd(b['typical_cost'])} | "
                f"{_fmt_usd(b['gross_margin_per_tenant'])} | {_fmt_pct(b['gross_margin_pct'])} | {be} |"
            )
        lines.append("")

    # --- 4. Market-comparable floor ---------------------------------------
    lines.append("## 4. Market-comparable floor")
    lines.append("")
    lines.append(MARKET_SEAM_NOTE)
    lines.append("")

    # --- 5. Pricing-model fit ---------------------------------------------
    lines.append("## 5. Pricing-model fit")
    lines.append("")
    fit = result["pricing_fit"]
    lines.append(f"**Cost shape**: `{fit['cost_shape']}` -> **model family**: `{fit['model_family']}`")
    lines.append("")
    lines.append(f"Rationale: {fit['rationale']}")
    lines.append("")
    if fit.get("never"):
        lines.append(f"Never: {', '.join(fit['never'])}.")
        lines.append("")
    if fit.get("notes"):
        lines.append(f"Notes: {fit['notes']}")
        lines.append("")
    tactics = result.get("tactics") or []
    if tactics:
        lines.append("**Applicable pricing tactics** (patterns/pricing-models/reference.md):")
        lines.append("")
        for t in tactics:
            lines.append(f"- **{t['id']}** — {t['one_liner']} _Guardrail: {t['guardrail']}_")
        lines.append("")

    return "\n".join(lines) + "\n"

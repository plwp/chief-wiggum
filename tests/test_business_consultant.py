"""Tests for scripts/business_consultant.py and scripts/consultant/*
(the /business-consultant cost-model deriver, chief-wiggum#122 steps 1+2).

Fixtures under tests/fixtures/business_consultant/ are synthetic -- controlled
numbers chosen to make the arithmetic assertions exact, never a real product's
adoption record or a real vendor quote.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "business_consultant"
FIXTURE_BASE = FIXTURES / "fixture_base"

sys.path.insert(0, str(SCRIPTS))

from consultant import derive, inputs, model, pricing_fit, render  # noqa: E402

FIXED_NOW = "2026-07-15"


def _fixture_adopted() -> dict:
    return json.loads((FIXTURES / "adopted.json").read_text())["patterns"]


def _fixture_cost_inputs() -> dict:
    """The media-UNCAPPED fixture (media_delivery_hour has capped_by: null) — used
    by the section-1 'largest uncapped meter' tests and the unbounded-state tests."""
    return json.loads((FIXTURES / "cost-inputs.json").read_text())


def _fixture_capped_adopted() -> dict:
    """A fully-matrix-capped adoption record: the matrix caps BOTH meters in
    cost-inputs-capped.json, so every tier's worst-case is finite/computable."""
    return json.loads((FIXTURES / "adopted-capped.json").read_text())["patterns"]


def _fixture_capped_cost_inputs() -> dict:
    """The fully-matrix-capped cost-inputs (every meter capped_by a matrix key)."""
    return json.loads((FIXTURES / "cost-inputs-capped.json").read_text())


def _fixture_stack_manifest() -> dict:
    return json.loads(
        (FIXTURE_BASE / "patterns" / "stacks" / "fixture-stack" / "manifest.json").read_text()
    )


# --- inputs.py ---------------------------------------------------------------


def test_load_adopted_reads_patterns_block(tmp_path):
    d = tmp_path / "docs" / "patterns"
    d.mkdir(parents=True)
    (d / "adopted.json").write_text((FIXTURES / "adopted.json").read_text())
    adopted = inputs.load_adopted(tmp_path)
    assert set(adopted) == {"tiered-subscription", "multi-tenant-isolation"}


def test_load_adopted_empty_when_missing(tmp_path):
    assert inputs.load_adopted(tmp_path) == {}


def test_tiered_subscription_binding_native_json():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    assert tiers == ["free", "pro"]
    assert matrix["pro"]["emails_per_month"] == 5000


def test_tiered_subscription_binding_cli_string_params():
    """A pattern bound via `apply_pattern.py --param matrix=<json>` stores the
    raw CLI string in adopted.json -- the deriver must parse that too."""
    adopted = {
        "tiered-subscription": {
            "parameters": {
                "tiers": '["free", "pro"]',
                "matrix": '{"free": {"price_monthly_usd": 0}, "pro": {"price_monthly_usd": 10}}',
            }
        }
    }
    tiers, matrix = inputs.tiered_subscription_binding(adopted)
    assert tiers == ["free", "pro"]
    assert matrix["pro"]["price_monthly_usd"] == 10


def test_tiered_subscription_binding_absent_pattern():
    assert inputs.tiered_subscription_binding({}) == ([], {})


def test_active_cost_tier_picks_highest_bound_tier():
    manifest = _fixture_stack_manifest()
    tier = inputs.active_cost_tier(_fixture_adopted(), manifest)
    assert tier == "T2"  # tiered-subscription binds at T2 in the fixture stack


def test_active_cost_tier_defaults_to_ladder_floor_when_nothing_bound():
    manifest = _fixture_stack_manifest()
    assert inputs.active_cost_tier({}, manifest) == "T0"


def test_load_cost_inputs_operator_path(tmp_path):
    p = tmp_path / "cost-inputs.json"
    p.write_text((FIXTURES / "cost-inputs.json").read_text())
    data, used_illustrative, source = inputs.load_cost_inputs(str(p), "fixture-stack", FIXTURE_BASE)
    assert used_illustrative is False
    assert source == str(p)
    assert data["flat_monthly"] == 5.0


def test_load_cost_inputs_falls_back_to_illustrative_seed():
    data, used_illustrative, source = inputs.load_cost_inputs(None, "fixture-stack", FIXTURE_BASE)
    assert used_illustrative is True
    assert "cost-inputs.illustrative.json" in source
    assert data["$caveat"]  # the loud caveat is present


def test_load_cost_inputs_missing_operator_file_raises(tmp_path):
    with pytest.raises(inputs.ConsultantInputError):
        inputs.load_cost_inputs(str(tmp_path / "nope.json"), "fixture-stack", FIXTURE_BASE)


def test_load_cost_inputs_unknown_stack_no_seed_raises(tmp_path):
    with pytest.raises(inputs.ConsultantInputError):
        inputs.load_cost_inputs(None, "no-such-stack", FIXTURE_BASE)


# --- model.py: cost shape -----------------------------------------------------


def test_derive_cost_shape_flat_nut_is_flat_monthly_plus_active_tier_fixed():
    ci = _fixture_cost_inputs()
    shape = model.derive_cost_shape(ci, "T2", _fixture_stack_manifest())
    assert shape.flat_monthly == 5.0
    assert shape.tier_fixed_amount == 40.0
    assert shape.flat_nut == 45.0


def test_derive_cost_shape_at_lower_tier_excludes_the_fixed_addon():
    ci = _fixture_cost_inputs()
    shape = model.derive_cost_shape(ci, "T1", _fixture_stack_manifest())
    assert shape.tier_fixed_amount == 0.0
    assert shape.flat_nut == 5.0


def test_largest_uncapped_meter_is_named():
    ci = _fixture_cost_inputs()
    shape = model.derive_cost_shape(ci, "T2", _fixture_stack_manifest())
    assert shape.largest_uncapped_meter is not None
    assert shape.largest_uncapped_meter["id"] == "media_delivery_hour"


def test_no_uncapped_meter_is_none():
    ci = _fixture_cost_inputs()
    ci = dict(ci, meters=[m for m in ci["meters"] if m["capped_by"] is not None])
    shape = model.derive_cost_shape(ci, "T2", _fixture_stack_manifest())
    assert shape.largest_uncapped_meter is None


def test_first_fixed_step_jump_names_the_first_nonzero_transition():
    ci = _fixture_cost_inputs()
    jump = model.first_fixed_step_jump(_fixture_stack_manifest(), ci)
    assert jump == {
        "from": "T1",
        "to": "T2",
        "trigger": "cold starts hurt UX",
        "add": "min-instances=1",
        "monthly_usd": 40.0,
    }


def test_first_fixed_step_jump_none_when_every_transition_is_zero_cost():
    ci = dict(_fixture_cost_inputs(), tier_fixed={"T1": 0.0, "T2": 0.0})
    assert model.first_fixed_step_jump(_fixture_stack_manifest(), ci) is None


# --- model.py: unit economics + underwater detection -------------------------


def test_unit_economics_worst_case_is_matrix_cap_times_rate():
    # Fully-capped fixture: BOTH meters are bounded by the matrix, so worst-case
    # is finite and computable. free = 100 emails * $0.002 + 2 media-hrs * $0.06
    #                               = $0.20 + $0.12 = $0.32
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    free = next(e for e in econ if e.tier == "free")
    assert free.worst_case_unbounded is False
    assert free.worst_case_cost == pytest.approx(0.32)
    assert free.worst_case_excluded_meters == []  # nothing excluded — all capped


def test_unit_economics_underwater_tier_is_flagged():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    free = next(e for e in econ if e.tier == "free")
    # free is priced at $0 but its worst-case cost is $0.32 -> underwater
    assert free.price == 0.0
    assert free.underwater is True


def test_unit_economics_non_underwater_tier():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    pro = next(e for e in econ if e.tier == "pro")
    # pro = 5000 emails * $0.002 + 50 media-hrs * $0.06 = $10 + $3 = $13 worst
    # case, priced at $25 -> not underwater
    assert pro.worst_case_cost == pytest.approx(13.0)
    assert pro.underwater is False


def test_unit_economics_typical_is_documented_fraction_of_worst_case():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"], typical_fraction=0.3)
    pro = next(e for e in econ if e.tier == "pro")
    assert pro.typical_cost == pytest.approx(pro.worst_case_cost * 0.3)


def test_unit_economics_no_price_field_is_not_flagged_underwater():
    # No price field AND a finite (fully-capped) worst case, so underwater is None
    # only because there's no price to compare against — not because of unbounded.
    matrix = {"mystery": {"emails_per_month": 100, "media_hours_per_month": 2}}
    econ = model.derive_unit_economics(["mystery"], matrix, _fixture_capped_cost_inputs()["meters"])
    assert econ[0].price is None
    assert econ[0].worst_case_unbounded is False
    assert econ[0].underwater is None


def test_unit_economics_unlimited_sentinel_is_unbounded_not_zero():
    """P1 regression: a -1 (unlimited) cap on a metered line must make the tier's
    worst-case UNBOUNDED, never a safe-looking $0 / 100%-margin / finite
    break-even. This is the invisible danger the deriver exists to catch."""
    matrix = {"enterprise": {"emails_per_month": -1, "price_monthly_usd": 500}}
    econ = model.derive_unit_economics(["enterprise"], matrix, _fixture_cost_inputs()["meters"])
    e = econ[0]
    assert e.worst_case_unbounded is True
    assert "email_send" in e.unbounded_meters
    assert e.worst_case_cost is None  # NOT 0.0
    assert e.underwater is None  # cannot flag underwater against an unbounded cost


def test_breakeven_unbounded_tier_has_no_finite_margin_or_breakeven():
    """P1 regression: an unbounded (uncapped-metered) paying tier yields no
    definitive margin/break-even — a heavy tenant can cost arbitrarily much."""
    matrix = {"enterprise": {"emails_per_month": -1, "price_monthly_usd": 500}}
    econ = model.derive_unit_economics(["enterprise"], matrix, _fixture_cost_inputs()["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    b = breakeven[0]
    assert b.unbounded is True
    assert b.gross_margin_per_tenant is None
    assert b.gross_margin_pct is None
    assert b.breakeven_tenants is None


def test_render_surfaces_unbounded_tier_never_as_dollar_zero():
    """P1 regression at the render layer: the unbounded state reaches the report
    as UNBOUNDED, not $0.00 / 100%."""
    matrix = {"free": {"emails_per_month": -1, "price_monthly_usd": 0},
              "paid": {"emails_per_month": -1, "price_monthly_usd": 100}}
    result = derive.run(
        target_dir=".", cost_inputs_path=str(FIXTURES / "cost-inputs.json"),
        stack_id="fixture-stack", now=FIXED_NOW, base=FIXTURE_BASE,
    )
    econ = model.derive_unit_economics(["paid"], matrix, _fixture_cost_inputs()["meters"])
    result["economics"] = [asdict(e) for e in econ]
    result["breakeven"] = [asdict(b) for b in model.derive_breakeven(45.0, econ)]
    md = render.render_pricing_md(result)
    assert "UNBOUNDED" in md
    section = md.split("## 2.")[1].split("## 4.")[0]
    assert "$0.00 | 100" not in section  # never a safe-looking 100% margin row


def test_unit_economics_missing_cap_field_is_surfaced_not_silently_omitted():
    """P2 regression: a declared meter whose cap key is absent from a tier's
    matrix must be surfaced (no_cap_declared_meters), never silently contribute
    $0 and vanish from the report."""
    # a matrix that caps NOTHING the email_send meter needs
    matrix = {"pro": {"seats": 5, "price_monthly_usd": 40}}
    meters = [{"id": "email_send", "unit": "send", "rate": 0.002, "unit_desc": "d",
               "capped_by": "emails_per_month", "provenance": "operator-verified",
               "verified_date": "2026-07-01"}]
    econ = model.derive_unit_economics(["pro"], matrix, meters)
    e = econ[0]
    assert "email_send" in e.no_cap_declared_meters
    assert "email_send" in e.worst_case_excluded_meters
    md = render.render_pricing_md({
        **_sample_result(),
        "economics": [asdict(e)],
        "breakeven": [],
    })
    assert "no cap declared" in md
    assert "email_send" in md


def test_capped_by_null_meter_forces_unbounded_not_finite_zero():
    """A genuinely-uncapped meter (capped_by: null, e.g. media delivery) makes
    the TIER's worst-case UNBOUNDED — it must not be silently excluded to leave a
    finite email-only subtotal that reads as safe."""
    matrix = {"pro": {"emails_per_month": 5000, "price_monthly_usd": 25}}
    meters = [
        {"id": "email_send", "unit": "send", "rate": 0.002, "unit_desc": "d",
         "capped_by": "emails_per_month", "provenance": "operator-verified", "verified_date": "2026-07-01"},
        {"id": "media_delivery_hour", "unit": "hour", "rate": 0.06, "unit_desc": "d",
         "capped_by": None, "provenance": "operator-verified", "verified_date": "2026-07-01"},
    ]
    econ = model.derive_unit_economics(["pro"], matrix, meters)
    e = econ[0]
    assert e.worst_case_unbounded is True
    assert "media_delivery_hour" in e.unbounded_meters
    assert e.worst_case_cost is None  # NOT $10 (email-only) and NOT $0
    assert e.underwater is None
    b = model.derive_breakeven(45.0, econ)[0]
    assert b.unbounded is True
    assert b.gross_margin_per_tenant is None
    assert b.breakeven_tenants is None
    md = render.render_pricing_md({**_sample_result(), "economics": [asdict(e)], "breakeven": [asdict(b)]})
    assert "UNBOUNDED" in md


def test_string_minus_one_cap_is_unbounded_not_negative_cost():
    """A matrix cap authored as the STRING "-1" must read as unlimited
    (unbounded), never coerced to float('-1') * rate = a negative 'cost'."""
    matrix = {"pro": {"emails_per_month": "-1", "price_monthly_usd": 25}}
    meters = [{"id": "email_send", "unit": "send", "rate": 0.002, "unit_desc": "d",
               "capped_by": "emails_per_month", "provenance": "operator-verified", "verified_date": "2026-07-01"}]
    econ = model.derive_unit_economics(["pro"], matrix, meters)
    e = econ[0]
    assert e.worst_case_unbounded is True
    assert "email_send" in e.unbounded_meters
    assert e.worst_case_cost is None


def test_unparseable_cap_is_surfaced_as_no_cap_declared_not_coerced():
    """A non-numeric cap value ("lots") can't bound the meter, so it is surfaced
    as no-cap-declared rather than coerced into a definitive (possibly bogus)
    number — and is NOT mistaken for an unlimited sentinel."""
    matrix = {"pro": {"emails_per_month": "lots", "price_monthly_usd": 25}}
    meters = [{"id": "email_send", "unit": "send", "rate": 0.002, "unit_desc": "d",
               "capped_by": "emails_per_month", "provenance": "operator-verified", "verified_date": "2026-07-01"}]
    econ = model.derive_unit_economics(["pro"], matrix, meters)
    e = econ[0]
    assert "email_send" in e.no_cap_declared_meters
    assert "email_send" not in e.unbounded_meters
    assert e.worst_case_unbounded is False
    assert e.worst_case_cost == pytest.approx(0.0)  # no bounded meter contributed
    md = render.render_pricing_md({**_sample_result(), "economics": [asdict(e)], "breakeven": []})
    assert "no cap declared" in md


# --- model.py: break-even + gross margin -------------------------------------


def test_breakeven_count_covers_the_flat_nut():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    shape = model.derive_cost_shape(ci, "T2", _fixture_stack_manifest())
    breakeven = model.derive_breakeven(shape.flat_nut, econ)
    pro = next(b for b in breakeven if b.tier == "pro")
    # pro typical = $13 * 0.3 = $3.90; margin = 25 - 3.90 = $21.10;
    # ceil(45 / 21.10) = 3
    assert pro.gross_margin_per_tenant == pytest.approx(21.10)
    assert pro.breakeven_tenants == 3


def test_breakeven_skips_free_zero_price_tiers():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    assert {b.tier for b in breakeven} == {"pro"}


def test_breakeven_never_when_margin_non_positive():
    # Fully-capped so the tier's worst case is FINITE — this exercises the
    # genuine margin<=0 path (break-even None because unrecoverable at typical
    # usage), NOT the unbounded path.
    matrix = {"paid": {"emails_per_month": 100, "media_hours_per_month": 2, "price_monthly_usd": 0.05}}
    econ = model.derive_unit_economics(["paid"], matrix, _fixture_capped_cost_inputs()["meters"])
    assert econ[0].worst_case_unbounded is False
    breakeven = model.derive_breakeven(45.0, econ)
    assert breakeven[0].unbounded is False
    assert breakeven[0].breakeven_tenants is None  # margin <= 0 at typical usage


def test_gross_margin_pct_is_margin_over_price():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_capped_adopted())
    ci = _fixture_capped_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    pro = next(b for b in breakeven if b.tier == "pro")
    assert pro.gross_margin_pct == pytest.approx(21.10 / 25.0 * 100, rel=1e-6)


# --- pricing_fit.py: decision-table lookup ------------------------------------


def test_classify_cost_shape_flat_when_no_meters():
    assert pricing_fit.classify_cost_shape([]) == pricing_fit.FLAT_COST


def test_classify_cost_shape_flat_when_every_meter_is_zero_rate():
    assert pricing_fit.classify_cost_shape([{"rate": 0}, {"rate": 0}]) == pricing_fit.FLAT_COST


def test_classify_cost_shape_per_unit_recurring_when_a_meter_has_rate():
    assert pricing_fit.classify_cost_shape([{"rate": 0.05}]) == pricing_fit.PER_UNIT_RECURRING


def test_classify_cost_shape_marketplace_is_explicit_declaration():
    assert pricing_fit.classify_cost_shape([{"rate": 0.05}], marketplace=True) == pricing_fit.MARKETPLACE


@pytest.mark.parametrize(
    "shape,expected_family",
    [
        (pricing_fit.FLAT_COST, "subscription-or-seat"),
        (pricing_fit.PER_UNIT_RECURRING, "usage-based-or-subscription"),
        (pricing_fit.MARKETPLACE, "take-rate"),
    ],
)
def test_fit_looks_up_every_cost_shape(shape, expected_family):
    row = pricing_fit.fit(shape)
    assert row["model_family"] == expected_family


def test_fit_per_unit_recurring_rules_out_lifetime_deal():
    row = pricing_fit.fit(pricing_fit.PER_UNIT_RECURRING)
    assert "lifetime-deal" in row["never"]


def test_fit_unknown_shape_raises():
    with pytest.raises(pricing_fit.PricingFitError):
        pricing_fit.fit("not-a-real-shape")


def test_tactics_carry_a_guardrail():
    tactics = pricing_fit.applicable_tactics()
    assert tactics  # the registry ships at least one tactic
    assert all(t.get("guardrail") for t in tactics)


# --- render.py: docs/pricing.md's 5-section contract -------------------------


def _sample_result() -> dict:
    return {
        "analysis_date": FIXED_NOW,
        "stack_id": "fixture-stack",
        "cost_inputs_source": "/fixtures/cost-inputs.json",
        "used_illustrative_seed": False,
        "caveat": "",
        "adopted_patterns": ["tiered-subscription"],
        "cost_shape": {
            "flat_monthly": 5.0,
            "active_tier": "T2",
            "tier_fixed_amount": 40.0,
            "flat_nut": 45.0,
            "meters": _fixture_cost_inputs()["meters"],
            "largest_uncapped_meter": _fixture_cost_inputs()["meters"][0],
            "first_step_jump": {"from": "T1", "to": "T2", "trigger": "cold starts", "add": "min-instances=1", "monthly_usd": 40.0},
        },
        # Built from the real dataclasses via asdict so the sample never drifts
        # from the model's field schema.
        "economics": [
            asdict(model.TierEconomics(
                tier="free", price=0.0, worst_case_cost=0.2,
                worst_case_excluded_meters=["media_delivery_hour"], typical_cost=0.06,
                typical_excluded_meters=["media_delivery_hour"], underwater=True)),
            asdict(model.TierEconomics(
                tier="pro", price=25.0, worst_case_cost=10.0,
                worst_case_excluded_meters=["media_delivery_hour"], typical_cost=3.0,
                typical_excluded_meters=["media_delivery_hour"], underwater=False)),
        ],
        "breakeven": [
            asdict(model.Breakeven(
                tier="pro", price=25.0, typical_cost=3.0, gross_margin_per_tenant=22.0,
                gross_margin_pct=88.0, breakeven_tenants=3)),
        ],
        "typical_fraction": 0.3,
        "pricing_fit": pricing_fit.fit(pricing_fit.PER_UNIT_RECURRING),
        "tactics": pricing_fit.applicable_tactics(),
    }


def test_render_has_all_five_sections():
    md = render.render_pricing_md(_sample_result())
    for heading in [
        "## 1. Cost shape",
        "## 2. Unit economics per tier",
        "## 3. Break-even & gross margin",
        "## 4. Market-comparable floor",
        "## 5. Pricing-model fit",
    ]:
        assert heading in md


def test_render_market_seam_is_an_unresolved_marker():
    md = render.render_pricing_md(_sample_result())
    assert "UNRESOLVED:" in md
    section = md.split("## 4. Market-comparable floor")[1].split("## 5.")[0]
    assert "UNRESOLVED:" in section


def test_render_surfaces_underwater_tier():
    md = render.render_pricing_md(_sample_result())
    assert "**YES**" in md  # the free tier's underwater flag


def test_render_surfaces_illustrative_caveat_when_seed_used():
    result = _sample_result()
    result["used_illustrative_seed"] = True
    result["caveat"] = "TEST CAVEAT TEXT"
    md = render.render_pricing_md(result)
    assert "TEST CAVEAT TEXT" in md


def test_render_authority_line_present():
    md = render.render_pricing_md(_sample_result())
    assert "not a quote" in md


def test_render_names_uncapped_meter_and_step_jump():
    md = render.render_pricing_md(_sample_result())
    assert "media_delivery_hour" in md
    assert "T1` -> `T2`" in md


# --- derive.py: end-to-end orchestration -------------------------------------


def test_derive_run_end_to_end_with_operator_cost_inputs(tmp_path):
    # Fully-capped adopted + cost-inputs, so the per-tier economics are finite
    # and the free tier's underwater flag is legitimately computable.
    docs = tmp_path / "docs" / "patterns"
    docs.mkdir(parents=True)
    (docs / "adopted.json").write_text(json.dumps({"patterns": _fixture_capped_adopted()}))

    result = derive.run(
        target_dir=tmp_path,
        cost_inputs_path=str(FIXTURES / "cost-inputs-capped.json"),
        stack_id="fixture-stack",
        now=FIXED_NOW,
        base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is False
    assert result["cost_shape"]["active_tier"] == "T2"
    assert result["cost_shape"]["flat_nut"] == 45.0
    assert result["pricing_fit"]["cost_shape"] == pricing_fit.PER_UNIT_RECURRING
    free = next(e for e in result["economics"] if e["tier"] == "free")
    assert free["worst_case_unbounded"] is False
    assert free["worst_case_cost"] == pytest.approx(0.32)
    assert free["underwater"] is True


def test_derive_run_falls_back_to_illustrative_seed_and_surfaces_caveat(tmp_path):
    result = derive.run(
        target_dir=tmp_path,  # no adopted.json at all
        cost_inputs_path=None,
        stack_id="fixture-stack",
        now=FIXED_NOW,
        base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is True
    assert "FIXTURE ILLUSTRATIVE" in result["caveat"]
    assert result["economics"] == []  # no tiered-subscription adopted


def test_derive_run_against_the_real_gcp_stack_illustrative_seed(tmp_path):
    """Integration check that the real shipped seed + real stack manifest work
    together end to end, not just the test fixtures."""
    result = derive.run(target_dir=tmp_path, cost_inputs_path=None, stack_id="gcp-serverless-saas", now=FIXED_NOW)
    assert result["used_illustrative_seed"] is True
    assert result["caveat"]
    assert result["cost_shape"]["largest_uncapped_meter"]["id"] == "media_delivery_hour"


def test_derive_run_explicit_seed_still_surfaces_caveat(tmp_path):
    """P2 regression: passing the illustrative seed EXPLICITLY via --cost-inputs
    must still surface the caveat — the caveat is a property of the data, not the
    auto-fallback code path."""
    seed = FIXTURE_BASE / "patterns" / "stacks" / "fixture-stack" / "cost-inputs.illustrative.json"
    result = derive.run(
        target_dir=tmp_path, cost_inputs_path=str(seed),
        stack_id="fixture-stack", now=FIXED_NOW, base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is True
    assert "FIXTURE ILLUSTRATIVE" in result["caveat"]


def test_derive_run_illustrative_meter_provenance_surfaces_caveat(tmp_path):
    """P2 regression: even an operator file with no top-level $caveat surfaces a
    caveat if ANY meter is marked provenance:'illustrative'."""
    ci = tmp_path / "ci.json"
    ci.write_text(json.dumps({
        "flat_monthly": 1.0, "tier_fixed": {},
        "meters": [{"id": "m", "unit": "u", "rate": 0.01, "unit_desc": "d",
                    "capped_by": None, "provenance": "illustrative", "verified_date": "2026-01-01"}],
    }))
    result = derive.run(
        target_dir=tmp_path, cost_inputs_path=str(ci),
        stack_id="fixture-stack", now=FIXED_NOW, base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is True
    assert result["caveat"]  # the default illustrative caveat


def test_derive_run_auto_uses_target_docs_cost_inputs_over_seed(tmp_path):
    """P2 regression: with no --cost-inputs, the target's own
    docs/cost-inputs.json is preferred over the stack's illustrative seed."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "cost-inputs.json").write_text((FIXTURES / "cost-inputs.json").read_text())
    result = derive.run(
        target_dir=tmp_path, cost_inputs_path=None,
        stack_id="fixture-stack", now=FIXED_NOW, base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is False  # operator-verified, not the seed
    assert result["cost_inputs_source"].endswith("docs/cost-inputs.json")


def test_is_illustrative_helper_detects_caveat_and_provenance():
    assert inputs.is_illustrative({"$caveat": "x", "meters": []}) is True
    assert inputs.is_illustrative({"meters": [{"provenance": "illustrative"}]}) is True
    assert inputs.is_illustrative({"meters": [{"provenance": "operator-verified"}]}) is False
    assert inputs.is_illustrative({"meters": []}) is False


# --- schema validation of the illustrative seed ------------------------------


def test_illustrative_seed_conforms_to_schema():
    schema = json.loads((ROOT / "templates" / "cost-inputs-schema.json").read_text())
    data = json.loads(
        (ROOT / "patterns" / "stacks" / "gcp-serverless-saas" / "cost-inputs.illustrative.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(data)


def test_illustrative_seed_every_meter_is_marked_illustrative():
    data = json.loads(
        (ROOT / "patterns" / "stacks" / "gcp-serverless-saas" / "cost-inputs.illustrative.json").read_text()
    )
    assert data.get("$caveat")
    assert all(m["provenance"] == "illustrative" for m in data["meters"])


def test_schema_rejects_missing_required_field():
    schema = json.loads((ROOT / "templates" / "cost-inputs-schema.json").read_text())
    bad = {"flat_monthly": 1.0, "meters": []}  # missing tier_fixed
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)


def test_schema_rejects_unknown_top_level_property():
    schema = json.loads((ROOT / "templates" / "cost-inputs-schema.json").read_text())
    bad = {"flat_monthly": 1.0, "meters": [], "tier_fixed": {}, "unexpected": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)


def test_schema_rejects_meter_missing_capped_by():
    schema = json.loads((ROOT / "templates" / "cost-inputs-schema.json").read_text())
    bad = {
        "flat_monthly": 1.0,
        "tier_fixed": {},
        "meters": [{
            "id": "x", "unit": "u", "rate": 1, "unit_desc": "d",
            "provenance": "illustrative", "verified_date": "2026-01-01",
        }],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)


def test_pricing_models_decision_table_covers_every_shape():
    table = pricing_fit.load_decision_table()
    shapes = {row["cost_shape"] for row in table["cost_shape_to_model"]}
    assert shapes == {pricing_fit.FLAT_COST, pricing_fit.PER_UNIT_RECURRING, pricing_fit.MARKETPLACE}


# --- CLI ----------------------------------------------------------------------


def test_cli_dry_run_writes_nothing_and_prints_text(tmp_path):
    docs = tmp_path / "docs" / "patterns"
    docs.mkdir(parents=True)
    (docs / "adopted.json").write_text(json.dumps({"patterns": _fixture_adopted()}))
    (tmp_path / ".git").mkdir()

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path), "--dry-run"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pricing-model fit" in proc.stdout
    assert not (tmp_path / "docs" / "pricing.md").exists()


def test_cli_json_format(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path),
         "--dry-run", "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["dry_run"] is True
    assert out["result"]["used_illustrative_seed"] is True  # no cost-inputs supplied


def test_cli_writes_docs_pricing_md_by_default(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out_file = tmp_path / "docs" / "pricing.md"
    assert out_file.is_file()
    assert "## 5. Pricing-model fit" in out_file.read_text()


def test_cli_out_override(tmp_path):
    (tmp_path / ".git").mkdir()
    out = tmp_path / "elsewhere.md"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()
    assert not (tmp_path / "docs" / "pricing.md").exists()


def test_cli_missing_cost_inputs_path_is_a_clean_error(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path),
         "--cost-inputs", str(tmp_path / "nope.json")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "not found" in proc.stderr


def test_cli_requires_a_git_repo_for_repo_flag(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "not a git repository" in proc.stderr


def test_cli_marketplace_flag_changes_pricing_fit(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "business_consultant.py"), "--repo", str(tmp_path),
         "--dry-run", "--format", "json", "--marketplace"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["result"]["pricing_fit"]["cost_shape"] == pricing_fit.MARKETPLACE

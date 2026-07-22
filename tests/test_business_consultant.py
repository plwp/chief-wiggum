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
    return json.loads((FIXTURES / "cost-inputs.json").read_text())


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
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    free = next(e for e in econ if e.tier == "free")
    # only email_send applies (media is globally uncapped -> excluded);
    # free cap 100 emails * $0.002/send = $0.20
    assert free.worst_case_cost == pytest.approx(0.20)
    assert "media_delivery_hour" in free.worst_case_excluded_meters


def test_unit_economics_underwater_tier_is_flagged():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    free = next(e for e in econ if e.tier == "free")
    # free tier is priced at $0 but its worst-case cost is $0.20 -> underwater
    assert free.price == 0.0
    assert free.underwater is True


def test_unit_economics_non_underwater_tier():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    pro = next(e for e in econ if e.tier == "pro")
    # pro cap 5000 emails * $0.002 = $10 worst case, priced at $25 -> not underwater
    assert pro.worst_case_cost == pytest.approx(10.0)
    assert pro.underwater is False


def test_unit_economics_typical_is_documented_fraction_of_worst_case():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"], typical_fraction=0.3)
    pro = next(e for e in econ if e.tier == "pro")
    assert pro.typical_cost == pytest.approx(pro.worst_case_cost * 0.3)


def test_unit_economics_no_price_field_is_not_flagged_underwater():
    matrix = {"mystery": {"emails_per_month": 100}}  # no price_monthly_usd
    econ = model.derive_unit_economics(["mystery"], matrix, _fixture_cost_inputs()["meters"])
    assert econ[0].price is None
    assert econ[0].underwater is None


def test_unit_economics_unlimited_sentinel_excluded_from_worst_case():
    matrix = {"enterprise": {"emails_per_month": -1, "price_monthly_usd": 500}}
    econ = model.derive_unit_economics(["enterprise"], matrix, _fixture_cost_inputs()["meters"])
    e = econ[0]
    assert "email_send" in e.worst_case_excluded_meters
    assert e.worst_case_cost == 0.0  # only email_send was capped, and it's -1 here


# --- model.py: break-even + gross margin -------------------------------------


def test_breakeven_count_covers_the_flat_nut():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    shape = model.derive_cost_shape(ci, "T2", _fixture_stack_manifest())
    breakeven = model.derive_breakeven(shape.flat_nut, econ)
    pro = next(b for b in breakeven if b.tier == "pro")
    # margin = 25 - (10 * 0.3) = 22; ceil(45 / 22) = 3
    assert pro.gross_margin_per_tenant == pytest.approx(22.0)
    assert pro.breakeven_tenants == 3


def test_breakeven_skips_free_zero_price_tiers():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    assert {b.tier for b in breakeven} == {"pro"}


def test_breakeven_never_when_margin_non_positive():
    matrix = {"paid": {"emails_per_month": 100, "price_monthly_usd": 0.05}}
    econ = model.derive_unit_economics(["paid"], matrix, _fixture_cost_inputs()["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    assert breakeven[0].breakeven_tenants is None


def test_gross_margin_pct_is_margin_over_price():
    tiers, matrix = inputs.tiered_subscription_binding(_fixture_adopted())
    ci = _fixture_cost_inputs()
    econ = model.derive_unit_economics(tiers, matrix, ci["meters"])
    breakeven = model.derive_breakeven(45.0, econ)
    pro = next(b for b in breakeven if b.tier == "pro")
    assert pro.gross_margin_pct == pytest.approx(22.0 / 25.0 * 100, rel=1e-6)


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
        "economics": [
            {"tier": "free", "price": 0.0, "worst_case_cost": 0.2, "worst_case_excluded_meters": ["media_delivery_hour"],
             "typical_cost": 0.06, "typical_excluded_meters": ["media_delivery_hour"], "underwater": True},
            {"tier": "pro", "price": 25.0, "worst_case_cost": 10.0, "worst_case_excluded_meters": ["media_delivery_hour"],
             "typical_cost": 3.0, "typical_excluded_meters": ["media_delivery_hour"], "underwater": False},
        ],
        "breakeven": [
            {"tier": "pro", "price": 25.0, "typical_cost": 3.0, "gross_margin_per_tenant": 22.0,
             "gross_margin_pct": 88.0, "breakeven_tenants": 3},
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
    docs = tmp_path / "docs" / "patterns"
    docs.mkdir(parents=True)
    (docs / "adopted.json").write_text(json.dumps({"patterns": _fixture_adopted()}))

    result = derive.run(
        target_dir=tmp_path,
        cost_inputs_path=str(FIXTURES / "cost-inputs.json"),
        stack_id="fixture-stack",
        now=FIXED_NOW,
        base=FIXTURE_BASE,
    )
    assert result["used_illustrative_seed"] is False
    assert result["cost_shape"]["active_tier"] == "T2"
    assert result["cost_shape"]["flat_nut"] == 45.0
    assert result["pricing_fit"]["cost_shape"] == pricing_fit.PER_UNIT_RECURRING
    free = next(e for e in result["economics"] if e["tier"] == "free")
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

"""Tests for scripts/plan_bet.py — business-model authoring stage (chief-wiggum#238).

Seeded-defect coverage for every check the script ships: the Fermi viability
gate (arithmetic impossibility BLOCKS unconditionally, --gate or not — the
one deliberate exception to report-only bring-up, Decision 3), premortem
coverage (too few failure modes, uncovered, an unexplained waiver), VPC
pain<->reliever bipartite completeness (uncovered pain, dangling edge, orphan
reliever), e3-value per-actor viability (inapplicable at <=2 actors, missing
flow data, a non-positive net for one actor), canvas/vpc field status<->
evidence consistency (validated/falsified with no asm_ids), the ASM<->canvas
join, goalpost integrity (hand-edit outside `rebaseline`), and a tampered
journal (fail closed, exit 4). Everything runs against a tmp_path portfolio
— never the real ~/.chief-wiggum.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

PLAN_BET = str(SCRIPTS / "plan_bet.py")
BET = str(SCRIPTS / "bet.py")
ASSUME = str(SCRIPTS / "assumption.py")

ENVELOPE = {"cash_cap_usd": 900, "time_cap_hours": 120}
CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-11-01", "direction": "has"},
    ]
}


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(script: str, portfolio: Path, *argv: str):
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)  # belt-and-braces: stay in tmp
    return subprocess.run(
        [sys.executable, script, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _bet(portfolio, *argv):
    return _run(BET, portfolio, *argv)


def _plan(portfolio, *argv):
    return _run(PLAN_BET, portfolio, *argv)


def _asm(portfolio, *argv):
    return _run(ASSUME, portfolio, *argv)


def _create_bet(portfolio, tmp_path, bet_id="b1"):
    env_p = tmp_path / f"{bet_id}-envelope.json"
    crit_p = tmp_path / f"{bet_id}-criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    proc = _bet(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--envelope", str(env_p), "--criteria", str(crit_p))
    assert proc.returncode == 0, proc.stderr
    return proc


def _field(value="x", status="hypothesis", asm_ids=None):
    return {"value": value, "status": status, "asm_ids": asm_ids or []}


VIABLE_FERMI = {
    "msc_usd": 2000, "price_usd": 25, "churn_assumption": 0.05,
    "funnel_assumptions": [0.05, 0.2], "tam": 50000,
}
IMPOSSIBLE_FERMI = {
    "msc_usd": 2000, "price_usd": 25, "churn_assumption": 0.05,
    "funnel_assumptions": [0.05, 0.2], "tam": 100,
}


def _valid_bm(bet_id="b1", **overrides) -> dict:
    bm = {
        "bet_id": bet_id,
        "created": "2026-08-04T00:00:00+00:00",
        "structure": {
            "revenue_model": "subscription",
            "distribution_channel_type": "partner_indirect",
            "value_configuration": "value_chain",
            "actor_types": [
                {"name": "practice", "roles": ["buyer", "payer", "user"]},
                {"name": "platform", "roles": ["platform_intermediary"]},
            ],
        },
        "canvas": {
            "problem": _field("manual spreadsheet accrual schedules"),
            "customer-segment": _field("AU bookkeepers"),
            "value-proposition": _field("auto-post amortization journals"),
            "solution": _field("tag invoices, auto post"),
            "channels": _field("app store"),
            "revenue-streams": _field("monthly subscription"),
            "cost-structure": _field("hosting + support"),
            "key-metrics": _field("signups"),
            "unfair-advantage": _field("AU focus"),
            "pricing": _field("19 usd deposit"),
            "competitive-position": _field("no direct twin"),
        },
        "premortem": [
            {"id": f"PM-{i:03d}", "statement": f"failure mode {i}", "asm_id": None,
             "waived": True, "waiver_reason": "covered elsewhere"}
            for i in range(1, 6)
        ],
        "vpc": {
            "pains": [_dict_with_id("PAIN-001", "manual accrual tracking")],
            "pain_relievers": [
                {**_field("auto-post journals"), "id": "REL-001", "addresses_pain_ids": ["PAIN-001"]},
            ],
        },
        "fermi_inputs": dict(VIABLE_FERMI),
    }
    for k, v in overrides.items():
        bm[k] = v
    return bm


def _dict_with_id(id_, value):
    d = _field(value)
    d["id"] = id_
    return d


def _write(tmp_path, name, obj) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


# ---- schema validation ----------------------------------------------------------


def test_valid_business_model_authors_cleanly(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (portfolio / "bets" / "b1" / "business-model.json").is_file()


def test_schema_invalid_business_model_refused(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bad = _valid_bm()
    del bad["structure"]["revenue_model"]  # required field missing
    f = _write(tmp_path, "bm.json", bad)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout
    assert not (portfolio / "bets" / "b1" / "business-model.json").exists()


def test_wrong_bet_id_in_file_is_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm(bet_id="other-bet")
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 2
    assert "does not match" in proc.stderr


def test_author_on_nonexistent_bet_is_usage_error(portfolio, tmp_path):
    f = _write(tmp_path, "bm.json", _valid_bm())
    proc = _plan(portfolio, "author", "no-such-bet", "--file", f)
    assert proc.returncode == 2


# ---- Fermi viability gate (Decision 3 — unconditional hard block) ---------------


def test_fermi_arithmetic_impossibility_blocks_without_gate(portfolio, tmp_path):
    """The whole point of Decision 3: this blocks WITHOUT --gate."""
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm(fermi_inputs=dict(IMPOSSIBLE_FERMI))
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 1, proc.stdout
    assert "fermi: ARITHMETIC IMPOSSIBILITY" in proc.stdout
    assert "BLOCKED" in proc.stdout
    assert not (portfolio / "bets" / "b1" / "business-model.json").exists()


def test_fermi_viable_numbers_pass(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm(fermi_inputs=dict(VIABLE_FERMI))
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fermi:" not in proc.stdout


def test_fermi_absent_is_skipped_not_blocked(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    del bm["fermi_inputs"]
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped: fermi_inputs not declared" in proc.stdout


def test_fermi_certain_churn_is_schema_invalid(portfolio, tmp_path):
    """churn_assumption must be < 1 — certain churn makes steady state
    unreachable by construction, caught at the schema layer."""
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm(fermi_inputs={**VIABLE_FERMI, "churn_assumption": 1.0})
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout and "schema" in proc.stdout


def test_fermi_blocks_on_rebaseline_too(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f_good = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f_good).returncode == 0
    bad = _valid_bm(fermi_inputs=dict(IMPOSSIBLE_FERMI))
    f_bad = _write(tmp_path, "bm-bad.json", bad)
    proc = _plan(portfolio, "rebaseline", "b1", "--file", f_bad, "--reason", "narrow TAM")
    assert proc.returncode == 1
    assert "fermi: ARITHMETIC IMPOSSIBILITY" in proc.stdout
    on_disk = json.loads((portfolio / "bets" / "b1" / "business-model.json").read_text())
    assert on_disk["fermi_inputs"]["tam"] == VIABLE_FERMI["tam"], "a REFUSED rebaseline must not touch the file"


def test_fermi_check_command_blocks_unconditionally(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    # Hand-write an impossible business-model.json directly (bypassing author/
    # rebaseline) so `check` sees a Fermi-impossible file on disk.
    bm = json.loads((portfolio / "bets" / "b1" / "business-model.json").read_text())
    bm["fermi_inputs"] = dict(IMPOSSIBLE_FERMI)
    (portfolio / "bets" / "b1" / "business-model.json").write_text(json.dumps(bm))
    proc = _plan(portfolio, "check", "b1")
    assert proc.returncode == 1
    assert "fermi: ARITHMETIC IMPOSSIBILITY" in proc.stdout


# ---- premortem coverage (Decision 4) --------------------------------------------


def test_premortem_below_floor_is_report_only_finding(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm(premortem=[
        {"id": "PM-001", "statement": "x", "asm_id": None, "waived": True, "waiver_reason": "r"},
    ])
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "need >= 5" in proc.stdout


def test_premortem_uncovered_entry_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["premortem"][0] = {"id": "PM-001", "statement": "no demand", "asm_id": None, "waived": False}
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "PM-001: uncovered" in proc.stdout


def test_premortem_waived_without_reason_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["premortem"][0] = {"id": "PM-001", "statement": "no demand", "asm_id": None,
                           "waived": True, "waiver_reason": None}
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "waived with no waiver_reason" in proc.stdout


def test_premortem_meeting_floor_with_real_reasons_is_clean(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "uncovered" not in proc.stdout
    assert "need >=" not in proc.stdout


# ---- VPC pain<->reliever bipartite completeness ---------------------------------


def test_vpc_uncovered_pain_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["vpc"]["pains"].append(_dict_with_id("PAIN-002", "second pain"))
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "PAIN-002: uncovered" in proc.stdout


def test_vpc_dangling_reliever_edge_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["vpc"]["pain_relievers"][0]["addresses_pain_ids"] = ["PAIN-999"]
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "dangling edge" in proc.stdout
    assert "PAIN-001: uncovered" in proc.stdout  # the real pain is now unaddressed


def test_vpc_orphan_reliever_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["vpc"]["pain_relievers"].append(
        {**_field("unrelated feature"), "id": "REL-002", "addresses_pain_ids": []}
    )
    f = _write(tmp_path, "bm.json", bm)
    # addresses_pain_ids has minItems:1 in the schema, so an empty list is
    # itself a schema violation — this seed exercises that path.
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout


def test_vpc_absent_is_skipped(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    del bm["vpc"]
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "skipped: no vpc block" in proc.stdout


# ---- e3-value per-actor viability (Decision 5) ----------------------------------


def test_e3_value_inapplicable_at_two_actors(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())  # 2 actors declared
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "e3-value" not in proc.stdout


def test_e3_value_triggers_at_three_actors_missing_flow(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["structure"]["actor_types"].append({"name": "end users", "roles": ["user"]})
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "e3-value: actor 'end users' has no revenue/cost flow" in proc.stdout


def test_e3_value_flags_nonpositive_actor_net(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["structure"]["actor_types"].append({"name": "end users", "roles": ["user"]})
    bm["e3_value"] = {"actor_flows": [
        {"actor": "practice", "revenue_usd": 100, "cost_usd": 20},
        {"actor": "platform", "revenue_usd": 10, "cost_usd": 10},
        {"actor": "end users", "revenue_usd": 0, "cost_usd": 5},
    ]}
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "actor 'platform' nets $0.00" in proc.stdout
    assert "actor 'end users' nets $-5.00" in proc.stdout
    assert "actor 'practice' nets" not in proc.stdout  # practice nets $80 > 0, not a finding


def test_e3_value_all_positive_is_clean(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["structure"]["actor_types"].append({"name": "end users", "roles": ["user"]})
    bm["e3_value"] = {"actor_flows": [
        {"actor": "practice", "revenue_usd": 100, "cost_usd": 20},
        {"actor": "platform", "revenue_usd": 10, "cost_usd": 2},
        {"actor": "end users", "revenue_usd": 5, "cost_usd": 1},
    ]}
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "e3-value" not in proc.stdout


# ---- canvas/vpc field status<->evidence consistency -----------------------------


def test_validated_field_without_asm_ids_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["canvas"]["pricing"] = _field("19 usd", status="validated", asm_ids=[])
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "pricing: status 'validated' with no asm_ids" in proc.stdout


def test_validated_field_with_asm_ids_is_clean_shape(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["canvas"]["pricing"] = _field("19 usd", status="validated", asm_ids=["ASM-001"])
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert proc.returncode == 0
    assert "status 'validated' with no asm_ids" not in proc.stdout
    # (still findable as a dangling asm_id — see the join tests below)


# ---- ASM<->canvas join -----------------------------------------------------------


def test_asm_join_flags_unmatched_depends_on_element(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    proc = _asm(portfolio, "add", "b1", "--statement",
                "at least 5% of AU bookkeepers will join the waitlist",
                "--source", "premortem", "--element", "not-a-real-field")
    assert proc.returncode == 0
    check = _plan(portfolio, "check", "b1")
    assert "depends_on_element 'not-a-real-field' does not match" in check.stdout


def test_asm_join_accepts_grounded_element_names(portfolio, tmp_path):
    """The exact ids real bets already used (value-proposition, customer-segment,
    pricing, competitive-position) must resolve cleanly."""
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    for elem in ("value-proposition", "customer-segment", "pricing", "competitive-position"):
        proc = _asm(portfolio, "add", "b1", "--statement",
                    "at least 5% of AU bookkeepers will join the waitlist",
                    "--source", "premortem", "--element", elem)
        assert proc.returncode == 0, proc.stderr
    check = _plan(portfolio, "check", "b1")
    assert "does not match" not in check.stdout


def test_asm_join_flags_dangling_cited_asm_id(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    # A real (but unrelated) assumption must exist for assumptions.json to be
    # present at all -- otherwise the whole join is `skipped:`, tested below.
    real = _asm(portfolio, "add", "b1", "--statement",
                "at least 5% of AU bookkeepers will join the waitlist",
                "--source", "premortem", "--element", "customer-segment")
    assert real.returncode == 0, real.stderr
    bm = _valid_bm()
    bm["canvas"]["pricing"] = _field("19 usd", status="hypothesis", asm_ids=["ASM-999"])
    f = _write(tmp_path, "bm.json", bm)
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    check = _plan(portfolio, "check", "b1")
    assert "cites 'ASM-999', no such assumption" in check.stdout


def test_asm_join_skipped_without_assumptions_file(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    proc = _plan(portfolio, "author", "b1", "--file", f)
    assert "skipped: no assumptions.json" in proc.stdout


# ---- four-state outcome (pass | findings | inapplicable | error, #289) ---------


def test_check_inapplicable_when_never_authored(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    proc = _plan(portfolio, "check", "b1")
    assert proc.returncode == 0
    assert "outcome=inapplicable" in proc.stdout


def test_check_error_on_unparsable_file(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    (portfolio / "bets" / "b1" / "business-model.json").write_text("{not json")
    proc = _plan(portfolio, "check", "b1")
    assert "outcome=error" in proc.stdout
    assert proc.returncode == 0  # error only fails --gate, matching check_traceability.py


def test_check_error_gated_fails(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    (portfolio / "bets" / "b1" / "business-model.json").write_text("{not json")
    proc = _plan(portfolio, "check", "b1", "--gate")
    assert proc.returncode == 1
    assert "outcome=error" in proc.stdout


def test_check_pass_on_clean_authored_model(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    proc = _plan(portfolio, "check", "b1")
    assert proc.returncode == 0
    # premortem entries are all waived with reasons, vpc is complete, e3 is
    # inapplicable (2 actors), no assumptions.json -> everything real is clean.
    assert "outcome=pass" in proc.stdout


def test_check_findings_when_something_real_is_wrong(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["vpc"]["pains"].append(_dict_with_id("PAIN-002", "second pain"))  # uncovered
    f = _write(tmp_path, "bm.json", bm)
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    proc = _plan(portfolio, "check", "b1")
    assert "outcome=findings" in proc.stdout


# ---- goalpost integrity ----------------------------------------------------------


def test_hand_edit_outside_rebaseline_flagged_on_check(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    bm_path = portfolio / "bets" / "b1" / "business-model.json"
    bm = json.loads(bm_path.read_text())
    bm["canvas"]["pricing"]["value"] = "hand-edited, no journal record"
    bm_path.write_text(json.dumps(bm))
    proc = _plan(portfolio, "check", "b1")
    assert "goalposts moved" in proc.stdout
    assert "outcome=findings" in proc.stdout


def test_rebaseline_requires_reason(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    proc = _plan(portfolio, "rebaseline", "b1", "--file", f)
    assert proc.returncode != 0
    assert "--reason" in proc.stderr or "required" in proc.stderr


def test_rebaseline_journals_old_and_new_hash(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    bm2 = _valid_bm()
    bm2["canvas"]["pricing"] = _field("29 usd deposit")
    f2 = _write(tmp_path, "bm2.json", bm2)
    proc = _plan(portfolio, "rebaseline", "b1", "--file", f2, "--reason", "pricing hypothesis update")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    journal_lines = (portfolio / "journal.jsonl").read_text().splitlines()
    rebaseline_recs = [json.loads(line) for line in journal_lines
                        if json.loads(line).get("event") == "business-model-rebaseline"]
    assert len(rebaseline_recs) == 1
    d = rebaseline_recs[0]["details"]
    assert d["old_business_model_hash"] and d["new_business_model_hash"]
    assert d["old_business_model_hash"] != d["new_business_model_hash"]
    assert d["reason"] == "pricing hypothesis update"
    # check now passes clean against the NEW baseline
    check = _plan(portfolio, "check", "b1")
    assert "goalposts moved" not in check.stdout


def test_rebaseline_before_author_is_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    proc = _plan(portfolio, "rebaseline", "b1", "--file", f, "--reason", "x")
    assert proc.returncode == 2


# ---- --gate promotes non-Fermi findings to blocking -----------------------------


def test_gate_flag_blocks_on_real_findings(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    bm = _valid_bm()
    bm["vpc"]["pains"].append(_dict_with_id("PAIN-002", "second pain"))
    f = _write(tmp_path, "bm.json", bm)
    proc = _plan(portfolio, "author", "b1", "--file", f, "--gate")
    assert proc.returncode == 1
    assert "REFUSED (--gate)" in proc.stdout
    assert not (portfolio / "bets" / "b1" / "business-model.json").exists()


def test_gate_flag_does_not_block_on_skipped_findings(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())  # no assumptions.json -> skipped: join
    proc = _plan(portfolio, "author", "b1", "--file", f, "--gate")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- tampered journal fails closed -----------------------------------------------


def test_tampered_journal_fails_closed_exit_4(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    f = _write(tmp_path, "bm.json", _valid_bm())
    assert _plan(portfolio, "author", "b1", "--file", f).returncode == 0
    jpath = portfolio / "journal.jsonl"
    lines = jpath.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["details"]["business_model_hash"] = "forged"
    lines[-1] = json.dumps(rec, sort_keys=True)
    jpath.write_text("\n".join(lines) + "\n")
    for cmd in (["check", "b1"], ["rebaseline", "b1", "--file", f, "--reason", "x"]):
        proc = _plan(portfolio, *cmd)
        assert proc.returncode == 4, f"{cmd}: {proc.stdout}{proc.stderr}"
        assert "tamper" in proc.stderr

"""Tests for scripts/assumption.py — validation experiments (chief-wiggum#236).

Seeded-defect coverage for every gate the script ships (all report-only by
default per docs/gate-rollout.md): a non-XYZ hypothesis, a generic population,
an opinion-verb behavior, a vanity metric as success criterion, a verdict with
no pre-registered card (the evasion-omission seed), a verdict against an
altered threshold (hash mismatch), an interview-only ASM capped at opinion
strength, the building-transition evidence floor, orphan/uncovered/dangling
traceability defects, the pivot dependency rule, and a tampered journal
(fail closed, exit 4). Everything runs against a tmp_path portfolio — never
the real ~/.chief-wiggum.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ratchet  # noqa: E402

ASSUME = str(SCRIPTS / "assumption.py")
BET = str(SCRIPTS / "bet.py")

ENVELOPE = {"cash_cap_usd": 900, "time_cap_hours": 120}
CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-11-01", "direction": "has"},
    ]
}

GOOD_XYZ = ("at least 5% of AU dog trainers who run group classes will leave "
            "an email on the waitlist page")
MONEY_XYZ = ("at least 3% of waitlisted AU dog trainers will pay a 20 USD "
             "deposit for early access")


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


def _asm(portfolio, *argv):
    return _run(ASSUME, portfolio, *argv)


def _create_bet(portfolio, tmp_path, bet_id="b1"):
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    proc = _bet(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--envelope", str(env_p), "--criteria", str(crit_p))
    assert proc.returncode == 0, proc.stderr
    return proc


def _add(portfolio, bet_id="b1", statement=GOOD_XYZ, source="premortem",
         element=None, *extra):
    argv = ["add", bet_id, "--statement", statement, "--source", source]
    if element:
        argv += ["--element", element]
    return _asm(portfolio, *argv, *extra)


def _card(portfolio, bet_id="b1", asm="ASM-001", method="landing-page-smoke-test",
          metric="visitor_to_signup_rate_pct", comparator=">=", value="5",
          sample_min="100", strength="2", extra=()):
    return _asm(portfolio, "card", bet_id, "--asm", asm, "--method", method,
                "--metric", metric, "--comparator", comparator, "--value", value,
                "--sample-min", sample_min, "--evidence-strength", strength, *extra)


def _journal(portfolio: Path) -> list[dict]:
    return ratchet.load_journal(SimpleNamespace(journal=portfolio / "journal.jsonl"))


def _assumptions(portfolio: Path, bet_id="b1") -> list[dict]:
    p = portfolio / "bets" / bet_id / "assumptions.json"
    return json.loads(p.read_text())["assumptions"]


def _cards(portfolio: Path, bet_id="b1") -> list[dict]:
    p = portfolio / "bets" / bet_id / "test-cards.json"
    return json.loads(p.read_text())["cards"]


def _state(portfolio: Path, bet_id: str) -> str:
    return json.loads((portfolio / "bets" / bet_id / "bet.json").read_text())["state"]


def _retro(portfolio: Path, bet_id: str) -> None:
    (portfolio / "bets" / bet_id / "retrospective.md").write_text(
        "# retro\n\nEnvelope respected; criteria evaluated honestly against the "
        "old thesis; the changed element invalidates dependent validation.\n"
    )


# ---- assumption ledger + XYZ falsifiability lint --------------------------------


def test_add_assigns_stable_ids_and_journals(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    p1 = _add(portfolio, element="customer-segment")
    assert p1.returncode == 0, p1.stderr
    p2 = _add(portfolio, statement=MONEY_XYZ, source="financial_model", element="pricing")
    assert p2.returncode == 0, p2.stderr
    asms = _assumptions(portfolio)
    assert [a["id"] for a in asms] == ["ASM-001", "ASM-002"]
    assert all(a["status"] == "untested" for a in asms)
    assert asms[0]["source"] == "premortem" and asms[1]["source"] == "financial_model"
    assert asms[0]["depends_on_element"] == "customer-segment"
    events = [r["event"] for r in _journal(portfolio)]
    assert events.count("asm-add") == 2


# ---- seeded defect: non-XYZ hypothesis ------------------------------------------


def test_non_xyz_hypothesis_flagged_report_only_and_gated(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    proc = _add(portfolio, statement="people will love this product", source="canvas")
    assert proc.returncode == 0  # report-only default: finding printed, added
    assert "not in XYZ form" in proc.stdout
    gated = _asm(portfolio, "add", "b1", "--statement", "this will be huge",
                 "--source", "canvas", "--gate")
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    assert len(_assumptions(portfolio)) == 1  # gated add NOT recorded


def test_generic_population_and_opinion_behavior_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    generic = _asm(portfolio, "add", "b1", "--statement",
                   "at least 10% of people will sign up in the first week",
                   "--source", "canvas", "--gate")
    assert generic.returncode == 1
    assert "not concrete" in generic.stdout
    opinion = _asm(portfolio, "add", "b1", "--statement",
                   "at least 10% of AU dog trainers will love the dashboard",
                   "--source", "canvas", "--gate")
    assert opinion.returncode == 1
    assert "opinion, not a measurable behavior" in opinion.stdout


# ---- test cards: pre-registration -----------------------------------------------


def test_card_hashes_threshold_into_journal_and_moves_asm_to_testing(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    proc = _card(portfolio)
    assert proc.returncode == 0, proc.stderr
    assert "hashed into the journal" in proc.stdout
    rec = [r for r in _journal(portfolio) if r["event"] == "card-create"][-1]
    assert rec["details"]["card_id"] == "TC-001"
    assert rec["details"]["threshold_hash"]
    assert _assumptions(portfolio)[0]["status"] == "testing"


def test_card_for_unknown_asm_is_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    proc = _card(portfolio, asm="ASM-999")
    assert proc.returncode == 2
    assert "no assumption ASM-999" in proc.stderr


def test_evidence_strength_out_of_enum_is_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    proc = _card(portfolio, strength="6")
    assert proc.returncode == 2
    assert "the enum is fixed" in proc.stderr


# ---- seeded defect: vanity metric -----------------------------------------------


def test_vanity_metric_rejected_as_success_criterion(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    proc = _card(portfolio, metric="total_signups")
    assert proc.returncode == 0  # report-only: finding printed
    assert "vanity metric" in proc.stdout or "falsifies nothing" in proc.stdout
    gated = _card(portfolio, metric="cumulative_gross_revenue", extra=("--gate",))
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    bare = _card(portfolio, metric="signups", extra=("--gate",))
    assert bare.returncode == 1  # a bare counter is not a per-cohort rate either
    assert "per-cohort rate" in bare.stdout


# ---- seeded defect: verdict with no card (evasion omission) ---------------------


def test_verdict_without_card_is_refused(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    proc = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "9",
                "--verdict", "validated")
    assert proc.returncode == 2
    assert "no card, no verdict" in proc.stderr


def test_hand_set_validated_status_without_card_verdict_is_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    asms = _assumptions(portfolio)
    asms[0]["status"] = "validated"  # the omission evasion: status without evidence
    (portfolio / "bets" / "b1" / "assumptions.json").write_text(
        json.dumps({"assumptions": asms}))
    proc = _asm(portfolio, "check", "b1")
    assert proc.returncode == 0  # report-only
    assert "no card verdict to back it" in proc.stdout
    assert _asm(portfolio, "check", "b1", "--gate").returncode == 1


# ---- seeded defect: verdict against altered threshold ---------------------------


def test_verdict_against_altered_threshold_refused_and_rebaseline_heals(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    cards_path = portfolio / "bets" / "b1" / "test-cards.json"
    doc = json.loads(cards_path.read_text())
    doc["cards"][0]["threshold"]["value"] = 1  # success bar quietly lowered
    cards_path.write_text(json.dumps(doc))
    gated = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "2",
                 "--verdict", "validated", "--gate")
    assert gated.returncode == 1
    assert "does not match the journaled pre-registration" in gated.stdout
    assert _cards(portfolio)[0]["verdict"] is None  # refused, not recorded
    # the sanctioned path: a journaled rebaseline with a reason
    heal = _asm(portfolio, "rebaseline", "b1", "TC-001", "--value", "1",
                "--reason", "traffic source changed; bar re-scoped before any result")
    assert heal.returncode == 0, heal.stderr
    rec = [r for r in _journal(portfolio) if r["event"] == "card-rebaseline"][-1]
    assert rec["details"]["old_threshold_hash"] != rec["details"]["new_threshold_hash"]
    assert rec["details"]["reason"].startswith("traffic source changed")
    ok = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "2",
              "--verdict", "validated", "--gate")
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_rebaseline_requires_reason_and_freezes_decided_cards(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    no_reason = _asm(portfolio, "rebaseline", "b1", "TC-001", "--value", "2")
    assert no_reason.returncode == 2  # argparse: --reason required
    _asm(portfolio, "verdict", "b1", "TC-001", "--result", "9", "--verdict", "validated")
    frozen = _asm(portfolio, "rebaseline", "b1", "TC-001", "--value", "2",
                  "--reason", "x")
    assert frozen.returncode == 2
    assert "frozen" in frozen.stderr


# ---- verdict semantics ----------------------------------------------------------


def test_verdict_updates_card_asm_and_journal(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    proc = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "7.5",
                "--verdict", "validated", "--sample-n", "140")
    assert proc.returncode == 0, proc.stderr
    card = _cards(portfolio)[0]
    assert card["result"] == 7.5 and card["verdict"] == "validated"
    assert _assumptions(portfolio)[0]["status"] == "validated"
    rec = [r for r in _journal(portfolio) if r["event"] == "card-verdict"][-1]
    assert rec["details"]["asm_status"] == "validated"
    again = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "1",
                 "--verdict", "falsified")
    assert again.returncode == 2  # a card is decided once
    assert "already has a verdict" in again.stderr


def test_falsified_verdict_falsifies_the_assumption(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    proc = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "0.4",
                "--verdict", "falsified")
    assert proc.returncode == 0, proc.stderr
    assert _assumptions(portfolio)[0]["status"] == "falsified"


def test_verdict_contradicting_threshold_is_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)  # bar: >= 5
    proc = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "2",
                "--verdict", "validated", "--gate")
    assert proc.returncode == 1
    assert "contradicts the pre-registered threshold" in proc.stdout


def test_underpowered_sample_is_flagged(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)  # sample_min 100
    proc = _asm(portfolio, "verdict", "b1", "TC-001", "--result", "8",
                "--verdict", "validated", "--sample-n", "12")
    assert proc.returncode == 0  # report-only
    assert "under-powered" in proc.stdout


# ---- seeded defect: interview-only evidence capped at opinion -------------------


def test_interview_only_evidence_caps_asm_at_weak(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    proc = _card(portfolio, method="interview", metric="interview_commit_rate_pct",
                 strength="4")
    assert proc.returncode == 0
    assert "capped at 1 (opinion)" in proc.stdout
    _asm(portfolio, "verdict", "b1", "TC-001", "--result", "80",
         "--verdict", "validated")
    status = _asm(portfolio, "status", "b1")
    assert "strength 1 (opinion)" in status.stdout  # not 4, however many interviews
    # ...and the building floor is NOT satisfied by interview-only validation
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "transition", "b1", "validating")
    gated = _bet(portfolio, "transition", "b1", "building", "--gate")
    assert gated.returncode == 1
    assert "evidence floor" in gated.stdout


# ---- building-transition evidence floor -----------------------------------------


def test_building_without_strength4_flagged_then_passes_with_money(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    _asm(portfolio, "verdict", "b1", "TC-001", "--result", "7.5",
         "--verdict", "validated")
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "transition", "b1", "validating")
    reported = _bet(portfolio, "transition", "b1", "building")
    assert reported.returncode == 0  # report-only: flagged, applied
    assert "purchase orders, not enthusiasm" in reported.stdout
    assert _state(portfolio, "b1") == "building"
    # reset and prove --gate refuses, then money-class evidence unlocks it
    bet_path = portfolio / "bets" / "b1" / "bet.json"
    bet = json.loads(bet_path.read_text())
    bet["state"] = "validating"
    bet_path.write_text(json.dumps(bet))
    gated = _bet(portfolio, "transition", "b1", "building", "--gate")
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    assert _state(portfolio, "b1") == "validating"
    _add(portfolio, statement=MONEY_XYZ, source="financial_model", element="pricing")
    _card(portfolio, asm="ASM-002", method="presale",
          metric="waitlist_to_deposit_conversion_pct", value="3",
          sample_min="40", strength="5")
    _asm(portfolio, "verdict", "b1", "TC-002", "--result", "4.2",
         "--verdict", "validated", "--sample-n", "48")
    ok = _bet(portfolio, "transition", "b1", "building", "--gate")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _state(portfolio, "b1") == "building"


def test_building_floor_skips_without_assumptions_file(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "transition", "b1", "validating")
    proc = _bet(portfolio, "transition", "b1", "building", "--gate")
    assert proc.returncode == 0, proc.stdout + proc.stderr  # skip, never fail
    assert "skipped: no assumptions.json" in proc.stdout
    assert _state(portfolio, "b1") == "building"


# ---- traceability gate (check_traceability.py's shape, new node types) ----------


def test_check_reports_uncovered_dangling_and_unregistered(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)  # ASM-001, no card → uncovered
    cards_path = portfolio / "bets" / "b1" / "test-cards.json"
    cards_path.write_text(json.dumps({"cards": [{
        # hand-added card: dangling asm ref AND no journaled pre-registration
        "card_id": "TC-009", "asm_id": "ASM-777", "method": "presale",
        "metric": "visitor_to_preorder_rate_pct",
        "threshold": {"comparator": ">=", "value": 2}, "sample_min": 50,
        "cost_estimate_usd": None, "evidence_strength": 5,
        "result": None, "sample_n": None, "verdict": None,
        "created": "2026-08-02T00:00:00+00:00", "verdict_ts": None,
    }]}))
    proc = _asm(portfolio, "check", "b1")
    assert proc.returncode == 0  # report-only
    assert "uncovered" in proc.stdout
    assert "dangling" in proc.stdout
    assert "no journaled pre-registration" in proc.stdout
    assert _asm(portfolio, "check", "b1", "--gate").returncode == 1


def test_check_clean_bet_passes_gate(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    _asm(portfolio, "verdict", "b1", "TC-001", "--result", "7.5",
         "--verdict", "validated", "--sample-n", "150")
    proc = _asm(portfolio, "check", "b1", "--gate")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_check_skips_when_no_ledger_files(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    proc = _asm(portfolio, "check", "b1", "--gate")
    assert proc.returncode == 0, proc.stdout
    assert "skipped" in proc.stdout


# ---- pivot dependency rule (Bland) ----------------------------------------------


def _validated_pair(portfolio, tmp_path):
    """b1 with ASM-001 (customer-segment) and ASM-002 (pricing), both validated."""
    _create_bet(portfolio, tmp_path)
    _add(portfolio, element="customer-segment")
    _add(portfolio, statement=MONEY_XYZ, source="financial_model", element="pricing")
    _card(portfolio)
    _card(portfolio, asm="ASM-002", method="presale",
          metric="waitlist_to_deposit_conversion_pct", value="3",
          sample_min="40", strength="5")
    _asm(portfolio, "verdict", "b1", "TC-001", "--result", "7.5", "--verdict", "validated")
    _asm(portfolio, "verdict", "b1", "TC-002", "--result", "4.2", "--verdict", "validated")


def test_pivot_reopens_dependent_asms_in_successor(portfolio, tmp_path):
    _validated_pair(portfolio, tmp_path)
    _retro(portfolio, "b1")
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    proc = _bet(portfolio, "transition", "b1", "killed",
                "--successor", "b1-v2", "--envelope", str(env_p),
                "--criteria", str(crit_p), "--changed-elements", "customer-segment",
                "--reason", "segment pivot")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "re-opened 1 assumption(s) (ASM-001)" in proc.stdout
    carried = _assumptions(portfolio, "b1-v2")
    by_id = {a["id"]: a for a in carried}
    assert by_id["ASM-001"]["status"] == "untested"  # dependent: re-opened
    assert by_id["ASM-001"]["reopened_by"].startswith("rec-")
    assert by_id["ASM-002"]["status"] == "validated"  # pricing unchanged: carried
    # cards do NOT carry — evidence was registered against the old thesis
    assert not (portfolio / "bets" / "b1-v2" / "test-cards.json").exists()
    rec = [r for r in _journal(portfolio) if r["event"] == "asm-reopen"][-1]
    assert rec["ref"] == "b1-v2"
    assert rec["details"]["reopened"] == ["ASM-001"]
    assert rec["details"]["pivot_from"] == "b1"
    # the old bet's ledger is untouched (history stays honest)
    assert _assumptions(portfolio, "b1")[0]["status"] == "validated"


def test_changed_elements_without_successor_is_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    proc = _bet(portfolio, "transition", "b1", "probing",
                "--changed-elements", "pricing")
    assert proc.returncode == 2
    assert "--successor" in proc.stderr


def test_pivot_without_changed_elements_does_not_touch_ledger(portfolio, tmp_path):
    _validated_pair(portfolio, tmp_path)
    _retro(portfolio, "b1")
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    proc = _bet(portfolio, "transition", "b1", "killed",
                "--successor", "b1-v2", "--envelope", str(env_p),
                "--criteria", str(crit_p), "--reason", "pivot")
    assert proc.returncode == 0, proc.stderr
    assert not (portfolio / "bets" / "b1-v2" / "assumptions.json").exists()


# ---- tampered journal fails closed ----------------------------------------------


def test_tampered_journal_fails_closed_exit_4(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _add(portfolio)
    _card(portfolio)
    jpath = portfolio / "journal.jsonl"
    lines = jpath.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["details"]["threshold_hash"] = "forged"  # interior rewrite
    lines[-1] = json.dumps(rec, sort_keys=True)
    jpath.write_text("\n".join(lines) + "\n")
    for cmd in (["check", "b1"], ["card", "b1", "--asm", "ASM-001", "--method", "m",
                 "--metric", "x_rate_pct", "--comparator", ">=", "--value", "1",
                 "--sample-min", "10", "--evidence-strength", "2"],
                ["verdict", "b1", "TC-001", "--result", "1", "--verdict", "falsified"]):
        proc = _asm(portfolio, *cmd)
        assert proc.returncode == 4, f"{cmd}: {proc.stdout}{proc.stderr}"
        assert "tamper" in proc.stderr


# ---- terminal bets are frozen ---------------------------------------------------


def test_terminal_bet_takes_no_validation_work(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    _bet(portfolio, "transition", "b1", "parked")
    proc = _add(portfolio)
    assert proc.returncode == 2
    assert "terminal" in proc.stderr

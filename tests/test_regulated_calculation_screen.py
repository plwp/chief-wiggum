"""Tests for standing screen 15 — regulated-calculation liability (chief-wiggum#260).

Harvested from a bet whose product computed a number a regulator can audit, killed
the same day it was created on premise falsification. A bet failing the screen's
sub-questions is not automatically refused — regulated calculation is a real market —
but the screen must be ANSWERED at create time; an unanswered screen while the thesis
names a regulated-calculation domain is a report-only finding, never a hard block.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bet as betlib  # noqa: E402

BET = str(SCRIPTS / "bet.py")

ENVELOPE = {
    "cash_cap_usd": 900,
    "liability_exposure": {"type": "capped_at", "amount_usd": 900},
    "tranches": [{"amount_usd": 900, "unlock_milestone_id": None}],
}
CRITERIA = {"criteria": []}

VALID_SCREEN = {
    "who_bears_error": "the customer, who could be penalised for a payroll miscalculation",
    "correctness_winnable": "incumbents disclaim accuracy; not a differentiator",
    "insurable": "no — contractually-assumed liability is a standard PI exclusion",
    "paid_configuration": "yes, in year one — priced as risk in the envelope",
    "interpretation_surface": "unbounded — dozens of jurisdiction-specific rulesets",
}


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(portfolio: Path, *argv: str):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)
    return subprocess.run(
        [sys.executable, BET, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _files(tmp_path, screen=None):
    env_p, crit_p = tmp_path / "envelope.json", tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    paths = [str(env_p), str(crit_p)]
    if screen is not None:
        screen_p = tmp_path / "screen.json"
        screen_p.write_text(json.dumps(screen))
        paths.append(str(screen_p))
    return paths


# ---- unit-level: regulated_calculation_findings / soundness -------------------

def test_findings_silent_when_thesis_has_no_regulated_keyword():
    bet = {"thesis": "a scheduling tool for dog groomers"}
    assert betlib.regulated_calculation_findings(bet) == []


def test_findings_flags_wage_keyword_without_screen():
    bet = {"thesis": "computes wage entitlements from roster exports"}
    out = betlib.regulated_calculation_findings(bet)
    assert len(out) == 1
    assert "regulated_calculation_screen" in out[0]


def test_findings_flags_tax_and_super_keywords():
    for kw in ("tax", "superannuation", "payroll", "clinical dosing", "penalty rate"):
        bet = {"thesis": f"a tool that computes {kw} for small clinics"}
        assert betlib.regulated_calculation_findings(bet), kw


def test_findings_silent_when_screen_is_recorded():
    bet = {"thesis": "computes wage entitlements", "regulated_calculation_screen": VALID_SCREEN}
    assert betlib.regulated_calculation_findings(bet) == []


def test_findings_silent_on_unrelated_thesis_even_with_a_screen_present():
    bet = {"thesis": "a scheduling tool", "regulated_calculation_screen": VALID_SCREEN}
    assert betlib.regulated_calculation_findings(bet) == []


def test_soundness_passes_a_well_formed_screen():
    assert betlib.regulated_calculation_soundness(VALID_SCREEN) == []


def test_soundness_flags_non_dict():
    assert betlib.regulated_calculation_soundness("nope") == [
        "regulated_calculation_screen must be an object"
    ]


def test_soundness_flags_each_missing_field():
    for field in betlib.REGULATED_SCREEN_FIELDS:
        screen = dict(VALID_SCREEN)
        del screen[field]
        out = betlib.regulated_calculation_soundness(screen)
        assert any(field in f for f in out), (field, out)


def test_soundness_flags_empty_string_field():
    screen = dict(VALID_SCREEN)
    screen["insurable"] = "   "
    out = betlib.regulated_calculation_soundness(screen)
    assert any("insurable" in f for f in out)


# ---- CLI: bet.py create ---------------------------------------------------------

def test_create_flags_regulated_thesis_without_screen_report_only(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements from timesheets",
             "--envelope", env_p, "--criteria", crit_p)
    assert r.returncode == 0
    assert "regulated_calculation_screen" in r.stdout


def test_create_blocks_under_gate_when_screen_missing(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements from timesheets",
             "--envelope", env_p, "--criteria", crit_p, "--gate")
    assert r.returncode == 1
    assert "REFUSED" in r.stdout


def test_create_clean_when_screen_provided(portfolio, tmp_path):
    env_p, crit_p, screen_p = _files(tmp_path, VALID_SCREEN)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements from timesheets",
             "--envelope", env_p, "--criteria", crit_p,
             "--regulated-calculation-screen", screen_p)
    assert r.returncode == 0
    assert "regulated_calculation_screen" not in r.stdout.replace(
        "--regulated-calculation-screen", "")


def test_create_silent_for_a_non_regulated_thesis(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "a booking tool for dog groomers",
             "--envelope", env_p, "--criteria", crit_p)
    assert r.returncode == 0
    assert "regulated_calculation_screen" not in r.stdout


def test_create_malformed_screen_is_a_soundness_finding(portfolio, tmp_path):
    bad_screen = dict(VALID_SCREEN)
    del bad_screen["insurable"]
    env_p, crit_p, screen_p = _files(tmp_path, bad_screen)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements",
             "--envelope", env_p, "--criteria", crit_p,
             "--regulated-calculation-screen", screen_p)
    assert r.returncode == 0
    assert "insurable" in r.stdout


def test_bet_json_persists_the_screen(portfolio, tmp_path):
    env_p, crit_p, screen_p = _files(tmp_path, VALID_SCREEN)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements",
             "--envelope", env_p, "--criteria", crit_p,
             "--regulated-calculation-screen", screen_p)
    assert r.returncode == 0
    bet = json.loads((portfolio / "bets" / "b1" / "bet.json").read_text())
    assert bet["regulated_calculation_screen"] == VALID_SCREEN


# ---- pivot successor does not inherit the screen -------------------------------

def test_pivot_successor_does_not_inherit_regulated_calculation_screen(portfolio, tmp_path):
    env_p, crit_p, screen_p = _files(tmp_path, VALID_SCREEN)
    r = _run(portfolio, "create", "b1", "--title", "t",
             "--thesis", "computes payroll entitlements",
             "--envelope", env_p, "--criteria", crit_p,
             "--regulated-calculation-screen", screen_p)
    assert r.returncode == 0
    retro = portfolio / "bets" / "b1" / "retrospective.md"
    retro.write_text("Envelope respected; criteria evaluated honestly; kill on trigger.\n" * 3)
    env2_p, crit2_p = tmp_path / "env2.json", tmp_path / "crit2.json"
    env2_p.write_text(json.dumps(ENVELOPE))
    crit2_p.write_text(json.dumps(CRITERIA))
    r3 = _run(portfolio, "transition", "b1", "killed", "--successor", "b2",
              "--envelope", str(env2_p), "--criteria", str(crit2_p),
              "--successor-title", "pivot", "--reason", "pivot")
    assert r3.returncode == 0, r3.stdout + r3.stderr
    successor = json.loads((portfolio / "bets" / "b2" / "bet.json").read_text())
    assert "regulated_calculation_screen" not in successor

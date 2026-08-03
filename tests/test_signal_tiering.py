"""Tests for signal-source tiering + the create-time competitor sweep (chief-wiggum#254).

A bet whose thesis rests solely on a Tier-C (public) signal is contested by
construction — the competitor sweep for it must exist and be current, checked at
CREATE time (not name-pick time, the sequencing that missed a real direct-twin
collision). Report-only always, per docs/gate-rollout.md: contested markets are
often the correct call — the mandate is legibility, never avoidance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
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
CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-11-01", "direction": "has"},
    ]
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


def _files(tmp_path, sweep=None):
    env_p, crit_p = tmp_path / "envelope.json", tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    paths = [str(env_p), str(crit_p)]
    if sweep is not None:
        sweep_p = tmp_path / "sweep.json"
        sweep_p.write_text(json.dumps(sweep))
        paths.append(str(sweep_p))
    return paths


def _sweep(*, days_ago=0, competitors=None, unresolved=None):
    return {
        "date": (date.today() - timedelta(days=days_ago)).isoformat(),
        "sources": ["search engine", "marketplace listing"],
        "competitors": competitors if competitors is not None else [],
        "unresolved": unresolved if unresolved is not None else [],
    }


# ---- create-time findings -----------------------------------------------------

def test_signal_tier_undeclared_is_skipped_not_a_finding(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p, "--criteria", crit_p)
    assert r.returncode == 0
    assert "signal_tier not declared" in r.stdout
    # skipped: findings never gate, even with --gate
    r2 = _run(portfolio, "create", "b2", "--title", "t", "--envelope", env_p,
               "--criteria", crit_p, "--gate")
    assert r2.returncode == 0


def test_tier_a_without_sweep_is_never_flagged(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "A")
    assert r.returncode == 0
    assert "competitor_sweep" not in r.stdout


def test_tier_b_without_sweep_is_never_flagged(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "B")
    assert r.returncode == 0
    assert "competitor_sweep" not in r.stdout


def test_tier_c_without_sweep_is_flagged_report_only(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C")
    assert r.returncode == 0
    assert "no competitor_sweep recorded" in r.stdout


def test_tier_c_without_sweep_blocks_under_gate(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--gate")
    assert r.returncode == 1
    assert "REFUSED" in r.stdout


def test_tier_c_with_fresh_sweep_is_clean(portfolio, tmp_path):
    env_p, crit_p, sweep_p = _files(tmp_path, _sweep(days_ago=1))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--competitor-sweep", sweep_p)
    assert r.returncode == 0
    assert "competitor_sweep" not in r.stdout or "stale" not in r.stdout


def test_tier_c_with_stale_sweep_is_flagged(portfolio, tmp_path):
    env_p, crit_p, sweep_p = _files(tmp_path, _sweep(days_ago=45))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--competitor-sweep", sweep_p)
    assert r.returncode == 0
    assert "stale" in r.stdout
    assert "45d" in r.stdout


def test_tier_c_with_sweep_exactly_at_boundary_is_clean(portfolio, tmp_path):
    env_p, crit_p, sweep_p = _files(tmp_path, _sweep(days_ago=30))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--competitor-sweep", sweep_p)
    assert r.returncode == 0
    assert "stale" not in r.stdout


def test_competitor_sweep_malformed_missing_fields_is_a_soundness_finding(portfolio, tmp_path):
    env_p, crit_p = tmp_path / "envelope.json", tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    sweep_p = tmp_path / "sweep.json"
    sweep_p.write_text(json.dumps({"date": "not-a-date", "sources": "nope"}))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", str(env_p),
              "--criteria", str(crit_p), "--signal-tier", "C",
              "--competitor-sweep", str(sweep_p))
    assert r.returncode == 0
    assert "competitor_sweep.date" in r.stdout
    assert "competitor_sweep.sources" in r.stdout
    assert "competitor_sweep.competitors" in r.stdout
    assert "competitor_sweep.unresolved" in r.stdout


def test_bet_json_persists_signal_tier_and_sweep(portfolio, tmp_path):
    env_p, crit_p, sweep_p = _files(tmp_path, _sweep(
        days_ago=0, competitors=[{"name": "Rival Co", "url": "https://rival.example"}]))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--competitor-sweep", sweep_p)
    assert r.returncode == 0
    bet = json.loads((portfolio / "bets" / "b1" / "bet.json").read_text())
    assert bet["signal_tier"] == "C"
    assert bet["competitor_sweep"]["competitors"][0]["name"] == "Rival Co"


# ---- unit-level: competitor_sweep_soundness / competitor_sweep_findings -------

def test_soundness_flags_non_dict_sweep():
    assert betlib.competitor_sweep_soundness("nope") == ["competitor_sweep must be an object"]


def test_soundness_passes_a_well_formed_sweep():
    assert betlib.competitor_sweep_soundness(_sweep()) == []


def test_findings_skipped_when_tier_undeclared():
    assert betlib.competitor_sweep_findings({}, date.today()) == [
        "skipped: signal_tier not declared (chief-wiggum#254)"
    ]


def test_findings_silent_for_tier_a_and_b():
    for tier in ("A", "B"):
        assert betlib.competitor_sweep_findings({"signal_tier": tier}, date.today()) == []


def test_findings_flags_missing_sweep_for_tier_c():
    out = betlib.competitor_sweep_findings({"signal_tier": "C"}, date.today())
    assert len(out) == 1 and "no competitor_sweep recorded" in out[0]


def test_findings_flags_stale_sweep_for_tier_c():
    bet = {"signal_tier": "C", "competitor_sweep": _sweep(days_ago=31)}
    out = betlib.competitor_sweep_findings(bet, date.today())
    assert len(out) == 1 and "stale" in out[0] and "31d" in out[0]


def test_findings_clean_for_fresh_tier_c_sweep():
    bet = {"signal_tier": "C", "competitor_sweep": _sweep(days_ago=5)}
    assert betlib.competitor_sweep_findings(bet, date.today()) == []


# ---- kill-brief surfacing ------------------------------------------------------

def test_kill_brief_surfaces_signal_tier_and_competitors(portfolio, tmp_path):
    env_p, crit_p, sweep_p = _files(tmp_path, _sweep(
        days_ago=2, competitors=[{"name": "Rival Co", "url": "https://rival.example"}],
        unresolved=["unclear if Rival Co is still active"]))
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C", "--competitor-sweep", sweep_p)
    assert r.returncode == 0

    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date.today())
    assert findings == []
    assert "## Signal grounding" in text
    assert "Rival Co" in text
    assert "unclear if Rival Co is still active" in text


def test_kill_brief_flags_missing_sweep_for_tier_c_without_refusing(portfolio, tmp_path):
    env_p, crit_p = _files(tmp_path)
    r = _run(portfolio, "create", "b1", "--title", "t", "--envelope", env_p,
              "--criteria", crit_p, "--signal-tier", "C")
    assert r.returncode == 0

    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date.today())
    assert findings == []  # missing sweep is surfaced in-brief, never a refusal reason
    assert "UNRESOLVED: no competitor_sweep recorded" in text
    assert "REQUIRED for a Tier-C bet" in text

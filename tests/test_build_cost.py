"""Tests for scripts/build_cost.py (chief-wiggum#257): nominal (API-priced) and
actual (plan-share) build-cost tracking, attributed per bet / 'factory' /
'unattributed', journaled into the same portfolio hash chain as bet.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bet as betlib  # noqa: E402
import build_cost  # noqa: E402
import ratchet  # noqa: E402

BUILD_COST = str(SCRIPTS / "build_cost.py")
BET = str(SCRIPTS / "bet.py")

REAL_MODEL = "claude-opus-4-8"       # priced in config/model_pricing.json
UNPRICED_MODEL = "no-such-model-xyz"  # not in the table -> nominal must be omitted

ENVELOPE = {
    "cash_cap_usd": 900,
    "liability_exposure": {"type": "capped_at", "amount_usd": 900},
    "tranches": [{"amount_usd": 900, "unlock_milestone_id": None}],
}
CRITERIA = {"criteria": []}


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(*argv: str, portfolio_dir: Path, env_extra: dict | None = None):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio_dir)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, BUILD_COST, *argv, "--portfolio-dir", str(portfolio_dir)],
        capture_output=True, text=True, env=env,
    )


def _bet_run(*argv: str, portfolio_dir: Path):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio_dir)
    return subprocess.run(
        [sys.executable, BET, *argv, "--portfolio-dir", str(portfolio_dir)],
        capture_output=True, text=True, env=env,
    )


def _create_bet(portfolio, tmp_path, bet_id="b1", envelope=None):
    env_p, crit_p = tmp_path / "envelope.json", tmp_path / "criteria.json"
    env_p.write_text(json.dumps(envelope or ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    r = _bet_run("create", bet_id, "--title", f"bet {bet_id}",
                 "--envelope", str(env_p), "--criteria", str(crit_p),
                 portfolio_dir=portfolio)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _journal(portfolio: Path) -> list[dict]:
    return ratchet.load_journal(SimpleNamespace(journal=portfolio / "journal.jsonl"))


# ---- nominal_usd: omitted, never zero, for an unpriced model -------------------

def test_nominal_usd_computed_for_a_priced_model(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    rec = build_cost.record_build_cost(
        portfolio, "b1", REAL_MODEL, 1_000_000, 500_000, None, "")
    assert rec["details"]["nominal_usd"] is not None
    assert rec["details"]["nominal_usd"] > 0


def test_nominal_usd_omitted_not_zeroed_for_unpriced_model(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    rec = build_cost.record_build_cost(
        portfolio, "b1", UNPRICED_MODEL, 1000, 500, None, "")
    assert "nominal_usd" not in rec["details"]


# ---- plan_share_pct: honest {unresolved}, never guessed ------------------------

def test_plan_share_unresolved_when_not_supplied(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    rec = build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 100, 50, None, "")
    assert rec["details"]["plan_share_pct"] is None
    assert rec["details"]["plan_share_unresolved"] is True


def test_plan_share_recorded_when_supplied(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    rec = build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 100, 50, 2.5, "")
    assert rec["details"]["plan_share_pct"] == 2.5
    assert rec["details"]["plan_share_unresolved"] is False


# ---- journaling: same hash chain as bet.py -------------------------------------

def test_build_cost_is_journaled_into_the_portfolio_chain(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 100, 50, 1.0, "note")
    recs = [r for r in _journal(portfolio) if r["event"] == "build-cost"]
    assert len(recs) == 1
    assert recs[0]["ref"] == "b1"
    assert recs[0]["details"]["note"] == "note"


def test_attribution_to_factory_and_unattributed_are_accepted(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, build_cost.FACTORY, REAL_MODEL, 10, 5, None, "")
    build_cost.record_build_cost(portfolio, build_cost.UNATTRIBUTED, REAL_MODEL, 10, 5, None, "")
    refs = {r["ref"] for r in _journal(portfolio) if r["event"] == "build-cost"}
    assert refs == {"factory", "unattributed"}


# ---- summarize(): sum-preserving, honest partial flags -------------------------

def test_summarize_sums_nominal_and_flags_partial_when_one_entry_unpriced(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 1_000_000, 0, None, "")
    build_cost.record_build_cost(portfolio, "b1", UNPRICED_MODEL, 1_000_000, 0, None, "")
    summary = build_cost.summarize(build_cost.load_build_costs(portfolio, "b1"))
    assert summary["records"] == 2
    assert summary["nominal_usd"] is not None and summary["nominal_usd"] > 0
    assert summary["nominal_partial"] is True


def test_summarize_plan_share_partial_when_one_entry_unresolved(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 10, 5, 1.0, "")
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 10, 5, None, "")
    summary = build_cost.summarize(build_cost.load_build_costs(portfolio, "b1"))
    assert summary["plan_share_partial"] is True
    assert summary["plan_share_pct"] is None  # partial -> UNRESOLVED as a whole, never a silent undercount


def test_summarize_empty_is_all_none(portfolio, tmp_path):
    summary = build_cost.summarize([])
    assert summary["records"] == 0
    assert summary["nominal_usd"] is None
    assert summary["plan_share_pct"] is None


# ---- portfolio_summary(): sum-preserving across buckets ------------------------

def test_portfolio_summary_never_drops_or_spreads_unattributed(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path, "b1")
    _create_bet(portfolio, tmp_path, "b2")
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 100, 50, 1.0, "")
    build_cost.record_build_cost(portfolio, build_cost.UNATTRIBUTED, REAL_MODEL, 100, 50, 1.0, "")
    summary = build_cost.portfolio_summary(portfolio)
    assert set(summary) == {"b1", build_cost.UNATTRIBUTED}
    assert summary["b1"]["records"] == 1
    assert summary[build_cost.UNATTRIBUTED]["records"] == 1


# ---- CLI ------------------------------------------------------------------------

def test_cli_record_and_summary_roundtrip(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    r = _run("record", "--attribute-to", "b1", "--model", REAL_MODEL,
             "--tokens-in", "1000", "--tokens-out", "500", "--plan-share-pct", "3.5",
             portfolio_dir=portfolio)
    assert r.returncode == 0, r.stdout + r.stderr
    r2 = _run("summary", "--attribute-to", "b1", "--format", "json", portfolio_dir=portfolio)
    assert r2.returncode == 0
    out = json.loads(r2.stdout)
    assert out["records"] == 1
    assert out["plan_share_pct"] == 3.5


def test_cli_record_unpriced_model_reports_unresolved_nominal(portfolio, tmp_path, capsys):
    _create_bet(portfolio, tmp_path)
    r = _run("record", "--attribute-to", "b1", "--model", UNPRICED_MODEL,
              "--tokens-in", "10", "--tokens-out", "5", portfolio_dir=portfolio)
    assert r.returncode == 0
    assert "UNRESOLVED" in r.stdout


def test_cli_portfolio_json_lists_every_bucket(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path, "b1")
    _run("record", "--attribute-to", "b1", "--model", REAL_MODEL,
          "--tokens-in", "10", "--tokens-out", "5", portfolio_dir=portfolio)
    _run("record", "--attribute-to", build_cost.FACTORY, "--model", REAL_MODEL,
          "--tokens-in", "10", "--tokens-out", "5", portfolio_dir=portfolio)
    r = _run("portfolio", "--format", "json", portfolio_dir=portfolio)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert set(out) == {"b1", "factory"}


def test_cli_negative_tokens_is_a_usage_error(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    r = _run("record", "--attribute-to", "b1", "--model", REAL_MODEL,
              "--tokens-in", "-5", "--tokens-out", "5", portfolio_dir=portfolio)
    assert r.returncode == 2


# ---- bet.py integration: portfolio + kill-brief --------------------------------

def test_bet_portfolio_surfaces_build_cost_per_bet(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 100, 50, 2.0, "")
    r = _bet_run("portfolio", portfolio_dir=portfolio)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "build cost:" in r.stdout
    assert "plan-share" in r.stdout


def test_bet_portfolio_surfaces_unattributed_bucket_separately(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, build_cost.UNATTRIBUTED, REAL_MODEL, 100, 50, None, "")
    r = _bet_run("portfolio", portfolio_dir=portfolio)
    assert r.returncode == 0
    assert "[unattributed]" in r.stdout


def test_bet_portfolio_json_includes_build_cost_other_bucket(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, build_cost.FACTORY, REAL_MODEL, 100, 50, None, "")
    r = _bet_run("portfolio", "--format", "json", portfolio_dir=portfolio)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "factory" in out["build_cost_other"]
    assert out["bets"][0]["build_cost"]["records"] == 0


def test_nominal_build_cap_exceeded_is_a_never_gating_finding(portfolio, tmp_path):
    env = dict(ENVELOPE)
    env["nominal_build_cap_usd"] = 0.0001  # trivially small, guaranteed to be exceeded
    _create_bet(portfolio, tmp_path, envelope=env)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 1_000_000, 500_000, None, "")
    r = _bet_run("portfolio", "--gate", portfolio_dir=portfolio)
    assert r.returncode == 0, r.stdout + r.stderr  # buildcost: findings never gate
    assert "buildcost:" in r.stdout
    assert "nominal_build_cap_usd" in r.stdout


def test_kill_brief_includes_build_cost_section(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    build_cost.record_build_cost(portfolio, "b1", REAL_MODEL, 1_000_000, 500_000, 4.2, "")
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1")
    assert findings == []
    assert "## Build cost" in text
    assert "4.2%" in text


def test_kill_brief_build_cost_section_clean_with_no_records(portfolio, tmp_path):
    _create_bet(portfolio, tmp_path)
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1")
    assert findings == []
    assert "## Build cost" in text
    assert "none recorded" in text

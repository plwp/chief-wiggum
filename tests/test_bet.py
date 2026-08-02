"""Tests for scripts/bet.py — the bet ledger (chief-wiggum#235).

Seeded-defect coverage for every gate the script ships (all report-only by
default per docs/gate-rollout.md): malformed states-and-dates criterion, spend
past the unlocked tranches, `killed` without a retrospective, a tampered
journal (fail closed, exit 4), the bets-in-flight cap, rebaseline without
--reason, a dated criterion firing at evaluate, and pivot-as-transition.
Everything runs against a tmp_path portfolio — never the real ~/.chief-wiggum.
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

import ratchet  # noqa: E402

BET = str(SCRIPTS / "bet.py")

ENVELOPE = {
    "cash_cap_usd": 900,
    "time_cap_hours": 120,
    "calendar_cap_days": 90,
    "attention_slots": 1,
    "tranches": [
        {"amount_usd": 300, "unlock_milestone_id": None},
        {"amount_usd": 600, "unlock_milestone_id": "M1"},
    ],
}

CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-11-01", "direction": "has"},
        {"id": "KC-2", "metric": "refund_rate_pct", "comparator": ">",
         "threshold": 30, "by_date": "2026-11-01", "direction": "has_not"},
    ]
}


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(portfolio: Path, *argv: str, env_extra: dict | None = None):
    import os
    env = dict(os.environ)
    # Belt-and-braces: even a test that forgets --portfolio-dir stays in tmp.
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, BET, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _files(tmp_path, envelope=None, criteria=None) -> tuple[str, str]:
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    env_p.write_text(json.dumps(envelope or ENVELOPE))
    crit_p.write_text(json.dumps(criteria or CRITERIA))
    return str(env_p), str(crit_p)


def _create(portfolio, tmp_path, bet_id="b1", *extra, envelope=None, criteria=None):
    env_p, crit_p = _files(tmp_path, envelope, criteria)
    return _run(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--envelope", env_p, "--criteria", crit_p, *extra)


def _journal(portfolio: Path) -> list[dict]:
    return ratchet.load_journal(SimpleNamespace(journal=portfolio / "journal.jsonl"))


def _state(portfolio: Path, bet_id: str) -> str:
    return json.loads((portfolio / "bets" / bet_id / "bet.json").read_text())["state"]


def _retro(portfolio: Path, bet_id: str) -> None:
    (portfolio / "bets" / bet_id / "retrospective.md").write_text(
        "# retro\n\nEnvelope respected; criteria evaluated honestly; the kill "
        "was executed on trigger. Distribution was never attempted.\n"
    )


# ---- create + journal ----------------------------------------------------------


def test_create_hashes_goalposts_into_journal(portfolio, tmp_path):
    proc = _create(portfolio, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (portfolio / "bets" / "b1" / "bet.json").is_file()
    assert (portfolio / "bets" / "b1" / "kill-criteria.json").is_file()
    assert (portfolio / "bets" / "b1" / "ledger.jsonl").is_file()
    assert (portfolio / ".git").exists()  # git-initialized on first use
    records = _journal(portfolio)  # verifies the hash chain from genesis
    assert len(records) == 1
    assert records[0]["event"] == "bet-create"
    assert records[0]["details"]["envelope_hash"]
    assert records[0]["details"]["criteria_hash"]


def test_create_duplicate_bet_is_usage_error(portfolio, tmp_path):
    assert _create(portfolio, tmp_path).returncode == 0
    proc = _create(portfolio, tmp_path)
    assert proc.returncode == 2
    assert "already exists" in proc.stderr


def test_env_var_portfolio_dir_is_respected(tmp_path):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(tmp_path / "envport")
    env_p, crit_p = _files(tmp_path)
    proc = subprocess.run(
        [sys.executable, BET, "create", "b1", "--title", "t",
         "--envelope", env_p, "--criteria", crit_p],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "envport" / "bets" / "b1" / "bet.json").is_file()


# ---- seeded defect: malformed criterion (states-and-dates lint) ---------------


def test_malformed_criterion_no_date_is_flagged_report_only(portfolio, tmp_path):
    bad = {"criteria": [{"id": "KC-1", "metric": "paid_conversions",
                         "comparator": ">=", "threshold": 3, "direction": "has"}]}
    proc = _create(portfolio, tmp_path, criteria=bad)
    assert proc.returncode == 0  # report-only default: findings print, exit 0
    assert "malformed" in proc.stdout and "no date" in proc.stdout


def test_malformed_criterion_no_metric_blocks_under_gate(portfolio, tmp_path):
    bad = {"criteria": [{"id": "KC-1", "comparator": ">=", "threshold": 3,
                         "by_date": "2026-11-01", "direction": "has"}]}
    proc = _create(portfolio, tmp_path, "b1", "--gate", criteria=bad)
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout
    assert not (portfolio / "bets" / "b1" / "bet.json").exists()


# ---- seeded defect: spend past the unlocked tranches ---------------------------


def test_spend_past_tranche_flagged_and_gated(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    ok = _run(portfolio, "spend", "b1", "--amount-usd", "250")
    assert ok.returncode == 0 and "exceeds" not in ok.stdout
    over = _run(portfolio, "spend", "b1", "--amount-usd", "100")
    assert over.returncode == 0  # report-only: recorded, finding printed
    assert "exceeds cumulative unlocked tranches" in over.stdout
    gated = _run(portfolio, "spend", "b1", "--amount-usd", "100", "--gate")
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    entries = (portfolio / "bets" / "b1" / "ledger.jsonl").read_text().splitlines()
    assert len([ln for ln in entries if ln.strip()]) == 2  # gated entry NOT appended


def test_unlock_milestone_raises_the_cap(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--amount-usd", "300")
    proc = _run(portfolio, "transition", "b1", "--unlock-milestone", "M1")
    assert proc.returncode == 0, proc.stderr
    assert "$900 now unlocked" in proc.stdout
    after = _run(portfolio, "spend", "b1", "--amount-usd", "200", "--gate")
    assert after.returncode == 0, after.stdout + after.stderr
    events = [r["event"] for r in _journal(portfolio)]
    assert "tranche-unlock" in events


# ---- seeded defect: killed without retrospective -------------------------------


def test_killed_without_retrospective_is_blocked(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "transition", "b1", "killed", "--reason", "done")
    assert proc.returncode == 1
    assert "retrospective.md" in proc.stderr
    assert _state(portfolio, "b1") == "proposed"


def test_trivial_retrospective_does_not_unlock_killed(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    (portfolio / "bets" / "b1" / "retrospective.md").write_text("# retro\n\nok\n")
    proc = _run(portfolio, "transition", "b1", "killed")
    assert proc.returncode == 1
    assert _state(portfolio, "b1") == "proposed"


def test_killed_with_retrospective_runs_harvest_check_skipped(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _retro(portfolio, "b1")
    proc = _run(portfolio, "transition", "b1", "killed", "--verdict", "kill")
    assert proc.returncode == 0, proc.stderr
    assert "harvest check skipped" in proc.stdout  # absent inputs: reported, never a silent block
    assert _state(portfolio, "b1") == "killed"
    trans = [r for r in _journal(portfolio) if r["event"] == "transition"][-1]
    assert trans["details"]["harvest"] == {
        "skipped": "harvest inputs absent (harvest_inputs.ttm_sdp_usd / "
                   ".wind_down_cost_usd) — est sale value unknown"}


def test_harvest_check_proposes_sold_when_sale_beats_wind_down(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _retro(portfolio, "b1")
    bet_path = portfolio / "bets" / "b1" / "bet.json"
    bet = json.loads(bet_path.read_text())
    bet["harvest_inputs"] = {"ttm_sdp_usd": 10000, "wind_down_cost_usd": 500}
    bet_path.write_text(json.dumps(bet))
    proc = _run(portfolio, "transition", "b1", "killed")
    assert proc.returncode == 0  # report-only
    assert "`sold`, not `killed`" in proc.stdout
    # under --gate the killed transition is refused until sold is chosen
    bet["state"] = "proposed"  # (previous run applied it report-only)
    bet_path.write_text(json.dumps(bet))
    gated = _run(portfolio, "transition", "b1", "killed", "--gate")
    assert gated.returncode == 1
    assert _state(portfolio, "b1") == "proposed"
    sold = _run(portfolio, "transition", "b1", "sold", "--gate")
    assert sold.returncode == 0, sold.stdout + sold.stderr


# ---- seeded defect: tampered journal fails closed ------------------------------


def test_tampered_journal_fails_closed_exit_4(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--amount-usd", "10")
    jpath = portfolio / "journal.jsonl"
    lines = jpath.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["details"]["envelope_hash"] = "forged"  # interior rewrite
    lines[0] = json.dumps(rec, sort_keys=True)
    jpath.write_text("\n".join(lines) + "\n")
    for cmd in (["portfolio"], ["spend", "b1", "--amount-usd", "1"],
                ["evaluate", "b1"], ["transition", "b1", "probing"]):
        proc = _run(portfolio, *cmd)
        assert proc.returncode == 4, f"{cmd}: {proc.stdout}{proc.stderr}"
        assert "tamper" in proc.stderr


# ---- seeded defect: bets-in-flight cap -----------------------------------------


def test_third_in_flight_bet_over_cap(portfolio, tmp_path):
    for bid in ("b1", "b2", "b3"):
        _create(portfolio, tmp_path, bid)
    _run(portfolio, "transition", "b1", "probing")
    _run(portfolio, "transition", "b2", "probing")
    gated = _run(portfolio, "transition", "b3", "probing", "--gate")
    assert gated.returncode == 1
    assert "exceed the cap of 2" in gated.stdout
    assert _state(portfolio, "b3") == "proposed"  # refused, not applied
    reported = _run(portfolio, "transition", "b3", "probing")
    assert reported.returncode == 0  # report-only: finding printed, applied
    assert "exceed the cap of 2" in reported.stdout
    assert _state(portfolio, "b3") == "probing"
    summary = _run(portfolio, "portfolio")
    assert summary.returncode == 0
    assert "exceed the cap of 2" in summary.stdout
    assert _run(portfolio, "portfolio", "--gate").returncode == 1


def test_max_in_flight_override(portfolio, tmp_path):
    for bid in ("b1", "b2", "b3"):
        _create(portfolio, tmp_path, bid)
        _run(portfolio, "transition", bid, "probing")
    proc = _run(portfolio, "portfolio", "--max-in-flight", "3", "--gate")
    assert proc.returncode == 0, proc.stdout


# ---- seeded defect: rebaseline discipline --------------------------------------


def test_rebaseline_without_reason_rejected(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    env_p, _ = _files(tmp_path, envelope={"cash_cap_usd": 2000})
    proc = _run(portfolio, "rebaseline", "b1", "--envelope", env_p)
    assert proc.returncode == 2  # argparse: --reason is required
    assert "--reason" in proc.stderr


def test_rebaseline_journals_old_and_new_hashes(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    old_hash = _journal(portfolio)[0]["details"]["envelope_hash"]
    env_p, _ = _files(tmp_path, envelope={"cash_cap_usd": 2000})
    proc = _run(portfolio, "rebaseline", "b1", "--envelope", env_p,
                "--reason", "means changed: contract income landed")
    assert proc.returncode == 0, proc.stderr
    rec = [r for r in _journal(portfolio) if r["event"] == "rebaseline"][-1]
    assert rec["details"]["old_envelope_hash"] == old_hash
    assert rec["details"]["new_envelope_hash"] != old_hash
    assert rec["details"]["reason"].startswith("means changed")
    bet = json.loads((portfolio / "bets" / "b1" / "bet.json").read_text())
    assert bet["envelope"]["cash_cap_usd"] == 2000


def test_hand_edited_criteria_detected_and_healed_by_rebaseline(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    crit_path = portfolio / "bets" / "b1" / "kill-criteria.json"
    doc = json.loads(crit_path.read_text())
    doc["criteria"][0]["threshold"] = 1  # goalposts quietly lowered
    crit_path.write_text(json.dumps(doc))
    proc = _run(portfolio, "evaluate", "b1")
    assert proc.returncode == 0
    assert "does not match the journaled baseline" in proc.stdout
    assert _run(portfolio, "evaluate", "b1", "--gate").returncode == 1
    new_p = tmp_path / "new-crit.json"
    new_p.write_text(json.dumps(doc))
    heal = _run(portfolio, "rebaseline", "b1", "--criteria", str(new_p),
                "--reason", "threshold deliberately lowered after re-scoping")
    assert heal.returncode == 0, heal.stderr
    assert _run(portfolio, "evaluate", "b1", "--gate").returncode == 0


# ---- seeded defect: dated criterion fires at evaluate --------------------------


def test_evaluate_fires_dated_criterion_and_blocks_spend(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"paid_conversions": 1}))
    proc = _run(portfolio, "evaluate", "b1", "--results", str(results),
                "--as-of", "2026-12-01")
    assert proc.returncode == 0
    assert "TRIGGERED" in proc.stdout
    assert "journaled kill-proposed" in proc.stdout
    assert [r for r in _journal(portfolio) if r["event"] == "kill-proposed"]
    # second evaluate does not duplicate the pending proposal
    again = _run(portfolio, "evaluate", "b1", "--results", str(results),
                 "--as-of", "2026-12-01")
    assert "already pending" in again.stdout
    assert len([r for r in _journal(portfolio) if r["event"] == "kill-proposed"]) == 1
    # further spend is blocked pending the human decision
    spend = _run(portfolio, "spend", "b1", "--amount-usd", "10", "--gate")
    assert spend.returncode == 1
    assert "blocked pending kill decision" in spend.stdout
    # accept: transition into kill_pending resolves the proposal
    accept = _run(portfolio, "transition", "b1", "kill_pending", "--verdict", "kill")
    assert accept.returncode == 0, accept.stderr
    assert _state(portfolio, "b1") == "kill_pending"
    assert _run(portfolio, "spend", "b1", "--amount-usd", "10", "--gate").returncode == 1


def test_kill_override_is_journaled_and_unblocks_spend(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "evaluate", "b1", "--as-of", "2026-12-01")
    bad = _run(portfolio, "transition", "b1", "--override-kill")
    assert bad.returncode == 2  # override without a reason is refused
    proc = _run(portfolio, "transition", "b1", "--override-kill",
                "--reason", "distribution unattempted — recycle, not kill")
    assert proc.returncode == 0, proc.stderr
    assert [r for r in _journal(portfolio) if r["event"] == "kill-override"]
    spend = _run(portfolio, "spend", "b1", "--amount-usd", "10", "--gate")
    assert spend.returncode == 0, spend.stdout


def test_has_not_criterion_triggers_immediately(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"refund_rate_pct": 45, "paid_conversions": 5}))
    proc = _run(portfolio, "evaluate", "b1", "--results", str(results),
                "--as-of", "2026-09-01")  # before by_date
    assert "KC-2 refund_rate_pct: TRIGGERED" in proc.stdout
    assert "KC-1 paid_conversions: MET" in proc.stdout


# ---- seeded defect: pivot closes + opens successor -----------------------------


def test_pivot_closes_bet_and_opens_successor(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _retro(portfolio, "b1")
    env_p, crit_p = _files(tmp_path)
    proc = _run(portfolio, "transition", "b1", "killed",
                "--successor", "b1-v2", "--envelope", env_p, "--criteria", crit_p,
                "--successor-title", "fresh thesis", "--reason", "pivot")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert _state(portfolio, "b1") == "killed"
    assert _state(portfolio, "b1-v2") == "proposed"
    old = json.loads((portfolio / "bets" / "b1" / "bet.json").read_text())
    new = json.loads((portfolio / "bets" / "b1-v2" / "bet.json").read_text())
    assert old["successor"] == "b1-v2"
    assert new["predecessor"] == "b1"
    events = [(r["event"], r["ref"]) for r in _journal(portfolio)]
    assert ("transition", "b1") in events
    assert ("bet-create", "b1-v2") in events  # fresh goalposts hashed for the successor


def test_pivot_requires_fresh_envelope_and_criteria(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _retro(portfolio, "b1")
    proc = _run(portfolio, "transition", "b1", "killed", "--successor", "b1-v2")
    assert proc.returncode == 2
    assert "fresh --envelope and --criteria" in proc.stderr
    assert _state(portfolio, "b1") == "proposed"


# ---- state machine -------------------------------------------------------------


def test_invalid_transition_is_usage_error(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "transition", "b1", "scaling")
    assert proc.returncode == 2
    assert "invalid transition" in proc.stderr
    assert _state(portfolio, "b1") == "proposed"


def test_terminal_bets_are_frozen(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "transition", "b1", "parked")
    assert _state(portfolio, "b1") == "parked"
    assert _run(portfolio, "transition", "b1", "probing").returncode == 2
    assert _run(portfolio, "spend", "b1", "--amount-usd", "1").returncode == 2
    env_p, _ = _files(tmp_path, envelope={"cash_cap_usd": 1})
    assert _run(portfolio, "rebaseline", "b1", "--envelope", env_p,
                "--reason", "x").returncode == 2


def test_kill_pending_resume_requires_override(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "transition", "b1", "kill_pending")
    proc = _run(portfolio, "transition", "b1", "probing")
    assert proc.returncode == 2
    assert "--override-kill" in proc.stderr
    ok = _run(portfolio, "transition", "b1", "probing", "--override-kill",
              "--reason", "fresh data changed the picture")
    assert ok.returncode == 0, ok.stderr
    assert _state(portfolio, "b1") == "probing"


# ---- bet-selection lint (#235 amendment) ---------------------------------------


def _means(portfolio, sales="novice", marketing="novice"):
    portfolio.mkdir(parents=True, exist_ok=True)
    (portfolio / "means.json").write_text(json.dumps({
        "skills": {"sales": sales, "marketing": marketing, "engineering": "strong"},
        "assets": ["chief-wiggum"], "network": [], "owned_audience": [],
        "hours_per_week": 10, "cash_available": 5000,
    }))


def test_selection_lint_flags_no_channel_no_audience(portfolio, tmp_path):
    _means(portfolio)
    proc = _create(portfolio, tmp_path)
    assert proc.returncode == 0  # report-only
    assert "no ecosystem channel and no owned audience" in proc.stdout
    gated = _create(portfolio, tmp_path, "b2", "--gate")
    assert gated.returncode == 1
    assert not (portfolio / "bets" / "b2").exists()


def test_selection_lint_passes_with_ecosystem_channel(portfolio, tmp_path):
    _means(portfolio)
    proc = _create(portfolio, tmp_path, "b1", "--ecosystem-channel", "shopify-app-store",
                   "--gate")
    assert proc.returncode == 0, proc.stdout


def test_selection_lint_passes_when_not_novice(portfolio, tmp_path):
    _means(portfolio, marketing="competent")
    proc = _create(portfolio, tmp_path, "b1", "--gate")
    assert proc.returncode == 0, proc.stdout


def test_selection_lint_passes_once_a_channel_is_focused(portfolio, tmp_path):
    _means(portfolio)
    assert _create(portfolio, tmp_path, "b1", "--ecosystem-channel", "x").returncode == 0
    (portfolio / "bets" / "b1" / "channels.json").write_text(json.dumps(
        {"channels": [{"channel": "seo", "status": "focused"}]}))
    proc = _create(portfolio, tmp_path, "b2", "--gate")
    assert proc.returncode == 0, proc.stdout


def test_selection_lint_skips_without_means_json(portfolio, tmp_path):
    proc = _create(portfolio, tmp_path, "b1", "--gate")
    assert proc.returncode == 0, proc.stdout  # skipped is reported, never blocks
    assert "skipped: no means.json" in proc.stdout


# ---- distribution-attempted status ---------------------------------------------


def test_distribution_unattempted_reported_never_omitted(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "evaluate", "b1", "--as-of", "2026-09-01")
    assert "distribution: unattempted" in proc.stdout
    trig = _run(portfolio, "evaluate", "b1", "--as-of", "2026-12-01")
    assert "no marketing, not no demand" in trig.stdout


def test_rep_entries_count_as_attempted_distribution(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--rep", "--note", "20 cold emails")
    proc = _run(portfolio, "evaluate", "b1", "--as-of", "2026-09-01")
    assert "distribution: attempted" in proc.stdout
    assert "1 rep entry" in proc.stdout


# ---- portfolio summary ---------------------------------------------------------


def test_portfolio_reports_loss_distribution_and_kill_hygiene(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--amount-usd", "150")
    _run(portfolio, "evaluate", "b1", "--as-of", "2026-12-01")  # trigger → proposal
    _retro(portfolio, "b1")
    _run(portfolio, "transition", "b1", "kill_pending")
    _run(portfolio, "transition", "b1", "killed", "--verdict", "kill")
    proc = _run(portfolio, "portfolio")
    assert proc.returncode == 0, proc.stderr
    assert "loss median $150" in proc.stdout
    assert "envelope respected" in proc.stdout
    assert "from trigger to close" in proc.stdout
    assert "never win/lose ranking" in proc.stdout
    js = _run(portfolio, "portfolio", "--format", "json")
    data = json.loads(js.stdout)
    assert data["dead_bets"][0]["loss_usd"] == 150
    assert data["dead_bets"][0]["envelope_respected"] is True


def test_journal_chain_matches_ratchet_format(portfolio, tmp_path):
    """The portfolio journal IS a ratchet-format chain: ratchet.load_journal
    verifies it, and recomputing each record's hash from body + previous hash
    reproduces the stored record_hash."""
    from chief_wiggum.hashing import stable_hash
    _create(portfolio, tmp_path)
    _run(portfolio, "transition", "b1", "probing")
    records = _journal(portfolio)
    prev = "genesis"
    for rec in records:
        body = {k: v for k, v in rec.items() if k != "record_hash"}
        assert rec["record_hash"] == stable_hash(prev, json.dumps(body, sort_keys=True))
        prev = rec["record_hash"]

"""Tests for scripts/channel.py + the #241 ledger-side checks in scripts/bet.py.

Seeded-defect coverage for the channel-engine gates (all report-only by
default per docs/gate-rollout.md): two focused channels, a CAC-omitted
experiment, a 4th concurrent testing channel, a referral experiment without a
baseline input flow, a sales-led channel at headcount 0, a missed rep cadence
(and its surfacing in `bet.py evaluate`'s distribution block), and the
*Traction* 50% rule staying silent when no hours are tagged. Everything runs
against a tmp_path portfolio — never the real ~/.chief-wiggum.
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

import channel as channel_mod  # noqa: E402
import ratchet  # noqa: E402

BET = str(SCRIPTS / "bet.py")
CHANNEL = str(SCRIPTS / "channel.py")

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


def _run(portfolio: Path, script: str, *argv: str):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)
    return subprocess.run(
        [sys.executable, script, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _bet(portfolio, *argv):
    return _run(portfolio, BET, *argv)


def _chan(portfolio, *argv):
    return _run(portfolio, CHANNEL, *argv)


def _create(portfolio, tmp_path, bet_id="b1", *extra):
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(CRITERIA))
    proc = _bet(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--envelope", str(env_p), "--criteria", str(crit_p), *extra)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return proc


def _journal(portfolio: Path) -> list[dict]:
    return ratchet.load_journal(SimpleNamespace(journal=portfolio / "journal.jsonl"))


def _channels(portfolio: Path, bet_id: str = "b1") -> dict[str, dict]:
    doc = json.loads((portfolio / "bets" / bet_id / "channels.json").read_text())
    return {c["channel"]: c for c in doc["channels"]}


def _testing_three(portfolio):
    """Standard inner ring: three channels into testing."""
    for ch in ("content-marketing", "search-engine-optimization", "community-building"):
        proc = _chan(portfolio, "test", "b1", ch, "--hypothesis", f"{ch} reaches ICP")
        assert proc.returncode == 0, proc.stderr + proc.stdout


def _complete(portfolio, ch="content-marketing", cac="12", acquired="4"):
    proc = _chan(portfolio, "record", "b1", ch,
                 "--customers-acquired", acquired, "--measured-cac", cac,
                 "--icp-note", "right ICP", "--verdict", "works")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return proc


# ---- enum + brainstorm ---------------------------------------------------------


def test_enum_is_exactly_19_channels():
    assert len(channel_mod.CHANNELS) == 19
    assert len(set(channel_mod.CHANNELS)) == 19
    assert channel_mod.SALES_LED <= set(channel_mod.CHANNELS)
    assert channel_mod.REFERRAL_CHANNELS <= set(channel_mod.CHANNELS)


def test_brainstorm_seeds_all_19_and_journals(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _chan(portfolio, "brainstorm", "b1")
    assert proc.returncode == 0, proc.stderr
    assert "brainstormed 19 channel(s)" in proc.stdout
    chans = _channels(portfolio)
    assert set(chans) == set(channel_mod.CHANNELS)
    assert all(c["status"] == "brainstormed" for c in chans.values())
    assert [r for r in _journal(portfolio) if r["event"] == "channel-brainstorm"]
    again = _chan(portfolio, "brainstorm", "b1")
    assert again.returncode == 0
    assert "already brainstormed" in again.stdout


def test_unknown_channel_is_usage_error(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    proc = _chan(portfolio, "test", "b1", "growth-hacking")
    assert proc.returncode == 2
    assert "enum is fixed" in proc.stderr


def test_rank_records_human_order(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    proc = _chan(portfolio, "rank", "b1", "content-marketing", "search-engine-optimization")
    assert proc.returncode == 0, proc.stderr
    chans = _channels(portfolio)
    assert chans["content-marketing"]["rank"] == 1
    assert chans["content-marketing"]["status"] == "ranked"
    assert chans["search-engine-optimization"]["rank"] == 2
    assert [r for r in _journal(portfolio) if r["event"] == "channel-rank"]


# ---- seeded defect: 4th concurrent testing channel -----------------------------


def test_fourth_testing_channel_flagged_and_gated(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _testing_three(portfolio)
    gated = _chan(portfolio, "test", "b1", "email-marketing", "--gate")
    assert gated.returncode == 1
    assert "exceed the Bullseye cap of 3" in gated.stdout
    assert "REFUSED" in gated.stdout
    assert _channels(portfolio)["email-marketing"]["status"] == "brainstormed"
    reported = _chan(portfolio, "test", "b1", "email-marketing")
    assert reported.returncode == 0  # report-only: finding printed, applied
    assert "exceed the Bullseye cap of 3" in reported.stdout
    assert _channels(portfolio)["email-marketing"]["status"] == "testing"


# ---- seeded defect: CAC-omitted experiment -------------------------------------


def test_cac_omitted_experiment_is_invalid(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _testing_three(portfolio)
    proc = _chan(portfolio, "record", "b1", "content-marketing",
                 "--customers-acquired", "2", "--verdict", "meh")
    assert proc.returncode == 0  # report-only default
    assert "without a measured CAC" in proc.stdout
    gated = _chan(portfolio, "record", "b1", "search-engine-optimization",
                  "--verdict", "meh", "--gate")
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    # a young testing record with nothing recorded is NOT incomplete
    status = _chan(portfolio, "status", "b1")
    assert "community-building: experiment invalid" not in status.stdout


def test_focus_of_incomplete_experiment_refused_under_gate(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _testing_three(portfolio)
    gated = _chan(portfolio, "focus", "b1", "content-marketing", "--gate")
    assert gated.returncode == 1
    assert "without a measured CAC" in gated.stdout
    assert _channels(portfolio)["content-marketing"]["status"] == "testing"


# ---- seeded defect: two focused channels ---------------------------------------


def test_second_focus_violates_exactly_one_focused(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _testing_three(portfolio)
    _complete(portfolio, "content-marketing")
    _complete(portfolio, "search-engine-optimization", cac="30", acquired="2")
    ok = _chan(portfolio, "focus", "b1", "content-marketing", "--gate")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    second = _chan(portfolio, "focus", "b1", "search-engine-optimization", "--gate")
    assert second.returncode == 1
    assert "exactly-one-focused-channel violated" in second.stdout
    assert "REFUSED" in second.stdout
    assert _channels(portfolio)["search-engine-optimization"]["status"] == "testing"
    # report-only lets it through but the status check keeps flagging it
    _chan(portfolio, "focus", "b1", "search-engine-optimization")
    status = _chan(portfolio, "status", "b1")
    assert "exactly-one-focused-channel violated" in status.stdout
    assert _chan(portfolio, "status", "b1", "--gate").returncode == 1


# ---- seeded defect: referral experiment without baseline flow ------------------


def test_referral_without_baseline_flow_is_invalid(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    proc = _chan(portfolio, "test", "b1", "viral-marketing",
                 "--hypothesis", "referral loop amplifies signups")
    assert proc.returncode == 0
    rec = _chan(portfolio, "record", "b1", "viral-marketing",
                "--customers-acquired", "3", "--k-factor", "0.4",
                "--reward-cost", "8")
    assert rec.returncode == 0  # report-only
    assert "no baseline input flow" in rec.stdout
    assert "multiplier" in rec.stdout
    gated = _chan(portfolio, "record", "b1", "viral-marketing",
                  "--customers-acquired", "3", "--gate")
    assert gated.returncode == 1
    # recording the baseline flow heals it; reward-cost serves as measured CAC
    healed = _chan(portfolio, "record", "b1", "viral-marketing",
                   "--baseline-flow", "SEO signups ~30/wk", "--gate")
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert "CAC $8" in healed.stdout


def test_referral_metrics_only_valid_on_viral_channel(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _chan(portfolio, "test", "b1", "content-marketing")
    proc = _chan(portfolio, "record", "b1", "content-marketing", "--k-factor", "0.5")
    assert proc.returncode == 2
    assert "referral-invite-loop metrics belong" in proc.stderr


# ---- seeded defect: sales-led channel at headcount 0 ---------------------------


def test_sales_led_channel_flagged_at_headcount_zero(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    proc = _chan(portfolio, "test", "b1", "sales", "--hypothesis", "founder sells")
    assert proc.returncode == 0  # report-only
    assert "sales-led channel active at headcount 0" in proc.stdout
    gated = _chan(portfolio, "test", "b1", "business-development", "--gate")
    assert gated.returncode == 1
    assert "REFUSED" in gated.stdout
    # a non-sales-led channel is untouched by the filter
    ok = _chan(portfolio, "test", "b1", "content-marketing", "--gate")
    assert ok.returncode == 0, ok.stdout


def test_headcount_on_bet_disables_the_filter(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    bet_path = portfolio / "bets" / "b1" / "bet.json"
    bet = json.loads(bet_path.read_text())
    bet["headcount"] = 1
    bet_path.write_text(json.dumps(bet))
    _chan(portfolio, "brainstorm", "b1")
    proc = _chan(portfolio, "test", "b1", "sales", "--gate")
    assert proc.returncode == 0, proc.stdout


# ---- CAC ≤ target join ---------------------------------------------------------


def test_cac_join_skipped_without_target(portfolio, tmp_path):
    _create(portfolio, tmp_path)  # no --target-cac
    _chan(portfolio, "brainstorm", "b1")
    _chan(portfolio, "test", "b1", "content-marketing")
    _complete(portfolio, "content-marketing", cac="500")
    proc = _chan(portfolio, "focus", "b1", "content-marketing", "--gate")
    assert proc.returncode == 0, proc.stdout  # skipped never blocks
    assert "skipped: bet declares no target_cac_usd" in proc.stdout


def test_cac_above_target_flagged_on_focus(portfolio, tmp_path):
    _create(portfolio, tmp_path, "b1", "--target-cac", "20")
    _chan(portfolio, "brainstorm", "b1")
    _chan(portfolio, "test", "b1", "content-marketing")
    _complete(portfolio, "content-marketing", cac="35")
    gated = _chan(portfolio, "focus", "b1", "content-marketing", "--gate")
    assert gated.returncode == 1
    assert "exceeds the bet's target CAC $20" in gated.stdout
    under = _chan(portfolio, "record", "b1", "content-marketing", "--measured-cac", "15")
    assert under.returncode == 0
    ok = _chan(portfolio, "focus", "b1", "content-marketing", "--gate")
    assert ok.returncode == 0, ok.stdout


# ---- Bullseye state machine ----------------------------------------------------


def test_focus_requires_testing_and_reentry_needs_reason(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    assert _chan(portfolio, "focus", "b1", "content-marketing").returncode == 2
    _chan(portfolio, "test", "b1", "content-marketing")
    _complete(portfolio, "content-marketing")
    assert _chan(portfolio, "focus", "b1", "content-marketing").returncode == 0
    # saturation re-entry: focused → testing requires --reason, journaled
    bare = _chan(portfolio, "test", "b1", "content-marketing")
    assert bare.returncode == 2
    assert "--reason" in bare.stderr
    proc = _chan(portfolio, "test", "b1", "content-marketing",
                 "--reason", "channel saturated at ~40 customers")
    assert proc.returncode == 0, proc.stderr
    assert _channels(portfolio)["content-marketing"]["status"] == "testing"
    rec = [r for r in _journal(portfolio) if r["event"] == "channel-test"][-1]
    assert rec["details"]["from"] == "focused"


def test_rejected_channel_cannot_reenter_and_terminal_bet_is_frozen(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _chan(portfolio, "test", "b1", "content-marketing")
    _complete(portfolio, "content-marketing")
    assert _chan(portfolio, "reject", "b1", "content-marketing",
                 "--verdict", "CAC underwater").returncode == 0
    assert _chan(portfolio, "test", "b1", "content-marketing").returncode == 2
    _bet(portfolio, "transition", "b1", "parked")
    frozen = _chan(portfolio, "test", "b1", "email-marketing")
    assert frozen.returncode == 2
    assert "terminal" in frozen.stderr
    # status still works on a terminal bet
    assert _chan(portfolio, "status", "b1").returncode == 0


# ---- seeded defect: missed cadence fires + surfaces in evaluate ----------------


def test_missed_cadence_fires_and_surfaces_in_evaluate(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "spend", "b1", "--rep", "--note", "1 mom-test call")
    proc = _bet(portfolio, "evaluate", "b1")
    assert proc.returncode == 0  # report-only
    assert "rep cadence: 1/3 Mom-Test reps" in proc.stdout
    assert "MISSED" in proc.stdout
    assert "rep cadence missed" in proc.stdout
    assert "distribution: attempted" in proc.stdout  # rep entries are evidence
    assert _bet(portfolio, "evaluate", "b1", "--gate").returncode == 1
    # the channel engine's own report surfaces the same doing-gap
    status = _chan(portfolio, "status", "b1")
    assert "rep cadence: 1/3" in status.stdout


def test_cadence_met_is_ok_and_respects_per_bet_override(portfolio, tmp_path):
    _create(portfolio, tmp_path, "b1", "--cadence", "2")
    created = [r for r in _journal(portfolio) if r["event"] == "bet-create"][-1]
    assert created["details"]["rep_cadence_per_week"] == 2  # journaled at create
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "spend", "b1", "--rep", "--note", "call 1")
    _bet(portfolio, "spend", "b1", "--rep", "--note", "call 2")
    proc = _bet(portfolio, "evaluate", "b1", "--gate")
    assert proc.returncode == 0, proc.stdout
    assert "rep cadence: 2/2 Mom-Test reps" in proc.stdout
    assert "MISSED" not in proc.stdout


def test_cadence_not_checked_outside_probing_validating(portfolio, tmp_path):
    _create(portfolio, tmp_path)  # proposed
    proc = _bet(portfolio, "evaluate", "b1")
    assert "rep cadence" not in proc.stdout
    assert "cadence missed" not in proc.stdout


# ---- seeded defect: 50% rule silent when untagged ------------------------------


def test_traction_rule_silent_when_no_tagged_hours(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "spend", "b1", "--hours", "10")  # untagged
    for _ in range(3):
        _bet(portfolio, "spend", "b1", "--rep")
    proc = _bet(portfolio, "evaluate", "b1", "--gate")
    assert proc.returncode == 0, proc.stdout  # absent data is never a finding
    assert "traction" not in proc.stdout.lower()


def test_traction_share_below_half_is_a_finding(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _bet(portfolio, "transition", "b1", "probing")
    _bet(portfolio, "spend", "b1", "--hours", "8", "--tag", "product")
    _bet(portfolio, "spend", "b1", "--hours", "2", "--tag", "traction")
    for _ in range(3):
        _bet(portfolio, "spend", "b1", "--rep")
    proc = _bet(portfolio, "evaluate", "b1")
    assert proc.returncode == 0  # report-only
    assert "traction 50% rule: traction share 20%" in proc.stdout
    assert _bet(portfolio, "evaluate", "b1", "--gate").returncode == 1
    _bet(portfolio, "spend", "b1", "--hours", "8", "--tag", "traction")
    ok = _bet(portfolio, "evaluate", "b1", "--gate")
    assert ok.returncode == 0, ok.stdout


# ---- distribution-attempted integration ----------------------------------------


def test_brainstormed_channels_are_not_attempted_distribution(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    proc = _bet(portfolio, "evaluate", "b1", "--as-of", "2026-09-01")
    assert "distribution: unattempted" in proc.stdout  # consideration ≠ attempt
    assert "19 brainstormed" in proc.stdout
    _chan(portfolio, "test", "b1", "content-marketing")
    after = _bet(portfolio, "evaluate", "b1", "--as-of", "2026-09-01")
    assert "distribution: attempted (1 channel experiment(s)" in after.stdout
    assert "1 testing" in after.stdout


def test_evaluate_shows_channel_and_rep_evidence_together(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _chan(portfolio, "brainstorm", "b1")
    _chan(portfolio, "test", "b1", "content-marketing")
    _complete(portfolio, "content-marketing")
    _chan(portfolio, "focus", "b1", "content-marketing")
    _bet(portfolio, "spend", "b1", "--rep", "--note", "demo call")
    proc = _bet(portfolio, "evaluate", "b1", "--as-of", "2026-09-01")
    assert "distribution: attempted" in proc.stdout
    assert "1 rep entry" in proc.stdout
    assert "1 focused" in proc.stdout

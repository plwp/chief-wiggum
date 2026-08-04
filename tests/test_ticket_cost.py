"""Tests for scripts/ticket_cost.py — per-ticket implementation cost: the ledger
slice behind the PR's ## Implementation Cost section, the Effort-class estimator
behind the issue's Nominal cost line, and the calibration loop between them.

The honesty properties are the point (fail-open bug class, chief-wiggum#289):
no records is UNMETERED (never $0), unpriced calls flag cost_partial (never
silently understate), an estimate below min-samples is UNRESOLVED (never a
guess), and partial actuals never feed the estimator's p50.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factory_log  # noqa: E402
import ticket_cost  # noqa: E402

REPO = "acme/app"
TICKET = "42"


def _cc(ticket=TICKET, *, repo="app", source="repl_main_thread", cost=1.0,
        tin=1000, tout=100, cache=5000):
    r = {"event": factory_log.CLAUDE_CODE, "query_source": source,
         "tokens_in": tin, "tokens_out": tout, "cache_read": cache,
         "cost_usd": cost}
    if repo:
        r["repo"] = repo
    if ticket:
        r["ticket"] = ticket
    return r


def _consult(ticket=TICKET, *, provider="codex", cost=0.25, tin=800, tout=200):
    return {"event": factory_log.CONSULT, "provider": provider, "repo": REPO,
            "ticket": ticket, "tokens_in": tin, "tokens_out": tout,
            "cost_usd": cost}


def _calibration(effort="M", actual=3.0, *, partial=False, repo=REPO):
    return {"event": ticket_cost.TICKET_COST, "repo": repo, "effort": effort,
            "actual_usd": actual, "cost_partial": partial}


# ---- summarize_ticket: the per-ticket slice ---------------------------------

def test_summarize_slices_by_ticket_and_layers():
    records = [
        _cc(cost=2.0),                                    # orchestrator
        _cc(cost=1.5, source="subagent"),                 # subagent
        {"event": factory_log.WORKER, "repo": REPO, "ticket": TICKET,
         "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.5},  # worker -> subagents
        _consult(cost=0.25),
        _consult(cost=0.10, provider="gemini"),
        _cc(ticket="99", cost=50.0),                      # other ticket: excluded
        _cc(ticket=None, cost=50.0),                      # untagged: excluded
        _consult(cost=50.0, ticket="99"),                 # other ticket: excluded
    ]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    assert s["status"] == "metered"
    assert s["records"] == 5
    assert s["layers"]["orchestrator"]["cost_usd"] == pytest.approx(2.0)
    assert s["layers"]["subagents"]["cost_usd"] == pytest.approx(2.0)
    assert s["layers"]["consults"]["cost_usd"] == pytest.approx(0.35)
    assert s["total_cost_usd"] == pytest.approx(4.35)
    assert s["cost_partial"] is False
    assert s["consult_providers"] == ["codex", "gemini"]


def test_summarize_matches_repo_basename_and_full_name():
    # Transcript ingests derive only the basename from cwd; consults carry owner/repo.
    records = [_cc(repo="app", cost=1.0), _consult(cost=0.5)]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    assert s["records"] == 2
    # A different repo with the same ticket number must NOT match.
    s2 = ticket_cost.summarize_ticket([_cc(repo="otherapp")], REPO, TICKET)
    assert s2["status"] == "unmetered"


def test_no_records_is_unmetered_never_zero_dollars():
    s = ticket_cost.summarize_ticket([], REPO, TICKET)
    assert s["status"] == "unmetered"
    assert s["total_cost_usd"] is None          # absence of telemetry, not $0
    md = ticket_cost.render_actual_markdown(s)
    assert "Unmetered" in md
    assert "not a $0 build" in md      # says why, instead of showing a zero total
    assert "**$" not in md             # no dollar total rendered at all


def test_unpriced_calls_flag_partial_never_silently_understate():
    records = [
        _cc(cost=2.0),
        {"event": factory_log.CONSULT, "provider": "codex", "repo": REPO,
         "ticket": TICKET, "tokens_in": 500, "tokens_out": 100},  # no cost_usd
    ]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    assert s["cost_partial"] is True
    assert s["layers"]["consults"]["unpriced_calls"] == 1
    assert s["total_cost_usd"] == pytest.approx(2.0)
    md = ticket_cost.render_actual_markdown(s)
    assert "understates" in md


# ---- markdown rendering ------------------------------------------------------

def test_markdown_has_table_total_and_no_heading():
    s = ticket_cost.summarize_ticket([_cc(cost=2.0), _consult(cost=0.5)], REPO, TICKET)
    md = ticket_cost.render_actual_markdown(s)
    assert "| Layer |" in md
    assert "**$2.50**" in md
    # shipping.build_pr_body owns the section heading.
    assert "## Implementation Cost" not in md


def test_markdown_variance_line_when_estimate_given():
    s = ticket_cost.summarize_ticket([_cc(cost=4.0)], REPO, TICKET)
    md = ticket_cost.render_actual_markdown(s, estimate_usd=3.20)
    assert "Estimated ~$3.20" in md
    assert "actual $4.00" in md
    assert "(+25%)" in md


# ---- estimator + calibration loop -------------------------------------------

def test_estimate_is_p50_of_matching_effort_class():
    records = [_calibration(actual=2.0), _calibration(actual=3.0),
               _calibration(actual=10.0), _calibration("L", actual=99.0)]
    est = ticket_cost.estimate_for_effort(records, "M")
    assert est["status"] == "ok"
    assert est["p50_usd"] == pytest.approx(3.0)
    assert est["samples"] == 3


def test_estimate_below_min_samples_is_unresolved_never_guessed():
    est = ticket_cost.estimate_for_effort([_calibration()], "M")
    assert est["status"] == "insufficient-samples"
    assert est["p50_usd"] is None
    line = ticket_cost.render_estimate_line(est)
    assert "UNRESOLVED" in line
    assert "$" not in line          # no invented figure anywhere in the line
    assert "need >=3" in line


def test_partial_and_unmetered_calibrations_never_feed_the_p50():
    records = [_calibration(actual=2.0), _calibration(actual=4.0),
               _calibration(actual=3.0),
               _calibration(actual=0.1, partial=True),      # lower bound: excluded
               _calibration(actual=None)]                   # unmetered: excluded
    est = ticket_cost.estimate_for_effort(records, "M")
    assert est["samples"] == 3
    assert est["excluded_partial"] == 2
    assert est["p50_usd"] == pytest.approx(3.0)


def test_estimate_repo_filter_narrows_history():
    records = [_calibration(actual=1.0), _calibration(actual=1.0),
               _calibration(actual=1.0, repo="other/repo")]
    est = ticket_cost.estimate_for_effort(records, "M", repo=REPO)
    assert est["samples"] == 2


def test_record_calibration_always_writes_without_telemetry_env(tmp_path, monkeypatch):
    # Recording is an explicit act, like the ingests — CW_TELEMETRY must not gate it.
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    s = ticket_cost.summarize_ticket([_cc(cost=2.5)], REPO, TICKET)
    ticket_cost.record_calibration(s, effort="M", estimate_usd=3.2)
    rec = json.loads(log.read_text().strip())
    assert rec["event"] == ticket_cost.TICKET_COST
    assert rec["actual_usd"] == pytest.approx(2.5)
    assert rec["effort"] == "M"
    assert rec["estimate_usd"] == pytest.approx(3.2)


def test_record_then_estimate_closes_the_loop(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    for cost in (2.0, 3.0, 4.0):
        s = ticket_cost.summarize_ticket([_cc(cost=cost)], REPO, TICKET)
        ticket_cost.record_calibration(s, effort="S")
    est = ticket_cost.estimate_for_effort(factory_log.read_log(), "S")
    assert est["status"] == "ok"
    assert est["p50_usd"] == pytest.approx(3.0)


# ---- ingest ticket tagging (factory_log side) --------------------------------

def _turn(request_id, *, cwd, sidechain=False):
    return json.dumps({
        "type": "assistant", "requestId": request_id, "sessionId": "sess-1",
        "timestamp": "2026-08-03T06:15:00.000Z", "isSidechain": sidechain,
        "cwd": cwd,
        "message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 100, "output_tokens": 200,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0,
                               "ephemeral_1h_input_tokens": 0},
        }},
    })


def test_transcript_ingest_tags_ticket_only_for_matching_repo(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    (root / "p" / "s.jsonl").write_text(
        _turn("r1", cwd="/repos/app") + "\n" + _turn("r2", cwd="/repos/unrelated") + "\n")
    n = factory_log.ingest_claude_transcripts(root, repo=REPO, ticket=TICKET)
    assert n == 2
    recs = {r["request_id"]: r for r in factory_log.read_log()}
    assert recs["r1"]["ticket"] == TICKET          # cwd-derived repo matches basename
    assert "ticket" not in recs["r2"]              # different repo: never tagged


def test_transcript_ingest_cwd_prefix_is_worktree_precise(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    wt = "/repos/app/.claude/worktrees/t42"
    (root / "p" / "s.jsonl").write_text(
        _turn("r1", cwd=wt) + "\n" + _turn("r2", cwd="/repos/app/.claude/worktrees/t99") + "\n")
    factory_log.ingest_claude_transcripts(root, repo=REPO, ticket=TICKET, cwd_prefix=wt)
    recs = {r["request_id"]: r for r in factory_log.read_log()}
    assert recs["r1"]["ticket"] == TICKET
    assert "ticket" not in recs["r2"]              # sibling worktree: not this ticket


def test_transcript_ingest_ticket_without_guard_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    with pytest.raises(ValueError):
        factory_log.ingest_claude_transcripts(tmp_path, ticket=TICKET)

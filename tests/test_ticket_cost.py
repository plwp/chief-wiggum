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
        tin=1000, tout=100, cache=5000, cwd=None, ts=None):
    r = {"event": factory_log.CLAUDE_CODE, "query_source": source,
         "tokens_in": tin, "tokens_out": tout, "cache_read": cache,
         "cost_usd": cost}
    if repo:
        r["repo"] = repo
    if ticket:
        r["ticket"] = ticket
    if cwd is not None:
        # Untagged (no `ticket`/`repo`) records still carry `cwd`+`ts` so
        # read-time window slicing (#345 §3) can recover them.
        r["cwd"] = cwd
    if ts is not None:
        r["ts"] = ts
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


def test_record_cli_with_window_flags_journals_the_windowed_slice(tmp_path, monkeypatch):
    """`record` must accept the same --cwd-prefix/--since-ts/--until-ts flags as
    `actual` and thread them into the SAME summarize path — otherwise a
    calibration recorded via the CLI silently reverts to tag-match-only,
    journaling a small "clean" consult-only sample while the real windowed
    slice (claude_code layers included) is far larger. This is the exact
    escape #345 exists to fix, now leaking into the calibration/estimator
    loop itself if left unfixed (chief-wiggum#345, orchestrator-verification
    finding)."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    wt = "/repos/app/.claude/worktrees/t42"
    # Untagged claude_code turn, recoverable only by cwd+window -- the whole
    # point of the read-time slice (#345 §3).
    factory_log._append({"event": factory_log.CLAUDE_CODE, "query_source": "repl_main_thread",
                         "tokens_in": 100, "tokens_out": 50, "cost_usd": 190.0,
                         "cwd": wt, "ts": 100})
    # Tagged consult -- the only thing a tag-match-only summarize would see.
    factory_log._append({"event": factory_log.CONSULT, "provider": "codex", "repo": REPO,
                         "ticket": TICKET, "tokens_in": 800, "tokens_out": 200, "cost_usd": 3.85})

    monkeypatch.setattr("sys.argv", [
        "ticket_cost.py", "record", "--repo", REPO, "--ticket", TICKET,
        "--cwd-prefix", wt, "--since-ts", "50", "--until-ts", "200",
    ])
    assert ticket_cost.main() == 0

    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["event"] == ticket_cost.TICKET_COST
    assert rec["actual_usd"] == pytest.approx(193.85)  # windowed: claude_code + consult


def test_record_cli_without_window_flags_is_unchanged(tmp_path, monkeypatch):
    """Backward compat: no window flags on the CLI -> the same tag-match-only
    summarize as before this fix (a record's own --cwd-prefix/--since-ts/
    --until-ts default to None, exactly like `actual`'s)."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    wt = "/repos/app/.claude/worktrees/t42"
    factory_log._append({"event": factory_log.CLAUDE_CODE, "query_source": "repl_main_thread",
                         "tokens_in": 100, "tokens_out": 50, "cost_usd": 190.0,
                         "cwd": wt, "ts": 100})  # untagged: must NOT be picked up without a window
    factory_log._append({"event": factory_log.CONSULT, "provider": "codex", "repo": REPO,
                         "ticket": TICKET, "tokens_in": 800, "tokens_out": 200, "cost_usd": 3.85})

    monkeypatch.setattr("sys.argv", ["ticket_cost.py", "record", "--repo", REPO, "--ticket", TICKET])
    assert ticket_cost.main() == 0

    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["actual_usd"] == pytest.approx(3.85)  # consult only, exactly as before


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


def test_transcript_ingest_cwd_prefix_does_not_cross_bill_a_lexical_sibling(tmp_path, monkeypatch):
    """A bare `str.startswith` would match `.../t420` against prefix `.../t42`
    — a sibling worktree, not a child of it. Reviewer finding (#345 fix-up):
    cwd_prefix must be exact-or-true-child, never a lexical prefix match."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    wt = "/repos/app/.claude/worktrees/t42"
    (root / "p" / "s.jsonl").write_text(
        _turn("r1", cwd=wt) + "\n" + _turn("r2", cwd="/repos/app/.claude/worktrees/t420") + "\n")
    factory_log.ingest_claude_transcripts(root, repo=REPO, ticket=TICKET, cwd_prefix=wt)
    recs = {r["request_id"]: r for r in factory_log.read_log()}
    assert recs["r1"]["ticket"] == TICKET
    assert "ticket" not in recs["r2"]              # lexical sibling t420: not this ticket


def test_transcript_ingest_ticket_without_guard_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    with pytest.raises(ValueError):
        factory_log.ingest_claude_transcripts(tmp_path, ticket=TICKET)


# ---- read-time window slicing (chief-wiggum#345 §3, conflict 3 half 2) ------
#
# Dedup is by request id and tagging happens at first ingest, so a turn swept
# up by an automatic catch-up ingest (or a sibling /implement-wave worker's
# ingest) can never be re-tagged afterwards — but its cwd and timestamp still
# say which build it belongs to. summarize_ticket's cwd_prefix/since/until
# params recover it at READ time, as a complement to the ticket tag, not a
# replacement.

_WT = "/repos/app/.claude/worktrees/t42"


def test_window_slicing_matches_untagged_turns_by_cwd_and_time():
    records = [
        _cc(ticket=None, repo=None, cwd=_WT, ts=100, cost=1.0),  # in window, matching cwd
        _cc(ticket=None, repo=None, cwd="/repos/app/.claude/worktrees/t99",
            ts=100, cost=50.0),                                  # sibling worktree: excluded
        _cc(ticket=None, repo=None, cwd=_WT, ts=5, cost=50.0),   # before the window: excluded
        _cc(ticket=None, repo=None, cwd=_WT, ts=500, cost=50.0),  # after the window: excluded
    ]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET, cwd_prefix=_WT, since=50, until=200)
    assert s["status"] == "metered"
    assert s["records"] == 1
    assert s["total_cost_usd"] == pytest.approx(1.0)


def test_window_slice_and_tag_never_double_count():
    """A record matching BOTH the ticket tag and the cwd/time window (the
    common case once tagging works) must still count once — `or` short-circuits."""
    records = [_cc(cwd=_WT, ts=100, cost=2.0)]  # tagged AND in-window
    s = ticket_cost.summarize_ticket(records, REPO, TICKET, cwd_prefix=_WT, since=0, until=200)
    assert s["records"] == 1
    assert s["total_cost_usd"] == pytest.approx(2.0)


def test_window_slicing_does_not_cross_bill_a_lexical_sibling_worktree():
    """Same #345 fix-up as the ingest-tagging case: prefix `.../t42` must not
    match cwd `.../t420` — a `startswith` on the raw strings would."""
    records = [
        _cc(ticket=None, repo=None, cwd=_WT, ts=100, cost=1.0),               # true match
        _cc(ticket=None, repo=None, cwd=_WT + "0", ts=100, cost=50.0),        # t420: lexical sibling
    ]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET, cwd_prefix=_WT, since=0, until=200)
    assert s["records"] == 1
    assert s["total_cost_usd"] == pytest.approx(1.0)


# ---- coverage: the capture denominator (chief-wiggum#345 AC4) ---------------
#
# A cost table that silently omits the layers it never saw invites a reader to
# take a consult-only figure as the total (#345, the #381 incident). Coverage
# must derive each layer's status from evidence INDEPENDENT of the rendered
# slice, and the four outcomes must stay distinct (the #289 taxonomy —
# "failed to observe" must never read as "pass").

def _stub_count_transcript_turns(monkeypatch, *, scanned=True, orchestrator=0, subagent=0):
    def _fake(root=None, *, since=None, until=None, repo=None, cwd_prefix=None):
        return {"scanned": scanned, "repl_main_thread": orchestrator, "subagent": subagent}
    monkeypatch.setattr(factory_log, "count_transcript_turns", _fake, raising=False)


def test_coverage_all_layers_captured_is_complete(monkeypatch):
    _stub_count_transcript_turns(monkeypatch)
    records = [_cc(source="repl_main_thread", cost=1.0), _cc(source="subagent", cost=1.0),
               _consult(cost=0.25)]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    cov = ticket_cost.coverage(s, records, since=0, until=1000)
    assert cov["status"] == "complete"
    assert cov["captured_layers"] == 3
    assert cov["total_layers"] == 3
    for layer in cov["layers"].values():
        assert layer["status"] in ("captured", "captured-partial")


def test_coverage_consults_only_names_the_missing_claude_layers(monkeypatch):
    """The #381 shape: only the consult layer flowed. Coverage must name the
    other two as UNCAPTURED with a turn-count denominator and a fix command —
    and the rendered table must never show $0.00 for them."""
    _stub_count_transcript_turns(monkeypatch, orchestrator=312, subagent=1204)
    records = [_consult(cost=0.35)]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    cov = ticket_cost.coverage(s, records, since=0, until=1000)
    assert cov["layers"]["orchestrator"]["status"] == "uncaptured"
    assert cov["layers"]["subagents"]["status"] == "uncaptured"
    assert "312" in cov["layers"]["orchestrator"]["basis"]
    assert cov["layers"]["orchestrator"]["fix"]
    assert cov["status"] == "consults-only"
    md = ticket_cost.render_coverage_markdown(cov)
    assert "UNCAPTURED" in md
    assert "$0.00" not in md
    assert "312" in md
    assert "1204" in md or "1,204" in md


def test_coverage_partial_prints_both_denominators(monkeypatch):
    _stub_count_transcript_turns(monkeypatch)
    priced = [_consult(cost=0.1, provider=f"p{i}") for i in range(3)]
    unpriced = [{"event": factory_log.CONSULT, "provider": f"u{i}", "repo": REPO,
                 "ticket": TICKET, "tokens_in": 100, "tokens_out": 50}
                for i in range(9)]
    records = priced + unpriced
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    cov = ticket_cost.coverage(s, records, since=0, until=1000)
    assert cov["layers"]["consults"]["status"] == "captured-partial"
    assert "9" in cov["layers"]["consults"]["basis"]
    assert "12" in cov["layers"]["consults"]["basis"]


def test_coverage_unknown_when_no_transcript_root(monkeypatch):
    _stub_count_transcript_turns(monkeypatch, scanned=False)
    records = [_consult(cost=0.25)]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    cov = ticket_cost.coverage(s, records, since=0, until=1000)
    assert cov["layers"]["orchestrator"]["status"] == "unknown"
    assert cov["layers"]["subagents"]["status"] == "unknown"
    md = ticket_cost.render_coverage_markdown(cov)
    assert "$0.00" not in md


def test_coverage_with_no_window_never_scans_and_is_unknown(monkeypatch):
    """`since is None` -> skip the scan entirely (an unbounded corpus scan at
    PR time is not worth its cost) -- unknown is the honest answer, never
    captured, never a fabricated evidence count."""
    scanned = {"called": False}

    def _fail_if_called(*a, **k):
        scanned["called"] = True
        return {"scanned": True, "repl_main_thread": 0, "subagent": 0}

    monkeypatch.setattr(factory_log, "count_transcript_turns", _fail_if_called, raising=False)
    records = [_consult(cost=0.25)]
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    cov = ticket_cost.coverage(s, records)  # no since/until at all
    assert cov["layers"]["orchestrator"]["status"] == "unknown"
    assert scanned["called"] is False


def test_coverage_untagged_consults_in_window_are_evidence(monkeypatch):
    """The exact #381 fingerprint: zero TAGGED consults for this ticket, but
    untagged same-repo consult spend sits in the build window -- that is
    evidence the review quorum ran untagged, not evidence nothing happened."""
    _stub_count_transcript_turns(monkeypatch, orchestrator=5, subagent=5)
    untagged_consults = [{"event": factory_log.CONSULT, "provider": "codex", "repo": REPO,
                          "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.1, "ts": 50}
                         for _ in range(3)]
    records = [_cc(source="repl_main_thread", cost=1.0),
               _cc(source="subagent", cost=1.0)] + untagged_consults
    s = ticket_cost.summarize_ticket(records, REPO, TICKET)
    assert s["status"] == "metered"
    assert s["layers"]["consults"]["calls"] == 0  # sanity: the tagged slice really is empty
    cov = ticket_cost.coverage(s, records, since=0, until=1000)
    assert cov["layers"]["consults"]["status"] == "uncaptured"
    assert "3" in cov["layers"]["consults"]["basis"]
    assert cov["layers"]["consults"]["fix"]


def test_unmetered_markdown_keeps_its_wording_and_gains_coverage(monkeypatch):
    _stub_count_transcript_turns(monkeypatch, orchestrator=10, subagent=20)
    s = ticket_cost.summarize_ticket([], REPO, TICKET)
    cov = ticket_cost.coverage(s, [], since=0, until=1000)
    md = ticket_cost.render_actual_markdown(s, cov=cov)
    # The §5.1 honesty properties must survive unchanged.
    assert "Unmetered" in md
    assert "not a $0 build" in md
    assert "**$" not in md
    # ...and now also carry the coverage table.
    assert "UNCAPTURED" in md.upper()


def test_exit_code_on_gap_is_opt_in(monkeypatch, tmp_path):
    """Default stays exit 0 (report-only, docs/gate-rollout.md); --exit-code-on-gap
    is the opt-in escalation and only it may return 3."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(factory_log, "count_transcript_turns",
                        lambda *a, **k: {"scanned": False}, raising=False)

    monkeypatch.setattr("sys.argv", ["ticket_cost.py", "actual", "--repo", REPO,
                                     "--ticket", TICKET, "--format", "text"])
    assert ticket_cost.main() == 0

    monkeypatch.setattr("sys.argv", ["ticket_cost.py", "actual", "--repo", REPO,
                                     "--ticket", TICKET, "--format", "text",
                                     "--exit-code-on-gap"])
    assert ticket_cost.main() == 3

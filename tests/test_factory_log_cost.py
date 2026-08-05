"""Tests for the gate cost/benefit signal: transcript ingest, cache-aware
pricing, distinct-finding counting, and honest unattributed cost.

These cover the three things that made `aggregate`'s verdicts unusable:
Claude Code's own cost was never ingested, `caught` re-counted the same finding
once per run, and an unattributed cost was reported as a measured $0.00.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factory_log  # noqa: E402

PRICING = {"claude-opus-5": {"input_per_mtok": 5.0, "output_per_mtok": 25.0}}
MULTS = {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}


def _turn(request_id, *, model="claude-opus-5", tin=100, tout=200, cache_read=0,
          w5=0, w1=0, sidechain=False, cwd="/repos/acme", ts="2026-08-03T06:15:00.000Z",
          agent=None):
    """One assistant turn as Claude Code writes it to a session transcript.

    ``agent`` is the sub-agent TYPE Claude Code stamps as ``attributionAgent``
    (e.g. ``general-purpose``, ``Explore``) — absent (``None``) for an
    orchestrator turn, matching the real transcript shape."""
    rec = {
        "type": "assistant", "requestId": request_id, "sessionId": "sess-1",
        "timestamp": ts, "isSidechain": sidechain, "cwd": cwd,
        "message": {"model": model, "usage": {
            "input_tokens": tin, "output_tokens": tout,
            "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": w5,
                               "ephemeral_1h_input_tokens": w1},
        }},
    }
    if agent is not None:
        rec["attributionAgent"] = agent
    return json.dumps(rec)


@pytest.fixture
def transcripts(tmp_path):
    root = tmp_path / "projects"
    (root / "proj-a").mkdir(parents=True)
    return root


# ---- cache-aware pricing ----------------------------------------------------

def test_cost_for_usage_prices_cache_buckets_at_their_own_rates():
    # 1M cache reads at 0.1x a $5/MTok input rate = $0.50, not $5.00.
    cost = factory_log.cost_for_usage("claude-opus-5", 0, 0, cache_read=1_000_000,
                                      pricing=PRICING, multipliers=MULTS)
    assert cost == pytest.approx(0.50)


def test_cost_for_usage_charges_1h_writes_more_than_5m_writes():
    five_m = factory_log.cost_for_usage("claude-opus-5", 0, 0, cache_write_5m=1_000_000,
                                        pricing=PRICING, multipliers=MULTS)
    one_h = factory_log.cost_for_usage("claude-opus-5", 0, 0, cache_write_1h=1_000_000,
                                       pricing=PRICING, multipliers=MULTS)
    assert five_m == pytest.approx(6.25)   # 1.25x
    assert one_h == pytest.approx(10.00)   # 2.0x
    assert one_h > five_m


def test_cost_for_usage_returns_none_for_unpriced_model():
    """An unpriced model records tokens with NO cost, never a fabricated 0.0."""
    assert factory_log.cost_for_usage("who-knows-5", 1000, 1000, pricing=PRICING) is None


def test_missing_multipliers_price_cache_at_full_input_rate_not_free():
    """A missing multiplier must not silently zero-rate cached tokens."""
    cost = factory_log.cost_for_usage("claude-opus-5", 0, 0, cache_read=1_000_000,
                                      pricing=PRICING, multipliers={})
    assert cost == pytest.approx(5.00)


def test_shipped_pricing_table_has_grounded_cache_multipliers():
    m = factory_log.load_cache_multipliers()
    assert m["cache_read"] == 0.1
    assert m["cache_write_5m"] == 1.25
    assert m["cache_write_1h"] == 2.0


def test_shipped_pricing_table_prices_the_current_opus():
    """claude-opus-5 was absent, so every turn on it recorded cost_usd: null."""
    assert factory_log.cost_for("claude-opus-5", 1_000_000, 0) == pytest.approx(5.0)


# ---- transcript ingest ------------------------------------------------------

def test_ingest_reads_usage_and_prices_it(transcripts, monkeypatch, tmp_path):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", tin=1_000_000, tout=0) + "\n")

    assert factory_log.ingest_claude_transcripts(transcripts) == 1
    rec = json.loads(Path(tmp_path / "log.jsonl").read_text().strip())
    assert rec["event"] == "claude_code"
    assert rec["tokens_in"] == 1_000_000
    assert rec["cost_usd"] == pytest.approx(5.0)
    assert rec["source"] == "transcript"


def test_ingest_is_idempotent_across_reruns(transcripts, monkeypatch, tmp_path):
    """Transcripts are append-only and keep old turns; a re-run must not
    re-count them, or repeated ingests inflate cost without bound."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    f = transcripts / "proj-a" / "s.jsonl"
    f.write_text(_turn("req-1") + "\n")

    assert factory_log.ingest_claude_transcripts(transcripts) == 1
    assert factory_log.ingest_claude_transcripts(transcripts) == 0

    f.write_text(_turn("req-1") + "\n" + _turn("req-2") + "\n")
    assert factory_log.ingest_claude_transcripts(transcripts) == 1

    records = [json.loads(x) for x in
               Path(tmp_path / "log.jsonl").read_text().splitlines() if x.strip()]
    assert sorted(r["request_id"] for r in records) == ["req-1", "req-2"]


def test_ingest_splits_orchestrator_from_subagent(transcripts, monkeypatch, tmp_path):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", sidechain=False) + "\n" + _turn("req-2", sidechain=True) + "\n")
    factory_log.ingest_claude_transcripts(transcripts)

    records = [json.loads(x) for x in
               Path(tmp_path / "log.jsonl").read_text().splitlines() if x.strip()]
    assert {r["query_source"] for r in records} == {"repl_main_thread", "subagent"}


def test_ingest_folds_worktree_cwd_back_to_parent_repo(transcripts, monkeypatch, tmp_path):
    """Otherwise every worktree branch looks like its own repo."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", cwd="/repos/acme/.claude/worktrees/feat-x") + "\n")
    factory_log.ingest_claude_transcripts(transcripts)

    rec = json.loads(Path(tmp_path / "log.jsonl").read_text().strip())
    assert rec["repo"] == "acme"


def test_ingest_skips_synthetic_turns(transcripts, monkeypatch, tmp_path):
    """`<synthetic>` turns are harness-generated and were never billed."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", model="<synthetic>") + "\n")
    assert factory_log.ingest_claude_transcripts(transcripts) == 0


def test_ingest_dedup_is_order_independent_when_a_zero_line_comes_first(
        transcripts, monkeypatch, tmp_path):
    """A request's usage is repeated on every content-block line it produced, and
    a few requests also carry an all-zero line. Dedup keeps the first line seen,
    so a leading zero line must not shadow the real usage."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", tin=0, tout=0) + "\n"
        + _turn("req-1", tin=1_000_000, tout=0) + "\n")

    assert factory_log.ingest_claude_transcripts(transcripts) == 1
    rec = json.loads(Path(tmp_path / "log.jsonl").read_text().strip())
    assert rec["tokens_in"] == 1_000_000
    assert rec["cost_usd"] == pytest.approx(5.0)


def test_repeated_usage_across_content_blocks_is_counted_once(
        transcripts, monkeypatch, tmp_path):
    """Summing every line instead of deduping by request id would roughly double
    the reported cost — most requests emit 2+ lines carrying the same usage."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        (_turn("req-1", tin=1_000_000, tout=0) + "\n") * 3)

    assert factory_log.ingest_claude_transcripts(transcripts) == 1
    records = [json.loads(x) for x in
               Path(tmp_path / "log.jsonl").read_text().splitlines() if x.strip()]
    assert factory_log.aggregate(records)["claude_code_cost_usd"] == pytest.approx(5.0)


def test_ingest_survives_corrupt_lines(transcripts, monkeypatch, tmp_path):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        "not json\n" + _turn("req-1") + "\n{}\n")
    assert factory_log.ingest_claude_transcripts(transcripts) == 1


def test_ingest_feeds_end_to_end_cost(transcripts, monkeypatch, tmp_path):
    """The headline number: claude_code_cost_usd stops reading $0.00."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    (transcripts / "proj-a" / "s.jsonl").write_text(
        _turn("req-1", tin=1_000_000, tout=0) + "\n")
    factory_log.ingest_claude_transcripts(transcripts)

    records = [json.loads(x) for x in
               Path(tmp_path / "log.jsonl").read_text().splitlines() if x.strip()]
    agg = factory_log.aggregate(records)
    assert agg["claude_code_cost_usd"] == pytest.approx(5.0)
    assert agg["cost_usd_total"] == pytest.approx(5.0)
    assert agg["claude_code"]["repl_main_thread"]["calls"] == 1


# ---- distinct findings ------------------------------------------------------

def _gate(name, caught, **extra):
    return {"event": "gate", "name": name, "result": "fail", "caught": caught, **extra}


def test_caught_is_not_inflated_by_reruns_on_unchanged_state():
    """5 runs re-reporting the same 20 orphans is 20 findings, not 100."""
    agg = factory_log.aggregate([_gate("check_traceability", 20) for _ in range(5)])
    g = agg["gates"]["check_traceability"]
    assert g["caught"] == 100          # raw sum retained for back-compat
    assert g["caught_distinct"] == 20  # the honest signal
    assert agg["verdict"]["check_traceability"]["caught"] == 20


def test_finding_ids_give_exact_distinct_count():
    records = [_gate("g", 2, finding_ids=["a", "b"]),
               _gate("g", 2, finding_ids=["b", "c"])]
    g = factory_log.aggregate(records)["gates"]["g"]
    assert g["caught_distinct"] == 3           # a, b, c — b deduped
    assert g["caught_basis"] == "finding-ids"


def test_zero_yield_gate_is_still_noise_or_unpriced():
    agg = factory_log.aggregate([_gate("check_patterns", 0) for _ in range(5)])
    assert agg["gates"]["check_patterns"]["caught_distinct"] == 0
    assert agg["verdict"]["check_patterns"]["verdict"] in ("noise-candidate", "unpriced")


# ---- honest unattributed cost ----------------------------------------------

def test_unattributed_cost_is_none_not_zero():
    """A gate nothing was charged to has UNKNOWN cost, not free cost. Reporting
    $0.000/catch made every catching gate read `earning` on a fabricated
    denominator."""
    v = factory_log.aggregate([_gate("g", 3)])["verdict"]["g"]
    assert v["cost_usd"] is None
    assert v["cost_per_catch"] is None


def test_zero_yield_unattributed_gate_reads_unpriced_not_cheap():
    v = factory_log.cost_value_verdict({"g": {"runs": 5, "caught_distinct": 0}}, {})
    assert v["g"]["verdict"] == "unpriced"


def test_demote_candidate_is_reachable_once_cost_is_attributed():
    """The verdict that tells you to switch a gate off — unreachable before,
    because cost defaulted to 0.0 and it requires cost > 0."""
    v = factory_log.cost_value_verdict({"g": {"runs": 5, "caught_distinct": 0}},
                                       {"g": {"calls": 5, "cost_usd": 12.5}})
    assert v["g"]["verdict"] == "demote-candidate"


def test_measured_free_still_reads_noise_candidate():
    """An attributed cost of exactly 0.0 is a measurement and must stay
    distinguishable from 'nothing was attributed'."""
    v = factory_log.cost_value_verdict({"g": {"runs": 5, "caught_distinct": 0}},
                                       {"g": {"calls": 5, "cost_usd": 0.0}})
    assert v["g"]["verdict"] == "noise-candidate"


def test_text_render_shows_dash_not_dollar_zero_for_unattributed():
    out = factory_log.render_report(factory_log.aggregate([_gate("g", 3)]))
    assert "$0.00" not in out


# ---- session cost report (report-only) --------------------------------------

def _cc(session, *, turns=1, model="claude-opus-5", cr=0, w5=0, w1=0, tout=0, cost=0.0):
    return [{"event": "claude_code", "session_id": session, "model": model,
             "request_id": f"{session}-{i}", "tokens_in": 0, "tokens_out": tout,
             "cache_read": cr, "cache_creation": w5 + w1,
             "cache_write_5m": w5, "cache_write_1h": w1, "cost_usd": cost}
            for i in range(turns)]


def test_cost_report_is_never_a_gate():
    """Every finding is something CW cannot act on, so it must not gate."""
    rep = factory_log.session_cost_report(_cc("s", turns=3))
    assert rep["gate"] is False


def test_cost_report_splits_composition():
    rep = factory_log.session_cost_report(
        _cc("s", turns=1, cr=1_000_000, tout=1_000_000, cost=1.0))
    # 1M cache reads at 0.1x $5 = $0.50; 1M output at $25 = $25.00
    assert rep["composition_usd"]["cache_read"] == pytest.approx(0.50)
    assert rep["composition_usd"]["output"] == pytest.approx(25.0)


def test_cost_report_flags_long_sessions_with_denominators():
    records = _cc("long", turns=250, cost=1.0) + _cc("short", turns=5, cost=1.0)
    rep = factory_log.session_cost_report(records)
    f = next(x for x in rep["findings"] if x["code"] == "long-sessions")
    assert "1 of 2 session(s)" in f["detail"]      # denominator, not a bare count
    assert "$250.00" in f["detail"]


def test_cost_report_computes_amplification_and_unit_cost():
    rep = factory_log.session_cost_report(_cc("s", turns=10, cr=100_000, cost=1.0))
    s = rep["top_sessions"][0]
    assert s["amplification"] == 10.0          # 1M cache-read over a 100k window
    assert s["mean_context"] == 100_000
    # $1.00/turn carried over exactly 100k of context normalises to 1.0.
    assert s["usd_per_turn_per_100k"] == pytest.approx(1.0)


def test_cost_report_flags_1h_ttl_dominance_without_recommending_a_switch():
    rep = factory_log.session_cost_report(_cc("s", turns=5, w1=1_000_000))
    f = next(x for x in rep["findings"] if x["code"] == "cache-ttl-1h-dominant")
    assert "NOT worth switching" in f["detail"]  # advisory, not a directive


def test_cost_report_marks_legacy_ttl_records_as_a_lower_bound():
    """Records predating the TTL split price at the cheaper 5m rate, so the
    write figure understates — that must be disclosed, not hidden."""
    legacy = [{"event": "claude_code", "session_id": "s", "model": "claude-opus-5",
               "cache_creation": 1_000_000, "cache_read": 0,
               "tokens_in": 0, "tokens_out": 0}]
    rep = factory_log.session_cost_report(legacy)
    assert rep["legacy_ttl_turns"] == 1
    assert "LOWER BOUND" in factory_log.render_cost_report(rep)


def test_cost_report_counts_unpriced_turns_rather_than_pricing_them_at_zero():
    rep = factory_log.session_cost_report(_cc("s", turns=3, model="who-knows-9", cr=999))
    assert rep["unpriced_turns"] == 3


def test_cost_report_handles_an_empty_log():
    rep = factory_log.session_cost_report([])
    assert rep["turns"] == 0 and rep["findings"] == []
    assert "ingest-claude-transcripts" in factory_log.render_cost_report(rep)


# ---- sub-agent transcript discovery (chief-wiggum#345) -----------------------
#
# Claude Code writes the orchestrator's turns to `<project>/<session>.jsonl`
# but every sub-agent's turns to a DEEPER path -- `<project>/<session>/
# subagents/agent-<id>.jsonl`, and workflow sub-agents deeper still
# (`.../subagents/workflows/<wf>/agent-<id>.jsonl`). A `*/*.jsonl` glob only
# sees the first level, which is why the sub-agent layer read $0 while
# carrying ~75% of real turn volume (verified against the real corpus, see
# the implementation plan). These tests fail against the old two-level glob.

def test_ingest_finds_subagent_transcripts_at_the_third_level(monkeypatch, tmp_path):
    """This test MUST fail on the old `root.glob("*/*.jsonl")` -- neither the
    subagents/ nor the subagents/workflows/<wf>/ turn would ever be seen."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(_turn("top-1", cwd="/repos/app") + "\n")

    subagents = proj / "s" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text(
        _turn("sub-1", cwd="/repos/app", sidechain=True) + "\n")

    wf = subagents / "workflows" / "wf1"
    wf.mkdir(parents=True)
    (wf / "agent-b.jsonl").write_text(
        _turn("sub-2", cwd="/repos/app", sidechain=True) + "\n")

    n = factory_log.ingest_claude_transcripts(root)
    assert n == 3
    ids = {r["request_id"] for r in factory_log.read_log()}
    assert ids == {"top-1", "sub-1", "sub-2"}


def test_subagent_turns_land_in_the_subagent_layer(monkeypatch, tmp_path):
    """query_source must derive from isSidechain OR the /subagents/ path --
    belt-and-braces, so a sub-agent transcript missing isSidechain (older
    format) still lands in the right layer."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    subagents = root / "p" / "s" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text(
        _turn("sub-1", sidechain=True) + "\n")
    (subagents / "agent-b.jsonl").write_text(
        _turn("sub-2", sidechain=False) + "\n")  # path alone must qualify

    factory_log.ingest_claude_transcripts(root)
    recs = {r["request_id"]: r for r in factory_log.read_log()}
    assert recs["sub-1"]["query_source"] == "subagent"
    assert recs["sub-2"]["query_source"] == "subagent"


def test_no_double_count_across_transcript_levels(monkeypatch, tmp_path):
    """Request-id overlap between the orchestrator and sub-agent levels is 0 in
    the real corpus (see the plan's verification), so widening the glob cannot
    double-count -- pinned here with a fixture that deliberately reuses one id."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(_turn("dup-1", cwd="/repos/app") + "\n")
    subagents = proj / "s" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text(
        _turn("dup-1", cwd="/repos/app", sidechain=True) + "\n")

    n = factory_log.ingest_claude_transcripts(root)
    assert n == 1
    assert len([r for r in factory_log.read_log() if r["request_id"] == "dup-1"]) == 1


def test_ingest_records_cwd_and_agent_type(monkeypatch, tmp_path):
    """`cwd` lets ticket_cost's read-time window slicing recover an untagged
    turn later; `agent_type` is the sub-agent TYPE name (never message
    content) and must never be conflated with the gate-cost `skill` key."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        _turn("r1", cwd="/repos/app", agent="general-purpose") + "\n")

    factory_log.ingest_claude_transcripts(root)
    rec = json.loads(Path(tmp_path / "log.jsonl").read_text().strip())
    assert rec["cwd"] == "/repos/app"
    assert rec["agent_type"] == "general-purpose"
    assert "skill" not in rec


def test_until_ts_excludes_in_flight_turns(monkeypatch, tmp_path):
    """`--until-ts` bounds a catch-up ingest so it can never consume an
    in-flight ticket's own turns (the tag-at-ingest race, chief-wiggum#345)."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        _turn("early", ts="2026-08-03T06:00:00.000Z") + "\n"
        + _turn("late", ts="2026-08-03T07:00:00.000Z") + "\n")

    cutoff = factory_log._parse_iso_ts("2026-08-03T06:30:00.000Z")
    n = factory_log.ingest_claude_transcripts(root, until=cutoff)
    assert n == 1
    assert {r["request_id"] for r in factory_log.read_log()} == {"early"}


# ---- count_transcript_turns: independent evidence, never a write (#345 AC4) -

def test_count_transcript_turns_reports_buckets_without_writing(monkeypatch, tmp_path):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    log.write_text("")  # present but empty, so byte-identity is checkable
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(_turn("r1", sidechain=False) + "\n")
    subagents = proj / "s" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text(_turn("r2", sidechain=True) + "\n")

    before = log.read_bytes()
    result = factory_log.count_transcript_turns(root)
    assert result == {"scanned": True, "repl_main_thread": 1, "subagent": 1}
    assert log.read_bytes() == before  # never writes -- a reader, not an ingest


def test_count_transcript_turns_missing_root_is_not_scanned(tmp_path):
    result = factory_log.count_transcript_turns(tmp_path / "does-not-exist")
    assert result["scanned"] is False
    # never crash, never claim zero as if it were a measured fact


def test_count_transcript_turns_since_filters_by_turn_ts_with_mtime_as_cheap_prefilter(
        monkeypatch, tmp_path):
    """AC4 promises an IN-WINDOW denominator, so `since`/`until` must be
    enforced per-turn (symmetric with ``ingest_claude_transcripts``) — the
    file-mtime check is kept ONLY as a cheap skip-whole-file pre-filter
    (sound for append-only files: an mtime that predates the window means
    every line in the file predates it too), never a substitute for reading
    a fresh file's actual turn timestamps. Reviewer finding on chief-wiggum#345."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)

    # Fresh-mtime file with two turns straddling the window: only the
    # in-window turn's own timestamp should count, even though the FILE
    # itself is fresh enough to pass the cheap pre-filter either way.
    mixed_file = proj / "mixed.jsonl"
    mixed_file.write_text(
        _turn("in-window", cwd="/repos/app", ts="2026-08-04T12:00:00.000Z") + "\n"
        + _turn("too-early", cwd="/repos/app", ts="2026-08-01T00:00:00.000Z") + "\n")

    # Stale-mtime file: skipped UNREAD by the cheap pre-filter, even though it
    # contains a turn whose own timestamp would otherwise be in-window --
    # proves the mtime pre-filter still exists as a separate, cheaper gate.
    stale_file = proj / "stale.jsonl"
    stale_file.write_text(
        _turn("stale-file-in-window-ts", cwd="/repos/app", ts="2026-08-04T12:00:00.000Z") + "\n")
    old_mtime = time.time() - 30 * 86400
    os.utime(stale_file, (old_mtime, old_mtime))

    since = factory_log._parse_iso_ts("2026-08-02T00:00:00.000Z")
    result = factory_log.count_transcript_turns(root, since=since)
    assert result["scanned"] is True
    assert result["repl_main_thread"] == 1  # only "in-window"


def test_count_transcript_turns_unparseable_ts_excluded_when_window_active(monkeypatch, tmp_path):
    """A turn with no parseable timestamp can't be placed in the window, so it
    must not silently count towards it once since/until is active — count
    only what can be confirmed to fall inside the window."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        _turn("bad-ts", cwd="/repos/app", ts="not-a-timestamp") + "\n"
        + _turn("good-ts", cwd="/repos/app", ts="2026-08-04T12:00:00.000Z") + "\n")

    since = factory_log._parse_iso_ts("2026-08-01T00:00:00.000Z")
    result = factory_log.count_transcript_turns(root, since=since)
    assert result["repl_main_thread"] == 1  # only "good-ts"


def test_count_transcript_turns_respects_repo_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        _turn("keep", cwd="/repos/app") + "\n"
        + _turn("other-repo", cwd="/repos/other") + "\n")

    result = factory_log.count_transcript_turns(root, repo="app")
    assert result["scanned"] is True
    assert result["repl_main_thread"] == 1


def test_count_transcript_turns_cwd_prefix_does_not_cross_bill_a_lexical_sibling(
        monkeypatch, tmp_path):
    """Same #345 fix-up as the ingest/ticket_cost sites: cwd_prefix `.../t42`
    must not match cwd `.../t420` -- a bare `str.startswith` would."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "log.jsonl"))
    root = tmp_path / "projects"
    proj = root / "p"
    proj.mkdir(parents=True)
    wt = "/repos/app/.claude/worktrees/t42"
    (proj / "s.jsonl").write_text(
        _turn("r1", cwd=wt) + "\n" + _turn("r2", cwd=wt + "0") + "\n")

    result = factory_log.count_transcript_turns(root, cwd_prefix=wt)
    assert result["scanned"] is True
    assert result["repl_main_thread"] == 1

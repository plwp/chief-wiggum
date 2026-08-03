"""Tests for the gate cost/benefit signal: transcript ingest, cache-aware
pricing, distinct-finding counting, and honest unattributed cost.

These cover the three things that made `aggregate`'s verdicts unusable:
Claude Code's own cost was never ingested, `caught` re-counted the same finding
once per run, and an unattributed cost was reported as a measured $0.00.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factory_log  # noqa: E402


PRICING = {"claude-opus-5": {"input_per_mtok": 5.0, "output_per_mtok": 25.0}}
MULTS = {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}


def _turn(request_id, *, model="claude-opus-5", tin=100, tout=200, cache_read=0,
          w5=0, w1=0, sidechain=False, cwd="/repos/acme", ts="2026-08-03T06:15:00.000Z"):
    """One assistant turn as Claude Code writes it to a session transcript."""
    return json.dumps({
        "type": "assistant", "requestId": request_id, "sessionId": "sess-1",
        "timestamp": ts, "isSidechain": sidechain, "cwd": cwd,
        "message": {"model": model, "usage": {
            "input_tokens": tin, "output_tokens": tout,
            "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": w5,
                               "ephemeral_1h_input_tokens": w1},
        }},
    })


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

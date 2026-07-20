"""Tests for scripts/factory_log.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import factory_log  # noqa: E402


def test_emit_is_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    monkeypatch.delenv("CW_FACTORY_LOG", raising=False)
    assert factory_log.emit(factory_log.GATE, name="x", result="pass") is False


def test_emit_writes_when_enabled(tmp_path, monkeypatch):
    log = tmp_path / "factory-log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    assert factory_log.emit(factory_log.GATE, name="ratchet", result="pass", repo="acme/app", caught=2, ts=1.0)
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "gate" and rec["name"] == "ratchet" and rec["caught"] == 2
    assert rec["ts"] == 1.0
    # None-valued fields are omitted
    assert "ticket" not in rec


def test_gate_timer_emits_with_duration(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    with factory_log.gate_timer("check_patterns", repo="acme/app") as g:
        g.caught = 3
        g.result = "fail"
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["name"] == "check_patterns" and rec["result"] == "fail" and rec["caught"] == 3
    assert "duration_ms" in rec


def test_gate_timer_records_error_on_exception(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    try:
        with factory_log.gate_timer("g", repo="r"):
            raise ValueError("boom")
    except ValueError:
        pass
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["result"] == "error"


def test_aggregate_gate_value_and_cost():
    records = [
        {"event": "gate", "name": "ratchet", "result": "pass", "caught": 0, "duration_ms": 10, "repo": "a"},
        {"event": "gate", "name": "ratchet", "result": "pass", "caught": 0, "duration_ms": 10, "repo": "a"},
        {"event": "gate", "name": "ratchet", "result": "fail", "caught": 1, "duration_ms": 10, "repo": "a"},
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": "a"},
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": "a"},
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": "a"},
        {"event": "consult", "provider": "opus", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.02, "repo": "a"},
    ]
    agg = factory_log.aggregate(records)
    assert agg["gates"]["ratchet"]["caught"] == 1
    assert agg["gates"]["ratchet"]["value"] == "earning"
    assert agg["gates"]["noisy"]["value"] == "noise-candidate"  # 3 runs, 0 caught
    assert agg["consults"]["opus"]["cost_usd"] == 0.02
    assert agg["cost_usd_total"] == 0.02


def test_aggregate_filters_by_repo():
    records = [
        {"event": "gate", "name": "g", "result": "pass", "repo": "a"},
        {"event": "gate", "name": "g", "result": "pass", "repo": "b"},
    ]
    assert factory_log.aggregate(records, repo="a")["gates"]["g"]["runs"] == 1


def test_pricing_table_has_grounded_anthropic_rows():
    table = factory_log.load_pricing()
    # authoritative rows (claude-api reference) must be present and priced
    for m in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-fable-5"):
        assert table[m]["verified"] is True
        assert table[m]["input_per_mtok"] and table[m]["output_per_mtok"]


def test_cost_for_computes_from_grounded_pricing():
    # Opus 4.8 = $5 / $25 per MTok
    cost = factory_log.cost_for("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == 30.0
    cost = factory_log.cost_for("claude-haiku-4-5", 2_000_000, 0)  # $1/MTok in
    assert cost == 2.0


def test_cost_for_returns_none_for_unpriced_or_unknown():
    assert factory_log.cost_for("codex", 1000, 1000) is None  # null price (unmapped model)
    assert factory_log.cost_for("some-unknown-model", 1000, 1000) is None


def test_cost_for_cross_provider_grounded():
    # each fetched from the vendor's live pricing page
    assert factory_log.cost_for("gpt-5.4", 1_000_000, 1_000_000) == 17.5      # 2.5 + 15
    assert factory_log.cost_for("gemini-2.5-flash", 1_000_000, 1_000_000) == 2.8  # 0.3 + 2.5
    assert factory_log.cost_for("glm-4.6", 1_000_000, 1_000_000) == 2.8       # 0.6 + 2.2
    assert factory_log.cost_for("claude-sonnet-5", 1_000_000, 1_000_000) == 12.0  # 2 + 10 (intro)


def test_emit_consult_records_tokens_and_grounded_cost(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-014 INV-fh-002
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("anthropic", "claude-opus-4-8", 1_000_000, 1_000_000, repo="acme/app")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "consult" and rec["provider"] == "anthropic"
    assert rec["tokens_in"] == 1_000_000 and rec["cost_usd"] == 30.0


def test_emit_consult_omits_cost_when_unpriced(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-014 INV-fh-002
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("openai", "gpt-5.9-unmapped", 1000, 500)  # resolved but unpriced model
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["tokens_in"] == 1000
    assert "cost_usd" not in rec  # unpriced -> no fabricated dollar figure


def test_emit_consult_without_tokens_records_frequency(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-011
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("codex", None, repo="dogeared-coach")  # CLI provider, usage not surfaced
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "consult" and rec["provider"] == "codex" and rec["repo"] == "dogeared-coach"
    assert "tokens_in" not in rec and "cost_usd" not in rec  # no fabricated count/cost


# --- ConsultUsageRecord honesty invariants (chief-wiggum#134) ---------------


def test_emit_consult_rejects_bare_cli_alias_as_resolved_model(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-013
    # 'codex'/'gemini'/'claude'/'claude-interactive'/'gemini-vertex'
    # are tool labels, never a billed model id — a caller passing one has failed
    # to resolve the model, and that must not silently become an unpriced-looking
    # record (indistinguishable from a genuinely unpriced model).
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    for alias in ("codex", "gemini", "claude", "claude-interactive", "gemini-vertex"):
        with pytest.raises(ValueError):
            factory_log.emit_consult("codex", alias, 100, 50)
    assert not log.exists() or log.read_text() == ""


def test_emit_consult_both_tokens_or_null_downgrades_partial(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-011
    # a one-sided token count (only one of in/out known) must never
    # be half-priced — both null out, and a claimed real source downgrades to
    # 'partial'.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("claude", "claude-sonnet-5", 100, None, usage_status="provider-json")
    rec = json.loads(log.read_text().splitlines()[0])
    assert "tokens_in" not in rec and "tokens_out" not in rec
    assert rec["usage_status"] == "partial"
    assert "cost_usd" not in rec


def test_emit_consult_records_adapter_usage_status_requested_model_and_pricing_version(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-002
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult(
        "claude", "claude-sonnet-5", 1000, 500, usage_status="provider-json",
        adapter="claude-cli", requested_model=None, repo="acme/app", ticket="42",
    )
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["adapter"] == "claude-cli"
    assert rec["usage_status"] == "provider-json"
    assert rec["ticket"] == "42"
    assert rec["pricing_version"] == factory_log.stable_hash(factory_log.PRICING_PATH.read_text())
    assert "requested_model" not in rec  # None omitted, same as any other field


def test_emit_consult_unknown_usage_status_is_dropped_not_trusted(tmp_path, monkeypatch):
    # @cw-trace verifies INV-fh-011
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("claude", "claude-sonnet-5", 100, 50, usage_status="made-up-status")
    rec = json.loads(log.read_text().splitlines()[0])
    assert "usage_status" not in rec
    # tokens/cost are unaffected — only the bogus status label is dropped
    assert rec["tokens_in"] == 100 and rec["cost_usd"] is not None


def test_emit_consult_unavailable_status_records_no_tokens_no_cost(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-011
    # IT-fh-05: usage-absent sample -> tokens=None, cost=None, usage_status='unavailable'.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("claude-interactive", None, usage_status="unavailable", repo="acme/app")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["usage_status"] == "unavailable"
    assert "tokens_in" not in rec and "cost_usd" not in rec


def test_emit_consult_coerces_string_token_counts(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-014 INV-fh-002
    # P3 regression (PR #195 review): numeric-string counts from a drifted
    # provider payload are coerced at the boundary and priced normally.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("claude", "claude-opus-4-8", "1000000", "1000000",
                             usage_status="provider-json")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["tokens_in"] == 1_000_000 and rec["tokens_out"] == 1_000_000
    assert rec["cost_usd"] == 30.0


def test_emit_consult_malformed_tokens_degrade_event_never_vanish_it(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-011
    # P3 regression: a malformed (non-numeric) token count must degrade the
    # EVENT (tokens nulled, status downgraded to partial) — never raise, which
    # would silently drop the whole record inside _emit_consult_telemetry's
    # swallow-all wrapper.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("codex", "gpt-5.5", "lots", 19, usage_status="provider-json")
    rec = json.loads(log.read_text().splitlines()[0])  # event WAS emitted
    assert rec["event"] == "consult"
    assert "tokens_in" not in rec and "tokens_out" not in rec
    assert rec["usage_status"] == "partial"
    assert "cost_usd" not in rec


def test_emit_consult_cost_for_exception_degrades_cost_never_vanishes_event(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-015 INV-fh-011
    # P3 regression: a raising cost derivation (e.g. a broken pricing row) must
    # yield cost_usd null on an otherwise-complete record, not a lost event.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))

    def broken_cost_for(model, tin, tout, pricing=None):
        raise RuntimeError("pricing table corrupted")

    monkeypatch.setattr(factory_log, "cost_for", broken_cost_for)
    factory_log.emit_consult("claude", "claude-opus-4-8", 1000, 500, usage_status="provider-json")
    rec = json.loads(log.read_text().splitlines()[0])  # event WAS emitted
    assert rec["tokens_in"] == 1000 and rec["tokens_out"] == 500
    assert rec["usage_status"] == "provider-json"
    assert "cost_usd" not in rec


def test_emit_consult_resolved_but_unpriced_model_still_records_tokens(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-014 INV-fh-002
    # ADR-fh-05: a resolved-but-unpriced model (e.g. codex's live-resolved gpt-*
    # id before that row exists) still records real tokens with cost_usd null —
    # the honest degradation, never a skipped record.
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("codex", "gpt-5.9-not-yet-priced", 12844, 19, usage_status="provider-json")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["tokens_in"] == 12844 and rec["tokens_out"] == 19
    assert "cost_usd" not in rec


def test_ingest_claude_code_folds_api_requests(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    otel = tmp_path / "otel.jsonl"
    otel.write_text("\n".join([
        json.dumps({"event.name": "api_request", "model": "claude-opus-4-8",
                    "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.0175,
                    "query_source": "repl_main_thread", "session.id": "s1"}),
        json.dumps({"event.name": "api_request", "model": "claude-haiku-4-5",
                    "input_tokens": 2000, "output_tokens": 100, "cost_usd": 0.0025,
                    "query_source": "subagent", "session.id": "s1"}),
        json.dumps({"event.name": "tool_decision", "tool_name": "Bash"}),  # ignored
        "not json",  # skipped
    ]) + "\n")
    n = factory_log.ingest_claude_code(otel, repo="dogeared-coach")
    assert n == 2
    agg = factory_log.aggregate(factory_log.read_log())
    assert agg["claude_code"]["repl_main_thread"]["cost_usd"] == 0.0175
    assert agg["claude_code"]["subagent"]["tokens_in"] == 2000
    assert agg["claude_code_cost_usd"] == 0.02  # 0.0175 + 0.0025
    assert agg["cost_usd_total"] == 0.02  # end-to-end (no consults here)


def test_verdict_excludes_pure_cost_build_loops():
    # `implement` has cost but no gate events -> it's build cost, not a validation
    v = factory_log.cost_value_verdict(
        {"code-review": {"runs": 2, "caught": 3}},
        {"code-review": {"calls": 2, "cost_usd": 0.6}, "implement": {"calls": 5, "cost_usd": 3.0}})
    assert "implement" not in v and "code-review" in v


def test_render_report_shows_verdict_and_cost():
    agg = factory_log.aggregate([
        {"event": "gate", "name": "browser-validate", "result": "pass", "caught": 0, "repo": "r"},
        {"event": "gate", "name": "browser-validate", "result": "pass", "caught": 0, "repo": "r"},
        {"event": "gate", "name": "browser-validate", "result": "pass", "caught": 0, "repo": "r"},
        {"event": "claude_code", "cost_usd": 0.15, "query_source": "subagent", "skill": "browser-validate", "repo": "r"},
        {"event": "gate", "name": "code-review", "result": "fail", "caught": 4, "repo": "r"},
    ], repo="r")
    report = factory_log.render_report(agg, repo="r")
    assert "Factory cost/value report — r" in report
    assert "demote-candidate" in report and "browser-validate" in report
    assert "VALIDATION" in report and "$/CATCH" in report


def test_cost_value_verdict():
    """Every validation costed + its value quantified into a keep/demote verdict."""
    gates = {
        "check_patterns": {"runs": 5, "caught": 2, "total_ms": 40},   # free, catches -> earning
        "expensive_noise": {"runs": 4, "caught": 0, "total_ms": 0},   # runs but nothing; paid via loop
        "cheap_noise": {"runs": 4, "caught": 0, "total_ms": 5},       # runs, nothing, ~free
    }
    by_loop = {"expensive_noise": {"calls": 4, "cost_usd": 1.20}}
    v = factory_log.cost_value_verdict(gates, by_loop)
    assert v["check_patterns"]["verdict"] == "earning" and v["check_patterns"]["cost_per_catch"] == 0.0
    assert v["expensive_noise"]["verdict"] == "demote-candidate"  # $1.20 over 4 runs, 0 catches
    assert v["cheap_noise"]["verdict"] == "noise-candidate"       # noisy but free
    # an LLM validation that caught something -> cost_per_catch
    v2 = factory_log.cost_value_verdict(
        {"code-review": {"runs": 2, "caught": 4}}, {"code-review": {"calls": 2, "cost_usd": 0.80}})
    assert v2["code-review"]["verdict"] == "earning"
    assert v2["code-review"]["cost_per_catch"] == 0.2  # $0.80 / 4 findings


def test_cost_attributed_per_loop_via_skill(tmp_path, monkeypatch):
    """The end state: every loop/validation is costed (via skill.name/agent.name)."""
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    otel = tmp_path / "otel.jsonl"
    otel.write_text("\n".join(json.dumps(x) for x in [
        {"event.name": "api_request", "cost_usd": 0.42, "query_source": "subagent", "skill.name": "code-review"},
        {"event.name": "api_request", "cost_usd": 0.05, "query_source": "subagent", "skill.name": "code-review"},
        {"event.name": "api_request", "cost_usd": 0.31, "query_source": "subagent", "agent.name": "architect"},
    ]) + "\n")
    factory_log.ingest_claude_code(otel)
    loops = factory_log.aggregate(factory_log.read_log())["cost_by_loop"]
    assert loops["code-review"] == {"calls": 2, "cost_usd": 0.47}
    assert loops["architect"] == {"calls": 1, "cost_usd": 0.31}


def test_ingest_tolerates_otlp_attributes_shape(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    otel = tmp_path / "otel.jsonl"
    # OTLP-style: fields under an `attributes` dict, event name under `name`
    otel.write_text(json.dumps({
        "name": "api_request",
        "attributes": {"model": "claude-sonnet-5", "input_tokens": 10,
                       "output_tokens": 5, "cost_usd": 0.0001, "query_source": "subagent"}}) + "\n")
    assert factory_log.ingest_claude_code(otel) == 1
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "claude_code" and rec["model"] == "claude-sonnet-5"


def test_ingest_writes_without_telemetry_enabled(tmp_path, monkeypatch):
    # explicit ingest always writes (unlike passive emit)
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))  # sets the path (also enables, but ingest doesn't depend on it)
    otel = tmp_path / "o.jsonl"
    otel.write_text(json.dumps({"event.name": "api_request", "model": "m", "cost_usd": 0.01}) + "\n")
    assert factory_log.ingest_claude_code(otel) == 1


def test_cw_gates_emit_telemetry_when_enabled(tmp_path):
    """The CW gate suite emits gate events under telemetry, feeding the verdict."""
    import os
    log = tmp_path / "gates.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    for gate in ("check_patterns.py", "check_portability.py", "check_cw_standards.py"):
        subprocess.run([sys.executable, str(SCRIPTS / gate)], capture_output=True, text=True, env=env)
    names = {json.loads(ln)["name"] for ln in log.read_text().splitlines()
             if json.loads(ln).get("event") == "gate"}
    assert {"check_patterns", "check_portability", "check_cw_standards"} <= names


def test_cli_emit_disabled_returns_1(tmp_path, monkeypatch):
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("CW_TELEMETRY", "CW_FACTORY_LOG")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "emit", "--event", "gate", "--name", "x", "--result", "pass"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 1
    assert "telemetry disabled" in proc.stderr


def test_cli_emit_and_aggregate(tmp_path):
    import os
    log = tmp_path / "f.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    subprocess.run([sys.executable, str(SCRIPTS / "factory_log.py"), "emit",
                    "--event", "gate", "--name", "ratchet", "--result", "pass", "--caught", "0", "--repo", "a"],
                   check=True, env=env)
    proc = subprocess.run([sys.executable, str(SCRIPTS / "factory_log.py"), "aggregate", "--repo", "a"],
                          capture_output=True, text=True, env=env, check=True)
    assert json.loads(proc.stdout)["gates"]["ratchet"]["runs"] == 1


# ---- escape events -----------------------------------------------------------

def test_emit_escape_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    monkeypatch.delenv("CW_FACTORY_LOG", raising=False)
    assert factory_log.emit_escape(
        "bug", severity="high", missed_by="ticket-gate", found_in="close-epic-review") is False


def test_emit_escape_writes_well_formed_record(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    ok = factory_log.emit_escape(
        "reset endpoint leaks account existence via timing", severity="high",
        missed_by="ticket-gate", found_in="close-epic-review", repo="acme/app",
        ticket="42", invariant="INV-012", fixed=True)
    assert ok
    rec = json.loads(log.read_text().splitlines()[0])
    ts = rec.pop("ts")
    assert isinstance(ts, float)
    assert rec == {
        "event": "escape",
        "summary": "reset endpoint leaks account existence via timing",
        "severity": "high", "missed_by": "ticket-gate", "found_in": "close-epic-review",
        "repo": "acme/app", "ticket": "42", "invariant": "INV-012", "fixed": True,
    }


def test_emit_escape_omits_optional_none_fields(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_escape("bug", severity="low", missed_by="ratchet", found_in="manual", repo="a")
    rec = json.loads(log.read_text().splitlines()[0])
    assert "ticket" not in rec and "invariant" not in rec and "fixed" not in rec


def test_cli_bug_writes_escape_record(tmp_path):
    import os
    log = tmp_path / "f.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "acme/app", "--summary", "IDOR on invoice download",
         "--severity", "critical", "--missed-by", "ticket-gate",
         "--found-in", "close-epic-review", "--ticket", "42", "--fixed"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "escape"
    assert rec["summary"] == "IDOR on invoice download"
    assert rec["severity"] == "critical"
    assert rec["missed_by"] == "ticket-gate"
    assert rec["found_in"] == "close-epic-review"
    assert rec["ticket"] == "42"
    assert rec["fixed"] is True


def test_cli_bug_disabled_returns_1():
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("CW_TELEMETRY", "CW_FACTORY_LOG")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "a", "--summary", "x", "--severity", "low",
         "--missed-by", "ticket-gate", "--found-in", "manual"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 1
    assert "telemetry disabled" in proc.stderr


def test_cli_bug_rejects_invalid_severity():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "a", "--summary", "x", "--severity", "extreme",
         "--missed-by", "ticket-gate", "--found-in", "manual"],
        capture_output=True, text=True)
    assert proc.returncode != 0


def test_aggregate_counts_escapes_per_missed_by_and_computes_recall():
    records = [
        {"event": "gate", "name": "ticket-gate", "result": "pass", "caught": 2, "repo": "a"},
        {"event": "escape", "summary": "bug1", "severity": "high", "missed_by": "ticket-gate",
         "found_in": "close-epic-review", "fixed": True, "repo": "a"},
        {"event": "escape", "summary": "bug2", "severity": "critical", "missed_by": "ticket-gate",
         "found_in": "close-epic-review", "repo": "a"},
        {"event": "escape", "summary": "bug3", "severity": "low", "missed_by": "saas-gate",
         "found_in": "manual", "repo": "a"},
    ]
    agg = factory_log.aggregate(records)
    # ticket-gate: caught 2, escaped 2 -> recall 0.5
    assert agg["escapes"]["ticket-gate"]["escaped"] == 2
    assert agg["escapes"]["ticket-gate"]["caught"] == 2
    assert agg["escapes"]["ticket-gate"]["fixed"] == 1
    assert agg["escapes"]["ticket-gate"]["recall"] == 0.5
    assert agg["escapes"]["ticket-gate"]["by_severity"] == {"high": 1, "critical": 1}
    # saas-gate: no gate events at all -> caught 0, escaped 1 -> recall 0.0
    assert agg["escapes"]["saas-gate"]["caught"] == 0
    assert agg["escapes"]["saas-gate"]["recall"] == 0.0
    assert agg["escapes_total"] == 3
    # existing gate/consult aggregation is untouched
    assert agg["gates"]["ticket-gate"]["caught"] == 2


def test_aggregate_escapes_do_not_break_existing_gate_aggregation():
    records = [
        {"event": "gate", "name": "ratchet", "result": "pass", "caught": 0, "duration_ms": 10, "repo": "a"},
        {"event": "gate", "name": "ratchet", "result": "fail", "caught": 1, "duration_ms": 10, "repo": "a"},
        {"event": "consult", "provider": "opus", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.02, "repo": "a"},
        {"event": "escape", "summary": "unrelated bug", "severity": "medium", "missed_by": "traceability",
         "found_in": "close-epic-review", "repo": "a"},
    ]
    agg = factory_log.aggregate(records)
    assert agg["gates"]["ratchet"]["caught"] == 1
    assert agg["gates"]["ratchet"]["value"] == "earning"
    assert agg["consults"]["opus"]["cost_usd"] == 0.02
    assert agg["escapes"]["traceability"]["escaped"] == 1
    assert "ratchet" not in agg["escapes"]


def test_render_report_shows_escapes_and_recall():
    agg = factory_log.aggregate([
        {"event": "gate", "name": "ticket-gate", "result": "pass", "caught": 2, "repo": "r"},
        {"event": "escape", "summary": "bug", "severity": "high", "missed_by": "ticket-gate",
         "found_in": "close-epic-review", "repo": "r"},
    ], repo="r")
    report = factory_log.render_report(agg, repo="r")
    assert "Escapes" in report and "ticket-gate" in report
    assert "RECALL" in report


# ---- demotion (docs/gate-validation.md, #168) ---------------------------------


def _write_validation_record(tmp_path, gate: str, seed_classes: list[str],
                             expected: str = "fire") -> Path:
    result = "fired" if expected == "fire" else "not-fired"
    vdir = tmp_path / "validation"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"{gate}.json").write_text(json.dumps({
        "gate": gate,
        "seeded_defect_trials": [
            {"seed_id": f"{c}-1", "seed_class": c, "repo": "r", "expected": expected,
             "result": result, "passed": True}
            for c in seed_classes
        ],
    }))
    return vdir


def test_demotion_check_none_without_seed_class(tmp_path):
    vdir = _write_validation_record(tmp_path, "check_single_writer", ["evasion-omission"])
    assert factory_log.demotion_check("check_single_writer", None, vdir) is None


def test_demotion_check_none_without_record(tmp_path):
    assert factory_log.demotion_check("check_single_writer", "evasion-omission",
                                       tmp_path / "no-such-dir") is None


def test_demotion_check_none_when_class_not_validated(tmp_path):
    vdir = _write_validation_record(tmp_path, "check_single_writer", ["evasion-omission"])
    assert factory_log.demotion_check("check_single_writer", "evasion-concurrency", vdir) is None


def test_demotion_check_fires_when_class_was_validated(tmp_path):
    vdir = _write_validation_record(tmp_path, "check_single_writer", ["evasion-omission"])
    demotion = factory_log.demotion_check("check_single_writer", "evasion-omission", vdir)
    assert demotion is not None
    assert demotion["gate"] == "check_single_writer"
    assert demotion["seed_class"] == "evasion-omission"
    assert "report-only" in demotion["instruction"]
    assert "check_single_writer" in demotion["instruction"]


def test_demotion_check_none_for_certified_no_fire_class(tmp_path):
    """A passing expected:"no-fire" trial certifies a documented NON-coverage
    boundary (e.g. evasion-sampling-gap proving vendor/ is out of scope). An
    escape through that boundary is consistent with the record's authority
    statement — it must NOT demote the gate."""
    vdir = _write_validation_record(tmp_path, "check_single_writer",
                                    ["evasion-sampling-gap"], expected="no-fire")
    assert factory_log.demotion_check(
        "check_single_writer", "evasion-sampling-gap", vdir) is None


def test_demotion_check_none_when_certified_fire_trial_is_forged(tmp_path):
    """A trial with passed:true but result:not-fired never certified a catch —
    it must not ground a demotion either (derived, not trusted)."""
    vdir = tmp_path / "validation"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "check_single_writer.json").write_text(json.dumps({
        "gate": "check_single_writer",
        "seeded_defect_trials": [
            {"seed_id": "x-1", "seed_class": "evasion-omission", "repo": "r",
             "expected": "fire", "result": "not-fired", "passed": True},
        ],
    }))
    assert factory_log.demotion_check(
        "check_single_writer", "evasion-omission", vdir) is None


def test_emit_demotion_writes_record(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    assert factory_log.emit_demotion("check_single_writer", "evasion-omission", repo="acme/app")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "demotion"
    assert rec["name"] == "check_single_writer"
    assert rec["details"] == "seed_class=evasion-omission"


# ---- emit_stale_demotion: chief-wiggum#198 / IT-fh-06 ------------------------
#
# The GENERIC demotion path — no seed_class, no escape, just a blocking gate's
# validation record going stale or missing/invalid (state-machines.json's
# Gate Blocking-Authority Lifecycle, G-008/G-014). Distinct from
# `emit_demotion` above, which requires a `seed_class` an escape-driven
# demotion always has and a staleness demotion never does.


def test_emit_stale_demotion_writes_generic_record(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    assert factory_log.emit_stale_demotion(
        "ratchet", "stale", previous_authority="blocking", repo="acme/app", ticket="198")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "demotion"
    assert rec["name"] == "ratchet"
    assert rec["details"] == "stale"
    assert rec["previous_authority"] == "blocking"
    assert rec["ticket"] == "198"
    # never the escape-driven emit_demotion's seed_class= detail shape
    assert not rec["details"].startswith("seed_class=")


def test_emit_stale_demotion_record_missing_variant(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    assert factory_log.emit_stale_demotion("ratchet", "record_missing", previous_authority="blocking")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["details"] == "record_missing"


def test_emit_stale_demotion_rejects_unknown_reason():
    with pytest.raises(AssertionError):
        factory_log.emit_stale_demotion("ratchet", "something-else")


def test_emit_stale_demotion_is_noop_without_telemetry_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("CW_FACTORY_LOG", raising=False)
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    assert factory_log.emit_stale_demotion("ratchet", "stale") is False


def test_cli_bug_with_unvalidated_seed_class_prints_no_demotion(tmp_path):
    import os
    log = tmp_path / "f.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "acme/app", "--summary", "x", "--severity", "low",
         "--missed-by", "check_single_writer", "--seed-class", "evasion-omission",
         "--validation-dir", str(tmp_path / "does-not-exist"), "--found-in", "manual"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "DEMOTION" not in proc.stderr


def test_cli_bug_with_validated_seed_class_triggers_demotion(tmp_path):
    import os
    vdir = _write_validation_record(tmp_path, "check_single_writer", ["evasion-omission"])
    log = tmp_path / "f.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "acme/app", "--summary", "x", "--severity", "high",
         "--missed-by", "check_single_writer", "--seed-class", "evasion-omission",
         "--validation-dir", str(vdir), "--found-in", "close-epic-review", "--ticket", "42"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "DEMOTION" in proc.stderr
    assert "check_single_writer" in proc.stderr
    records = [json.loads(ln) for ln in log.read_text().splitlines()]
    events = {r["event"] for r in records}
    assert events == {"escape", "demotion"}
    # the escape event itself carries the seed_class, so aggregation/audit can
    # join escapes to the seed classes they refute without re-parsing stderr
    escape = next(r for r in records if r["event"] == "escape")
    assert escape["seed_class"] == "evasion-omission"


def test_emit_escape_records_seed_class(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_escape("bug", severity="high", missed_by="check_single_writer",
                             found_in="manual", repo="a", seed_class="evasion-omission")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["seed_class"] == "evasion-omission"


def test_cli_bug_seed_class_in_escape_even_without_demotion(tmp_path):
    """--seed-class is recorded on the escape event even when no validation
    record exists (no demotion) — the tag is telemetry, not just a trigger."""
    import os
    log = tmp_path / "f.jsonl"
    env = {**os.environ, "CW_FACTORY_LOG": str(log)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "acme/app", "--summary", "x", "--severity", "low",
         "--missed-by", "check_single_writer", "--seed-class", "evasion-omission",
         "--validation-dir", str(tmp_path / "no-records"), "--found-in", "manual"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "escape" and rec["seed_class"] == "evasion-omission"


def test_cli_bug_demotion_instruction_printed_even_when_telemetry_disabled(tmp_path):
    """The demotion instruction is a structural check against the validation
    record, independent of whether telemetry logging is opted in — it must not
    be silenced just because CW_TELEMETRY/CW_FACTORY_LOG are unset."""
    import os
    vdir = _write_validation_record(tmp_path, "check_single_writer", ["evasion-omission"])
    env = {k: v for k, v in os.environ.items() if k not in ("CW_TELEMETRY", "CW_FACTORY_LOG")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_log.py"), "bug",
         "--repo", "acme/app", "--summary", "x", "--severity", "high",
         "--missed-by", "check_single_writer", "--seed-class", "evasion-omission",
         "--validation-dir", str(vdir), "--found-in", "manual"],
        capture_output=True, text=True, env=env,
    )
    assert "DEMOTION" in proc.stderr
    assert proc.returncode == 1  # escape itself still couldn't be logged (telemetry off)
    assert "telemetry disabled" in proc.stderr


# ---- query telemetry (code_query.py, #159) -------------------------------------


def test_emit_query_writes_verb_path_and_hit_count(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    assert factory_log.emit_query("orient", repo="acme/app", path="src/order.py", hit_count=3)
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "query"
    assert rec["verb"] == "orient"
    assert rec["path"] == "src/order.py"
    assert rec["hit_count"] == 3


def test_emit_query_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    monkeypatch.delenv("CW_FACTORY_LOG", raising=False)
    assert factory_log.emit_query("writers", repo="acme/app") is False


def test_aggregate_counts_queries_per_verb():
    records = [
        {"event": "query", "verb": "orient", "hit_count": 4},
        {"event": "query", "verb": "orient", "hit_count": 0},
        {"event": "query", "verb": "writers", "hit_count": 2},
    ]
    agg = factory_log.aggregate(records)
    assert agg["queries"]["orient"] == {"calls": 2, "hits": 1, "misses": 1}
    assert agg["queries"]["writers"] == {"calls": 1, "hits": 1, "misses": 0}
    assert agg["queries_total"] == 3


def test_render_report_shows_query_verbs():
    agg = factory_log.aggregate([
        {"event": "query", "verb": "orient", "hit_count": 4, "repo": "r"},
    ], repo="r")
    report = factory_log.render_report(agg, repo="r")
    assert "code_query.py verbs" in report
    assert "orient" in report

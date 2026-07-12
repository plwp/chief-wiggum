"""Tests for scripts/factory_log.py."""

import json
import subprocess
import sys
from pathlib import Path

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
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("anthropic", "claude-opus-4-8", 1_000_000, 1_000_000, repo="acme/app")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "consult" and rec["provider"] == "anthropic"
    assert rec["tokens_in"] == 1_000_000 and rec["cost_usd"] == 30.0


def test_emit_consult_omits_cost_when_unpriced(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("openai", "codex", 1000, 500)  # codex model unmapped -> unpriced
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["tokens_in"] == 1000
    assert "cost_usd" not in rec  # unpriced -> no fabricated dollar figure


def test_emit_consult_without_tokens_records_frequency(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log.emit_consult("codex", None, repo="dogeared-coach")  # CLI provider, usage not surfaced
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["event"] == "consult" and rec["provider"] == "codex" and rec["repo"] == "dogeared-coach"
    assert "tokens_in" not in rec and "cost_usd" not in rec  # no fabricated count/cost


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

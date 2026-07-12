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

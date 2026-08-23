"""Phase latency reaches the ledger from a skill (chief-wiggum#375 proposal 6).

`emit_phase`, `phase_timer` and `phase_summary` all existed and **nothing
called any of them** — the fourth "built but never wired" in this family. Worse,
`emit --event` did not accept `phase` at all, so a skill could not have emitted
one even deliberately: the skills are bash, and the Python API was unreachable
from where the phases actually run.

#375 records the consequence plainly: a live run took ~90 minutes, the phase
costs were reconstructed by hand afterwards, and "there's no data to rank these
fixes". These tests keep the path from a bash phase boundary to a per-phase
rollup open end to end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import factory_log  # noqa: E402


def _run(*args, log: Path, env_extra: dict | None = None):
    import os
    env = {**os.environ, "CW_FACTORY_LOG": str(log), **(env_extra or {})}
    return subprocess.run([sys.executable, str(SCRIPTS / "factory_log.py"), *args],
                          capture_output=True, text=True, env=env)


def _records(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# --- the CLI a skill actually uses --------------------------------------------

def test_phase_verb_records_an_explicit_duration(tmp_path):
    log = tmp_path / "f.jsonl"
    result = _run("phase", "--name", "step4a_consults", "--duration-ms", "1500",
                  "--ticket", "375", log=log)
    assert result.returncode == 0, result.stderr
    rec = _records(log)[0]
    assert rec["event"] == "phase"
    assert rec["phase"] == "step4a_consults"
    assert rec["duration_ms"] == 1500.0
    assert rec["ticket"] == "375"
    assert rec["outcome"] == "ok"


def test_now_and_since_round_trip(tmp_path):
    """The pattern the skill uses: stamp with `now`, record with `--since`."""
    log = tmp_path / "f.jsonl"
    started = _run("now", log=log)
    assert started.returncode == 0
    t0 = float(started.stdout.strip())

    # Backdate by a known amount. Asserting `>= 0` passes when --since is
    # ignored entirely and every phase records as zero — which is exactly what
    # mutation testing caught this assertion doing.
    result = _run("phase", "--name", "step7_review_quorum",
                  "--since", str(t0 - 2.0), log=log)
    assert result.returncode == 0, result.stderr
    rec = _records(log)[0]
    assert rec["phase"] == "step7_review_quorum"
    assert 1900.0 <= rec["duration_ms"] <= 4000.0, (
        f"backdated 2s and recorded {rec['duration_ms']}ms — --since is not "
        f"being used to compute the duration")


def test_now_is_a_verb_because_bsd_date_has_no_millis(tmp_path):
    """CW targets macOS, where `date +%s%3N` prints a literal 3N. The verb is
    why the skill's timing is portable rather than silently wrong."""
    log = tmp_path / "f.jsonl"
    out = _run("now", log=log).stdout.strip()
    assert float(out) > 1_600_000_000  # a plausible epoch, not "%3N"


def test_a_phase_that_blew_up_is_not_recorded_as_a_good_one(tmp_path):
    """A phase that failed fast would otherwise read as a phase that went well
    — the same conflation the gate outcomes keep apart."""
    log = tmp_path / "f.jsonl"
    assert _run("phase", "--name", "step4a_consults", "--duration-ms", "40",
                "--outcome", "error", log=log).returncode == 0
    assert _records(log)[0]["outcome"] == "error"


def test_neither_since_nor_duration_is_a_usage_error(tmp_path):
    log = tmp_path / "f.jsonl"
    result = _run("phase", "--name", "x", log=log)
    assert result.returncode == 2
    assert "--since" in result.stderr


def test_detail_is_carried_through(tmp_path):
    log = tmp_path / "f.jsonl"
    _run("phase", "--name", "step4a_consults", "--duration-ms", "10",
         "--detail", "3 providers, 1 exhausted", log=log)
    assert _records(log)[0]["detail"] == "3 providers, 1 exhausted"


def test_emit_accepts_phase_as_an_event(tmp_path):
    """The gap that made the Python API unreachable from bash: `phase` was not
    in `emit --event`'s choices."""
    log = tmp_path / "f.jsonl"
    result = _run("emit", "--event", "phase", "--name", "x", "--duration-ms", "5", log=log)
    assert result.returncode == 0, result.stderr


# --- telemetry stays optional and never breaks the loop -----------------------

def test_phase_is_a_noop_when_telemetry_is_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("CW_TELEMETRY", raising=False)
    monkeypatch.delenv("CW_FACTORY_LOG", raising=False)
    assert factory_log.emit_phase("x", duration_ms=1.0) is False


# --- the rollup ---------------------------------------------------------------

def test_phase_summary_totals_and_names_the_slowest():
    records = [
        {"event": "phase", "phase": "consults", "duration_ms": 1000.0},
        {"event": "phase", "phase": "consults", "duration_ms": 500.0},
        {"event": "phase", "phase": "review", "duration_ms": 2000.0},
        {"event": "gate", "name": "ratchet", "result": "pass"},
    ]
    summary = factory_log.phase_summary(records)
    assert summary["slowest"] == "review"
    assert summary["total_ms"] == 3500.0
    consults = next(p for p in summary["phases"] if p["phase"] == "consults")
    assert consults["runs"] == 2 and consults["mean_ms"] == 750.0


def test_phase_summary_counts_errors_separately():
    records = [
        {"event": "phase", "phase": "consults", "duration_ms": 40.0, "outcome": "error"},
        {"event": "phase", "phase": "consults", "duration_ms": 900.0, "outcome": "ok"},
    ]
    summary = factory_log.phase_summary(records)
    assert summary["phases"][0]["errors"] == 1
    assert summary["phases"][0]["runs"] == 2


def test_phase_summary_is_empty_rather_than_wrong_with_no_phases():
    summary = factory_log.phase_summary([{"event": "gate", "name": "x"}])
    assert summary["phases"] == []
    assert summary["slowest"] is None
    assert summary["total_ms"] == 0.0


def test_aggregate_surfaces_phases(tmp_path):
    """The rollup existed and `aggregate` never called it, so the numbers were
    computable and never computed."""
    log = tmp_path / "f.jsonl"
    _run("phase", "--name", "step7_review_quorum", "--duration-ms", "2000", log=log)
    _run("phase", "--name", "step4a_consults", "--duration-ms", "500", log=log)
    result = _run("aggregate", "--format", "json", log=log)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "phases" in payload, "aggregate does not surface phase latency"
    assert payload["phases"]["slowest"] == "step7_review_quorum"


# --- the skill actually calls it ----------------------------------------------

def test_implement_times_the_phases_375_measured_by_hand():
    """#375's whole point: the phase costs in that ticket were reconstructed by
    hand after the fact. If /implement stops calling this, they would be again."""
    text = (Path(__file__).resolve().parent.parent
            / ".claude" / "commands" / "implement.md").read_text()
    assert "factory_log.py\" now" in text, "no phase start stamp in /implement"
    # Match the exact `--name <phase>` token, not a bare substring: renaming
    # step7_review_quorum to step7_review_quorum_DISABLED still CONTAINS the
    # original, so a substring check reports a disabled phase as timed.
    for phase in ("step4_consult_and_synthesis", "step5_6_tdd_and_implement",
                  "step7_review_quorum", "step8_verification"):
        assert f"--name {phase} " in text or f"--name {phase}\n" in text, (
            f"/implement does not time {phase}")


def test_the_skill_never_lets_a_measurement_fail_the_loop():
    """`|| true`: telemetry is a no-op unless CW_TELEMETRY=1, and an
    instrument must never break the thing it measures."""
    text = (Path(__file__).resolve().parent.parent
            / ".claude" / "commands" / "implement.md").read_text()
    # The emitted command quotes the script path, so the literal is
    # `factory_log.py" phase` — matching on `factory_log.py phase` finds
    # nothing and the guard passes vacuously.
    blocks = [b for b in text.split("```") if 'factory_log.py" phase' in b]
    assert blocks, "no phase-record block found in /implement"
    for block in blocks:
        assert "|| true" in block, (
            "a phase measurement without `|| true` can fail the loop it measures")

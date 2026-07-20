"""Tests for the report-only AI-slop gate (scripts/quality_slop_gate.py).

Covers the DETERMINISTIC parts against synthesized engine outputs: band
classification, report formatting, findings extraction, and report-only exit 0.
Graceful-skip is asserted when the underlying tools/history are absent. The
tool-dependent live path is exercised only when git-of-theseus is importable
(built from a tiny synthetic git repo) and is gated on availability.
"""

from __future__ import annotations

import argparse
import json as _json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import check_gate_validation as _gv
import pytest
import quality_slop_gate as gate

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# --- band classification ----------------------------------------------------


def test_survival_band_beats_pre_ai():
    r = gate.evaluate_survival({
        "survival_by_age_days": {
            14: {"survival_pct": 98.0, "lines_old_enough": 500},
            30: {"survival_pct": 97.0, "lines_old_enough": 400},
        }
    })
    assert r["status"] == "measured"
    assert r["survival_14d"] == 98.0
    assert r["survival_30d"] == 97.0
    assert r["band_14d"] == "better-than-pre-ai"


def test_survival_band_between():
    r = gate.evaluate_survival({
        "survival_by_age_days": {
            14: {"survival_pct": 95.0, "lines_old_enough": 500},
            30: {"survival_pct": 94.0, "lines_old_enough": 400},
        }
    })
    assert r["band_14d"] == "pre-ai..ai"


def test_survival_band_past_ai_is_a_finding():
    r = gate.evaluate_survival({
        "survival_by_age_days": {
            14: {"survival_pct": 90.0, "lines_old_enough": 500},
            30: {"survival_pct": 88.0, "lines_old_enough": 400},
        }
    })
    assert r["band_14d"] == "past-ai"


def test_survival_too_young_when_no_lines_old_enough():
    """git-of-theseus ran but no commit lived >= 14 days -> too_young, not a crash."""
    r = gate.evaluate_survival({
        "survival_by_age_days": {
            14: {"survival_pct": None, "lines_old_enough": 0},
            30: {"survival_pct": None, "lines_old_enough": 0},
        }
    })
    assert r["status"] == "too_young"
    assert "14 days" in r["detail"]


def test_survival_skipped_passthrough():
    r = gate.evaluate_survival({"skipped": "git-of-theseus not found"})
    assert r["status"] == "skipped"
    assert "git-of-theseus" in r["detail"]


def test_survival_handles_string_keys():
    """git-of-theseus JSON round-trips can key ages as strings."""
    r = gate.evaluate_survival({
        "survival_by_age_days": {
            "14": {"survival_pct": 95.0, "lines_old_enough": 500},
            "30": {"survival_pct": 94.0, "lines_old_enough": 400},
        }
    })
    assert r["status"] == "measured"
    assert r["survival_14d"] == 95.0


def test_duplication_band_beats_pre_ai():
    r = gate.evaluate_duplication({"duplication_pct_lines": 4.0})
    assert r["status"] == "measured"
    assert r["band"] == "better-than-pre-ai"


def test_duplication_band_between():
    r = gate.evaluate_duplication({"duplication_pct_lines": 10.0})
    assert r["band"] == "pre-ai..ai"


def test_duplication_band_past_ai_is_a_finding():
    r = gate.evaluate_duplication({"duplication_pct_lines": 20.0})
    assert r["band"] == "past-ai"


def test_duplication_skipped_passthrough():
    r = gate.evaluate_duplication({"skipped": "jscpd/node not found"})
    assert r["status"] == "skipped"
    assert "jscpd" in r["detail"] or "node" in r["detail"]


# --- report formatting ------------------------------------------------------


def test_format_report_measured_shows_bands_and_caveat():
    sv = gate.evaluate_survival({
        "survival_by_age_days": {
            14: {"survival_pct": 98.0, "lines_old_enough": 500},
            30: {"survival_pct": 97.0, "lines_old_enough": 400},
        }
    })
    dup = gate.evaluate_duplication({"duplication_pct_lines": 5.0})
    out = gate.format_report(sv, dup)
    assert "report-only" in out
    assert "[VENDOR]" in out          # caveat labelling
    assert "DORA 2024" in out
    assert "96.9" in out and "94.3" in out   # survival bands
    assert "8.3" in out and "12.3" in out    # duplication bands
    assert "98.0%" in out
    assert "5.0%" in out
    assert "beats the pre-AI human baseline" in out


def test_format_report_surfaces_skips_honestly():
    sv = gate.evaluate_survival({"skipped": "git-of-theseus not found"})
    dup = gate.evaluate_duplication({"skipped": "jscpd/node not found"})
    out = gate.format_report(sv, dup)
    assert "skipped: git-of-theseus not found" in out
    assert "skipped: jscpd/node not found" in out


def test_format_report_surfaces_too_young():
    sv = gate.evaluate_survival({
        "survival_by_age_days": {14: {"survival_pct": None, "lines_old_enough": 0}}
    })
    dup = gate.evaluate_duplication({"duplication_pct_lines": 5.0})
    out = gate.format_report(sv, dup)
    assert "too young" in out


# --- findings extraction ----------------------------------------------------


def test_no_findings_when_healthy():
    sv = gate.evaluate_survival({
        "survival_by_age_days": {14: {"survival_pct": 98.0, "lines_old_enough": 500}}
    })
    dup = gate.evaluate_duplication({"duplication_pct_lines": 4.0})
    assert gate.has_findings(sv, dup) == []


def test_findings_when_both_past_ai():
    sv = gate.evaluate_survival({
        "survival_by_age_days": {14: {"survival_pct": 90.0, "lines_old_enough": 500}}
    })
    dup = gate.evaluate_duplication({"duplication_pct_lines": 20.0})
    findings = gate.has_findings(sv, dup)
    assert len(findings) == 2
    assert any("survival" in f for f in findings)
    assert any("duplication" in f for f in findings)


def test_no_findings_when_skipped():
    sv = gate.evaluate_survival({"skipped": "git-of-theseus not found"})
    dup = gate.evaluate_duplication({"skipped": "jscpd/node not found"})
    assert gate.has_findings(sv, dup) == []


# --- run() exit-code contract (report-only vs --gate) -----------------------


def _args(**kw):
    base = dict(owner_repo=None, repo=None, workdir=None, report=True, gate=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_engines(monkeypatch, sv_result, dup_result):
    monkeypatch.setattr(gate.survival, "analyze", lambda *a, **k: sv_result)
    monkeypatch.setattr(gate.duplication, "analyze", lambda *a, **k: dup_result)


def test_run_report_only_exit_0_even_with_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "resolve_target", lambda o, r: str(tmp_path))
    _patch_engines(
        monkeypatch,
        {"survival_by_age_days": {14: {"survival_pct": 90.0, "lines_old_enough": 500}}},
        {"duplication_pct_lines": 20.0},
    )
    rc = gate.run(_args(workdir=str(tmp_path / "wd")))
    assert rc == 0  # report-only NEVER blocks
    out = capsys.readouterr().out
    assert "report-only" in out


def test_run_gate_blocks_only_on_past_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "resolve_target", lambda o, r: str(tmp_path))
    _patch_engines(
        monkeypatch,
        {"survival_by_age_days": {14: {"survival_pct": 90.0, "lines_old_enough": 500}}},
        {"duplication_pct_lines": 20.0},
    )
    rc = gate.run(_args(workdir=str(tmp_path / "wd"), report=False, gate=True))
    assert rc == 1


def test_run_gate_passes_when_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "resolve_target", lambda o, r: str(tmp_path))
    _patch_engines(
        monkeypatch,
        {"survival_by_age_days": {14: {"survival_pct": 98.0, "lines_old_enough": 500}}},
        {"duplication_pct_lines": 4.0},
    )
    rc = gate.run(_args(workdir=str(tmp_path / "wd"), report=False, gate=True))
    assert rc == 0


def test_run_gate_does_not_block_on_skipped_tools(tmp_path, monkeypatch):
    """A missing tool must never turn --gate into a false block."""
    monkeypatch.setattr(gate, "resolve_target", lambda o, r: str(tmp_path))
    _patch_engines(
        monkeypatch,
        {"skipped": "git-of-theseus not found"},
        {"skipped": "jscpd/node not found"},
    )
    rc = gate.run(_args(workdir=str(tmp_path / "wd"), report=False, gate=True))
    assert rc == 0


# --- live tool-gated path (deterministic-ish, availability-gated) -----------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(
    shutil.which("git-of-theseus-analyze") is None,
    reason="git-of-theseus not installed on PATH",
)
def test_survival_live_young_repo_self_skips(tmp_path):
    """A brand-new repo has < 14 days of history -> the gate reports too_young."""
    repo = tmp_path / "young"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    env = {**os.environ}
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True, text=True, env=env)
    raw = gate.survival.analyze(str(repo), workdir=str(tmp_path / "s"))
    sv = gate.evaluate_survival(raw)
    # young repo: either the analyzer produced no 14-day-old lines (too_young)
    # or the tool skipped — both are graceful, neither crashes.
    assert sv["status"] in {"too_young", "skipped"}


# ---- --scanner-version (#184) ----------------------------------------------


def test_scanner_version_is_deterministic_and_stable_across_calls():
    assert gate._scanner_version() == gate._scanner_version()


def test_cli_scanner_version_prints_hex_digest():
    # gate.main() has no injectable argv (reads sys.argv directly), so the CLI
    # contract — no other arguments needed, exit 0, no side effects — is
    # exercised via subprocess like ratchet.py's.
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "quality_slop_gate.py"), "--scanner-version"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert len(out) == 64  # sha256 hex digest
    int(out, 16)  # valid hex


# ---- gate-validation record trials (#184, docs/gate-validation.md) ----------
#
# Re-executes every seeded-defect trial the shipped quality_slop_gate.json record
# claims, calling the REAL pure verdict functions (evaluate_survival /
# evaluate_duplication / has_findings) with the fixture band files as input — the
# only reproducible-in-CI path (git-of-theseus / jscpd are not installed).

_GV_ROOT = Path(__file__).resolve().parent.parent
_GV_CORPUS = _GV_ROOT / "tests" / "fixtures" / "gate_validation" / "quality_slop_gate_clean"
_GV_RECORD = _GV_ROOT / "docs" / "quality" / "validation" / "quality_slop_gate.json"
_GV_VALIDATION_DIR = _GV_ROOT / "docs" / "quality" / "validation"
_GV_EXPECTED_TO_RESULT = {"fire": "fired", "no-fire": "not-fired"}

# seed_id -> (band file, int_keys) — int_keys re-casts the serialized string age
# keys to the engine-native integer keys the survival engine actually emits.
_GV_TRIALS = {
    "slop-direct-01": ("survival_past_ai.json", True),
    "slop-direct-02": ("duplication_past_ai.json", False),
    "slop-config-indirection-01": ("survival_past_ai.json", False),
    "slop-omission-01": ("too_young.json", False),
    "slop-sampling-gap-01": ("both_skipped.json", False),
}


def _gv_outcome(band_file: str, int_keys: bool = False) -> str:
    data = _json.loads((_GV_CORPUS / band_file).read_text())
    sv_result = data["survival_result"]
    if int_keys and "survival_by_age_days" in sv_result:
        sv_result = {
            **sv_result,
            "survival_by_age_days": {int(k): v for k, v in sv_result["survival_by_age_days"].items()},
        }
    sv = gate.evaluate_survival(sv_result)
    dup = gate.evaluate_duplication(data["duplication_result"])
    return "fired" if gate.has_findings(sv, dup) else "not-fired"


def _gv_record() -> dict:
    return _json.loads(_GV_RECORD.read_text())


def test_slop_gate_record_trials_backed_by_live_functions():
    record = _gv_record()
    assert record["gate"] == "quality_slop_gate"
    assert record["scanner_version"] == gate._scanner_version(), "record scanner_version is stale"
    digest = _gv.corpus_digest(_GV_CORPUS)
    trials = record["seeded_defect_trials"]
    assert {t["seed_id"] for t in trials} == set(_GV_TRIALS)
    for t in trials:
        assert t["sha"] == digest, f"{t['seed_id']} pins a stale corpus digest"
        band_file, int_keys = _GV_TRIALS[t["seed_id"]]
        result = _gv_outcome(band_file, int_keys)
        assert result == t["result"], (t["seed_id"], result, t["result"])
        assert t["passed"] == (result == _GV_EXPECTED_TO_RESULT[t["expected"]])


def test_slop_gate_dual_key_survival_classifies_identically():
    # The engine emits int keys; a serialized band file carries string keys. The
    # config-indirection trial rests on both classifying identically.
    data = _json.loads((_GV_CORPUS / "survival_past_ai.json").read_text())
    str_keyed = gate.evaluate_survival(data["survival_result"])
    int_keyed = gate.evaluate_survival({
        **data["survival_result"],
        "survival_by_age_days": {int(k): v for k, v in data["survival_result"]["survival_by_age_days"].items()},
    })
    assert str_keyed == int_keyed
    assert str_keyed["band_14d"] == "past-ai"


def test_slop_gate_clean_corpus_backed_by_live_functions():
    record = _gv_record()
    run = record["clean_corpus_runs"][0]
    assert run["sha"] == _gv.corpus_digest(_GV_CORPUS)
    data = _json.loads((_GV_CORPUS / "clean.json").read_text())
    sv = gate.evaluate_survival(data["survival_result"])
    dup = gate.evaluate_duplication(data["duplication_result"])
    findings = gate.has_findings(sv, dup)
    assert findings == []
    coverage = {
        "signals_evaluated": 2,
        "measured_signals": sum(1 for v in (sv, dup) if v.get("status") == "measured"),
        "bands_classified": sum(1 for v in (sv, dup) if v.get("band_14d") or v.get("band")),
    }
    assert run["findings"] == len(findings) == 0
    assert run["coverage"] == coverage


def test_slop_gate_record_passes_gate_of_gates():
    rep = _gv.check("quality_slop_gate", _GV_VALIDATION_DIR)
    assert rep.record_found and rep.passing, rep.to_dict()

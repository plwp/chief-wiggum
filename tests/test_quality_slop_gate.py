"""Tests for the report-only AI-slop gate (scripts/quality_slop_gate.py).

Covers the DETERMINISTIC parts against synthesized engine outputs: band
classification, report formatting, findings extraction, and report-only exit 0.
Graceful-skip is asserted when the underlying tools/history are absent. The
tool-dependent live path is exercised only when git-of-theseus is importable
(built from a tiny synthetic git repo) and is gated on availability.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess

import pytest
import quality_slop_gate as gate

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

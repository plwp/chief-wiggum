"""Tests for scripts/quality/hotspots.py + scripts/hotspot_discovery.py (#187).

Covers the two contracts the epic calls out explicitly:
  - CTR-fh-030/INV-fh-001: hotspots.py REUSES churn.analyze / complexity.lizard_ccn /
    process.compute_coupling — it must not reimplement git-log parsing or coupling.
  - CTR-fh-031/032: window_days is derived (never wall-clock) and generation is
    deterministic — same (git_sha, window_days, normalization) => byte-identical
    hotspots array, ties broken (score desc, file asc).

The synthetic-outlier trial and determinism check (both required by the issue and
IT-fh-08) need real lizard-derived complexity numbers; they're skipped (not failed)
when lizard isn't on PATH — the same discipline tests/test_quality_metrics.py uses
for every other lizard-dependent numeric assertion in this repo (CI has no lizard;
`--venv`/local installs do).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from quality import complexity, hotspots, process


def _day(n: int) -> str:
    """2026-01-01 + n days, as an ISO date (rolls over months correctly)."""
    return (date(2026, 1, 1) + timedelta(days=n)).isoformat()

SCRIPT = Path(__file__).parent.parent / "scripts" / "hotspot_discovery.py"

HAS_LIZARD = shutil.which("lizard") is not None
requires_lizard = pytest.mark.skipif(not HAS_LIZARD, reason="lizard not installed on PATH")


# --- synthetic repo fixture --------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit(repo, subject, files: dict, date: str):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", subject, "--date", date],
        check=True, capture_output=True, text=True,
        env={
            "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com",
            "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com",
            "GIT_COMMITTER_DATE": date,
            "PATH": os.environ.get("PATH", ""),
        },
    )


def _complex_body(rev: int) -> str:
    branches = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(40))
    return f"def outlier(x):\n{branches}    return -1\n\n\n# rev {rev}\n"


@pytest.fixture()
def synth_repo(tmp_path):
    """8 commits over 35 days:
    - pair_a.py / pair_b.py co-change in the first 4 (day 0/5/10/15) ->
      change-coupling with co_changes=4, confidence=1.0.
    - outlier.py: a large-CCN function, added+grown in the last 4 commits
      (day 20/25/30/35) -> the clear churn x complexity outlier.
    - quiet.py: added once, never touched again -> low churn, low complexity.
    """
    repo = tmp_path / "synth"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")

    for i, day in enumerate((0, 5, 10, 15)):
        _commit(repo, f"feat(pair): iterate {i}", {
            "pair_a.py": f"def a():\n    return {i}\n",
            "pair_b.py": f"def b():\n    return {i}\n",
        }, date=f"{_day(day)}T12:00:00")

    _commit(repo, "feat: add quiet module", {"quiet.py": "def g():\n    return 1\n"},
            date=f"{_day(16)}T12:00:00")

    for i, day in enumerate((20, 25, 30, 35)):
        _commit(repo, f"feat(outlier): grow {i}", {"outlier.py": _complex_body(i)},
                date=f"{_day(day)}T12:00:00")

    return repo


# --- CTR-fh-030: reuse, never reimplement ------------------------------------


def test_compute_coupling_is_the_only_coupling_computation_hotspots_uses(synth_repo, monkeypatch):
    """hotspots.discover must call process.compute_coupling — not roll its own
    git-log/co-change parser (INV-fh-001).

    @cw-trace verifies CTR-fh-030 INV-fh-001
    """
    calls = []
    original = process.compute_coupling

    def spy(repo, min_co=4):
        calls.append((repo, min_co))
        return original(repo, min_co=min_co)

    monkeypatch.setattr(hotspots.process_mod, "compute_coupling", spy)
    hotspots.discover(str(synth_repo), venv=None)
    assert calls, "hotspots.discover never called process.compute_coupling"
    assert calls[0][0] == str(synth_repo)


def test_coupled_with_reflects_process_compute_coupling(synth_repo):
    """pair_a.py <-> pair_b.py co-change 4 times (>= DEFAULT_MIN_CO): the exact
    same pair process.compute_coupling reports must show up composed into
    whichever hotspot record(s) reference either file."""
    expected_pairs = {(p["a"], p["b"]) for p in process.compute_coupling(str(synth_repo))}
    assert ("pair_a.py", "pair_b.py") in expected_pairs

    result = hotspots.discover(str(synth_repo), venv=None, trend=False)
    by_file = {h["file"]: h for h in result["hotspots"]}
    if "pair_a.py" in by_file:
        partners = {c["file"] for c in by_file["pair_a.py"]["coupled_with"]}
        assert "pair_b.py" in partners


# --- CTR-fh-031: window_days derived from commit dates, never wall clock ----


def test_window_days_derived_from_commit_span_not_wallclock(synth_repo):
    """@cw-trace verifies CTR-fh-031"""
    result = hotspots.discover(str(synth_repo), venv=None, trend=False)
    # first commit day 0, last commit day 35 (2026-01-01 .. 2026-01-36 == 02-05)
    assert result["window_days"] == 35


def test_check_mode_window_days_matches_regenerate(synth_repo):
    """@cw-trace verifies CTR-fh-031"""
    regen = hotspots.window_days_at(str(synth_repo))
    assert regen == 35


# --- CTR-fh-032: determinism -------------------------------------------------


@requires_lizard
def test_hotspots_array_is_byte_identical_across_two_runs_at_same_sha(synth_repo):
    """@cw-trace verifies CTR-fh-032 CTR-fh-033 INV-fh-007"""
    r1 = hotspots.discover(str(synth_repo))
    r2 = hotspots.discover(str(synth_repo))
    assert r1["git_sha"] == r2["git_sha"]
    assert json.dumps(r1["hotspots"], sort_keys=True) == json.dumps(r2["hotspots"], sort_keys=True)
    # no stable-ID-shaped field anywhere in the record (INV-fh-007).
    blob = json.dumps(r1)
    import re

    assert not re.search(r"\b(BR|CTR|INV|ARC|EDG|SLO|BUD|ASM|PRC|MIG)-[A-Za-z0-9-]+-\d{3}\b", blob)


@requires_lizard
def test_hotspots_tie_break_is_score_desc_then_file_asc(synth_repo):
    """@cw-trace verifies CTR-fh-032"""
    result = hotspots.discover(str(synth_repo))
    scores_and_files = [(h["score"], h["file"]) for h in result["hotspots"]]
    expected = sorted(scores_and_files, key=lambda sf: (-sf[0], sf[1]))
    assert scores_and_files == expected


# --- the seeded trial: a known churn x complexity outlier ranks #1 ----------


@requires_lizard
def test_synthetic_churn_x_complexity_outlier_ranks_first(synth_repo):
    """@cw-trace verifies CTR-fh-030 CTR-fh-032"""
    result = hotspots.discover(str(synth_repo))
    assert result["hotspots"], "expected at least one rankable file"
    top = result["hotspots"][0]
    assert top["file"] == "outlier.py"
    assert top["decile"] == 10
    assert top["score"] == 1.0  # both norm_churn and norm_complexity are 1.0 (the max)


# --- graceful degradation without lizard ------------------------------------


def test_hotspots_degrades_gracefully_without_lizard(synth_repo, monkeypatch):
    """@cw-trace verifies CTR-fh-030"""
    monkeypatch.setattr(complexity.shutil, "which", lambda _n: None)
    monkeypatch.setattr(complexity.os.path, "exists", lambda _p: False)
    result = hotspots.discover(str(synth_repo))
    assert result["hotspots"] == []
    assert "lizard" in result.get("note", "")
    # still a well-formed, schema-tagged record — degradation doesn't crash.
    assert result["schema"] == "hotspots/1"
    assert result["git_sha"]


def test_hotspots_empty_repo_never_raises(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    result = hotspots.discover(str(repo))
    assert result["hotspots"] == []
    assert result["window_days"] == 0


# --- CLI: generate + --check (never gates; nonzero only on staleness) ------


@requires_lizard
def test_cli_generate_then_check_passes_at_same_sha(synth_repo):
    out = synth_repo / "docs" / "quality" / "hotspots.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()

    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stderr


@requires_lizard
def test_cli_check_fails_after_head_advances(synth_repo):
    """@cw-trace verifies CTR-fh-031"""
    out = synth_repo / "docs" / "quality" / "hotspots.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    _commit(synth_repo, "chore: advance head", {"new_file.py": "x = 1\n"},
            date="2026-02-10T12:00:00")

    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert check.returncode == 1
    assert "Stale" in check.stderr

    # generate mode itself never gates — always exit 0, even though the
    # artifact was stale a moment ago.
    regen = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert regen.returncode == 0, regen.stderr


def test_cli_check_missing_file_exits_1(tmp_path):
    """@cw-trace verifies CTR-fh-031"""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo),
         "--out", str(repo / "docs" / "quality" / "hotspots.json"), "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 1


def test_no_stable_id_field_in_generated_record(synth_repo):
    """@cw-trace verifies CTR-fh-033 INV-fh-007"""
    result = hotspots.discover(str(synth_repo), venv=None)
    import re

    blob = json.dumps(result)
    assert not re.search(r"\b(BR|CTR|INV|ARC|EDG|SLO|BUD|ASM|PRC|MIG)-[A-Za-z0-9-]+-\d{3}\b", blob)

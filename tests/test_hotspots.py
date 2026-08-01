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


# --- PR #194 review fixes ----------------------------------------------------


def test_check_fails_when_record_was_generated_from_dirty_worktree(synth_repo):
    """Generate on a dirty worktree -> record carries dirty=true -> --check
    fails even after the tree is restored to clean (the recorded inputs were
    unreproducible at that sha).

    @cw-trace verifies CTR-fh-031
    """
    out = synth_repo / "docs" / "quality" / "hotspots.json"
    tracked = synth_repo / "pair_a.py"
    original = tracked.read_text()
    tracked.write_text(original + "# uncommitted local edit\n")

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr  # generate never gates
    assert json.loads(out.read_text())["dirty"] is True

    tracked.write_text(original)  # restore: tree clean again (artifact itself is ignored)
    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert check.returncode == 1
    assert "dirty" in check.stderr.lower()


def test_check_fails_when_worktree_is_currently_dirty(synth_repo):
    """Generate clean -> dirty the tree -> --check fails on the CURRENT dirty
    state, even though the recorded state was clean and the sha matches.

    @cw-trace verifies CTR-fh-031
    """
    out = synth_repo / "docs" / "quality" / "hotspots.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["dirty"] is False

    tracked = synth_repo / "pair_a.py"
    tracked.write_text(tracked.read_text() + "# uncommitted local edit\n")
    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(synth_repo), "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert check.returncode == 1
    assert "dirty" in check.stderr.lower()


def test_check_fails_when_complexity_tool_state_changes(synth_repo, monkeypatch):
    """A record built with one lizard state must not --check clean under a
    different one — the same sha would now produce a different record. Covers
    both directions (absent -> present and present -> absent/other version).

    @cw-trace verifies CTR-fh-031
    """
    import hotspot_discovery

    out = synth_repo / "docs" / "quality" / "hotspots.json"
    result = hotspots.discover(str(synth_repo))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result))

    # Same tool state -> passes.
    assert hotspot_discovery.run_check(str(synth_repo), str(out)) == 0

    # Simulate the tool state changing (e.g. record built without lizard,
    # lizard later installed — or the version changing).
    recorded = result["complexity_source"]
    flipped = "lizard-9.9.9-simulated" if recorded == "absent" else "absent"
    monkeypatch.setattr(hotspots, "complexity_source", lambda venv=None, gobin=None: flipped)
    assert hotspot_discovery.run_check(str(synth_repo), str(out)) == 1


def test_record_carries_dirty_and_complexity_source_fields(synth_repo):
    result = hotspots.discover(str(synth_repo))
    assert result["dirty"] is False
    src = result["complexity_source"]
    assert src == "absent" or src.startswith("lizard-")
    # a lizard-less record must say so honestly
    if not HAS_LIZARD:
        assert src == "absent"


@requires_lizard
def test_lizard_paths_normalized_to_git_style_for_nested_files(tmp_path):
    """Per-file complexity keys must be git-style (forward-slash, repo-relative)
    so the churn-intersection and code_query's exact-membership check hold on
    every platform — regression for a nested path.

    @cw-trace verifies CTR-fh-034
    """
    repo = tmp_path / "nested"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    body = "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(5)) + "    return -1\n"
    for i in range(2):
        _commit(repo, f"feat: rev {i}", {"pkg/sub/mod.py": body + f"# rev {i}\n"},
                date=f"{_day(i)}T12:00:00")
    result = hotspots.discover(str(repo))
    files = {h["file"] for h in result["hotspots"]}
    assert "pkg/sub/mod.py" in files
    assert not any("\\" in f for f in files)


def test_rank_and_decile_use_raw_scores_not_rounded():
    """Two raw scores that round to the same 6-dp value must still rank by the
    RAW value — rounding-induced ties must not let filename order decide
    (which could shift deciles).

    @cw-trace verifies CTR-fh-032
    """
    lo = {"file": "a.py", "score": 0.12345601, "norm_churn": 0.5, "norm_complexity": 0.5,
          "churn": 1, "commits": 1, "complexity": 1, "coupled_with": [], "trend": None}
    hi = {"file": "z.py", "score": 0.12345649, "norm_churn": 0.5, "norm_complexity": 0.5,
          "churn": 1, "commits": 1, "complexity": 1, "coupled_with": [], "trend": None}
    ranked = hotspots._rank_decile_round([lo, hi])
    # Filename order alone would put a.py first; the raw score puts z.py first.
    assert [h["file"] for h in ranked] == ["z.py", "a.py"]
    # Both serialize to the same rounded score — the tie is display-only.
    assert ranked[0]["score"] == ranked[1]["score"] == 0.123456


def test_history_walk_is_head_based_never_all(synth_repo):
    """An unrelated local ref pointing at extra commits must not change the
    output for the same HEAD — the walk starts at HEAD, never --all
    (the former --include-merges flag mapped to --all and was dropped).

    @cw-trace verifies CTR-fh-032
    """
    before = hotspots.discover(str(synth_repo), venv=None, trend=False)

    # Park extra history on a side branch; HEAD returns to where it was.
    _git(synth_repo, "checkout", "-q", "-b", "side")
    _commit(synth_repo, "feat: side-branch only", {"side_only.py": "def s():\n    return 1\n"},
            date=f"{_day(40)}T12:00:00")
    _git(synth_repo, "checkout", "-q", "-")

    after = hotspots.discover(str(synth_repo), venv=None, trend=False)
    assert before["git_sha"] == after["git_sha"]
    assert before["window_days"] == after["window_days"]
    assert json.dumps(before["hotspots"], sort_keys=True) == json.dumps(after["hotspots"], sort_keys=True)

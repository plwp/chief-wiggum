"""Tests for the code-quality metric engines (/code-metrics).

Covers the DETERMINISTIC pure-Python paths — churn parsing/attribution and
process metrics (coupling/entropy/ownership) — against a tiny synthetic git repo
built in a tmp dir. External-tool wrappers are only checked for graceful skip
when the tool is absent; their numeric output is validated in the smoke run, not
here (they depend on lizard/git-of-theseus/jscpd being installed).
"""

from __future__ import annotations

import os
import subprocess

import pytest
from quality import churn, complexity, duplication, process, report, survival, trend

# --- synthetic repo fixture -------------------------------------------------


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _commit(repo, subject, files: dict, author="Ada <ada@example.com>"):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    name, email = author.split(" <")
    email = email.rstrip(">")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", subject,
         "--author", author, "--date", "2026-01-01T12:00:00"],
        check=True, capture_output=True, text=True,
        env={
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": "2026-01-01T12:00:00",
            "PATH": os.environ.get("PATH", ""),
        },
    )


@pytest.fixture()
def synth_repo(tmp_path):
    """A tiny repo: 5 commits, two authors, coupled files, a fix, a big commit."""
    repo = tmp_path / "synth"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")

    # a.py and b.py always change together -> change coupling
    _commit(repo, "feat(core): add a and b", {
        "a.py": "def a():\n    return 1\n",
        "b.py": "def b():\n    return 2\n",
    })
    _commit(repo, "feat(core): grow a and b (#12)", {
        "a.py": "def a():\n    return 1 + 1\n",
        "b.py": "def b():\n    return 2 + 2\n",
    }, author="Grace <grace@example.com>")
    _commit(repo, "fix(core): bug in a", {
        "a.py": "def a():\n    return 3\n",
    })
    _commit(repo, "refactor: touch a and b again", {
        "a.py": "def a():\n    return 4\n",
        "b.py": "def b():\n    return 5\n",
    })
    # a large commit (>400 changed lines)
    big = "\n".join(f"x{i} = {i}" for i in range(500)) + "\n"
    _commit(repo, "feat: big module", {"big.py": big})
    return repo


# --- churn ------------------------------------------------------------------


def test_churn_scale_and_attribution(synth_repo):
    r = churn.analyze(str(synth_repo), no_merges=True)
    assert r["scale"]["commits"] == 5
    attr = r["attribution"]
    # every commit uses a conventional prefix
    assert attr["conventional_pct"] == 100.0
    # exactly one commit has a #ref
    assert attr["ticket_ref_pct"] == 20.0
    assert set(attr["type_histogram"]) >= {"feat", "fix", "refactor"}
    # two distinct authors seen
    assert set(attr["author_histogram"]) == {"Ada", "Grace"}


def test_churn_hotspots_and_totals(synth_repo):
    r = churn.analyze(str(synth_repo), no_merges=True)
    files = {h["file"] for h in r["hotspots"]}
    assert {"a.py", "b.py", "big.py"} <= files
    # a.py touched in 4 commits, b.py in 3
    a = next(h for h in r["hotspots"] if h["file"] == "a.py")
    assert a["commits"] == 4
    assert r["churn"]["added"] > 0


def test_churn_empty_repo(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    r = churn.analyze(str(repo))
    assert r.get("error") == "no commits"


# --- process ----------------------------------------------------------------


def test_process_change_coupling(synth_repo):
    r = process.analyze(str(synth_repo))
    assert r["commits_analyzed"] == 5
    # a.py <-> b.py co-changed in 3 commits (>= threshold of 4? no -> not listed)
    # coupling requires co_changes >= 4; here they co-change 3 times, so empty.
    # Lower-bound the invariant instead: coupling entries are well-formed if present.
    for c in r["change_coupling_top"]:
        assert 0 < c["confidence"] <= 1
        assert c["co_changes"] >= 4


def test_process_entropy_and_ownership(synth_repo):
    r = process.analyze(str(synth_repo))
    # entropy is a normalized 0..1 value
    assert 0.0 <= r["change_entropy_normalized"] <= 1.0
    own = r["ownership"]
    assert own["distinct_authors"] == 2
    assert own["bus_factor_50pct"] >= 1
    assert 0 < own["top_author_share"] <= 1


def test_process_commit_size_and_fix_ratio(synth_repo):
    r = process.analyze(str(synth_repo))
    # the 500-line commit should register as a large commit
    assert r["commit_size"]["pct_large_commits_gt400"] > 0
    # one fix commit out of five
    assert r["defect_proxy"]["fix_commit_pct"] == 20.0


def test_process_coupling_threshold(tmp_path):
    """Files co-changing >=4 times should appear in coupling."""
    repo = tmp_path / "coupled"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    for i in range(5):
        _commit(repo, f"feat: iter {i}", {
            "x.py": f"v = {i}\n",
            "y.py": f"w = {i}\n",
        })
    r = process.analyze(str(repo))
    pairs = {(c["a"], c["b"]) for c in r["change_coupling_top"]}
    assert ("x.py", "y.py") in pairs


# --- report consolidation (pure) -------------------------------------------


def test_report_build_combined_is_pure(synth_repo):
    engines = {
        "churn": churn.analyze(str(synth_repo)),
        "complexity": {"skipped": "lizard not found"},
        "process": process.analyze(str(synth_repo)),
        "trend": {"skipped": "--skip-trend"},
        "survival": {"skipped": "--skip-survival"},
        "duplication": {"skipped": "--skip-duplication"},
    }
    combined = report.build_combined(engines)
    s = combined["summary"]
    assert s["repo"] == "synth"
    assert s["commits"] == 5
    assert s["rework_ratio"] >= 0
    md = report.render_markdown(engines, combined, charts=[])
    assert "Code-Quality Metrics" in md
    assert "lizard not found" in md  # skip note surfaced honestly


# --- graceful degradation of external-tool wrappers -------------------------


def test_survival_skips_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(survival.shutil, "which", lambda _n: None)
    r = survival.analyze(str(tmp_path), workdir=str(tmp_path / "s"))
    assert "skipped" in r
    assert "git-of-theseus" in r["skipped"]


def test_duplication_skips_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(duplication.shutil, "which", lambda _n: None)
    r = duplication.analyze(str(tmp_path), workdir=str(tmp_path / "d"))
    assert "skipped" in r
    assert "jscpd" in r["skipped"] or "node" in r["skipped"]


def test_complexity_skips_without_lizard(synth_repo, monkeypatch):
    monkeypatch.setattr(complexity.shutil, "which", lambda _n: None)
    # also block the sys.executable sibling lookup
    monkeypatch.setattr(complexity.os.path, "exists", lambda _p: False)
    r = complexity.analyze(str(synth_repo))
    assert r.get("skipped") == "lizard not found"


def test_trend_skips_without_lizard(synth_repo, monkeypatch):
    monkeypatch.setattr(trend, "_tool", lambda *a, **k: None)
    r = trend.analyze(str(synth_repo), workdir=str(synth_repo / "wt"), n=3)
    assert r.get("skipped") == "lizard not found"

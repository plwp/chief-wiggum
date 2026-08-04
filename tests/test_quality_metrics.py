"""Tests for the code-quality metric engines (/code-metrics).

Covers the DETERMINISTIC pure-Python paths — churn parsing/attribution and
process metrics (coupling/entropy/ownership) — against a tiny synthetic git repo
built in a tmp dir. External-tool wrappers are only checked for graceful skip
when the tool is absent; their numeric output is validated in the smoke run, not
here (they depend on lizard/git-of-theseus/jscpd being installed).
"""

from __future__ import annotations

import json
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


# --- #289: a crashed git-of-theseus-analyze must never be indistinguishable
# from "tool not installed", and a reused outdir must never let a crashed
# run's result be a STALE survival.json left by an earlier successful run. ---


def _fake_theseus_tool(tmp_path, *, returncode=0, survival_payload=None):
    """A fake ``git-of-theseus-analyze`` executable. When ``survival_payload``
    (a dict) is given, it is written to ``<outdir>/survival.json`` before the
    script exits with ``returncode`` — otherwise nothing is written, modeling
    a tool that dies before producing output."""
    script = tmp_path / "git-of-theseus-analyze"
    lines = [
        "#!/usr/bin/env python3",
        "import sys, os, json",
        "args = sys.argv[1:]",
        "outdir = args[args.index('--outdir') + 1]",
        "os.makedirs(outdir, exist_ok=True)",
    ]
    if survival_payload is not None:
        lines.append(
            "json.dump(" + repr(survival_payload)
            + ", open(os.path.join(outdir, 'survival.json'), 'w'))"
        )
    lines.append(f"sys.exit({returncode})")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return str(script)


def _patch_theseus_which(monkeypatch, tool_path):
    monkeypatch.setattr(
        survival.shutil, "which",
        lambda name: tool_path if name == "git-of-theseus-analyze" else None,
    )


def test_survival_nonzero_exit_is_crashed_not_silently_measured(tmp_path, monkeypatch):
    """survival.py never checked the subprocess returncode (#289) — a crashed
    git-of-theseus-analyze that happened to leave no survival.json used to
    fall through to the exact same shape as "tool not installed", masking a
    real defect as a declared limitation."""
    tool = _fake_theseus_tool(tmp_path, returncode=1)
    _patch_theseus_which(monkeypatch, tool)
    result = survival.analyze(str(tmp_path / "repo"), workdir=str(tmp_path / "wd"))
    assert result.get("status") == "crashed"
    assert "survival_by_age_days" not in result


def test_survival_success_exit_but_no_output_is_crashed(tmp_path, monkeypatch):
    """Exit 0 with no survival.json written is ALSO a crash, not a pass."""
    tool = _fake_theseus_tool(tmp_path, returncode=0)  # writes nothing
    _patch_theseus_which(monkeypatch, tool)
    result = survival.analyze(str(tmp_path / "repo"), workdir=str(tmp_path / "wd2"))
    assert result.get("status") == "crashed"


def test_survival_stale_survival_json_is_never_reused_after_a_crash(tmp_path, monkeypatch):
    """outdir may be reused across runs. A run that CRASHES must never let a
    survival.json left by an EARLIER successful run get parsed as this run's
    fresh output — the exact silent-success failure #289 exists to remove."""
    outdir = tmp_path / "wd"
    outdir.mkdir()
    stale = {"deadbeefcafe": [[1700000000, 100], [1700950000, 40]]}  # a fabricated prior run
    (outdir / "survival.json").write_text(json.dumps(stale))

    tool = _fake_theseus_tool(tmp_path, returncode=1)  # crashes; writes nothing
    _patch_theseus_which(monkeypatch, tool)

    result = survival.analyze(str(tmp_path / "repo"), workdir=str(outdir))
    assert result.get("status") == "crashed"
    assert "survival_by_age_days" not in result
    assert "deadbeefcafe" not in json.dumps(result)


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


# --- #328: a sampled commit's metrics are immutable — the second measurement
# of the SAME commit must perform zero checkouts (no `git worktree add`).
# lizard is faked out (CI has none of the battery's tools installed) so this
# exercises the caching path, not lizard itself.


def _spy_on_worktree_add(monkeypatch, module):
    calls: list[tuple] = []
    real_run = module.run

    def spy(*a, **kw):
        calls.append(a)
        return real_run(*a, **kw)

    monkeypatch.setattr(module, "run", spy)
    return calls


def test_trend_measure_at_second_call_same_commit_skips_checkout(synth_repo, monkeypatch):
    monkeypatch.setattr(trend, "_tool", lambda *a, **k: "/usr/bin/true")
    monkeypatch.setattr(trend, "lizard_ccn", lambda files, lizard_bin: [
        {"nloc": 3, "ccn": 1, "length": 3, "file": f} for f in files
    ])
    commit, _date = trend.sample_commits(str(synth_repo), 2)[0]
    calls = _spy_on_worktree_add(monkeypatch, trend)
    workdir = str(synth_repo / "wt")

    m1 = trend.measure_at(str(synth_repo), commit, "/usr/bin/true", workdir)
    adds_after_first = sum(1 for c in calls if "worktree" in c and "add" in c)
    assert adds_after_first >= 1

    m2 = trend.measure_at(str(synth_repo), commit, "/usr/bin/true", workdir)
    adds_after_second = sum(1 for c in calls if "worktree" in c and "add" in c)
    assert adds_after_second == adds_after_first, (
        "second measure_at for the SAME commit must perform zero checkouts"
    )
    assert m1 == m2


def test_trend_measure_at_no_cache_env_forces_a_real_checkout(synth_repo, monkeypatch):
    monkeypatch.setattr(trend, "_tool", lambda *a, **k: "/usr/bin/true")
    monkeypatch.setattr(trend, "lizard_ccn", lambda files, lizard_bin: [])
    commit, _date = trend.sample_commits(str(synth_repo), 2)[0]
    calls = _spy_on_worktree_add(monkeypatch, trend)
    workdir = str(synth_repo / "wt")

    trend.measure_at(str(synth_repo), commit, "/usr/bin/true", workdir)
    n1 = sum(1 for c in calls if "worktree" in c and "add" in c)
    monkeypatch.setenv("CW_QUALITY_NO_CACHE", "1")
    trend.measure_at(str(synth_repo), commit, "/usr/bin/true", workdir)
    n2 = sum(1 for c in calls if "worktree" in c and "add" in c)
    assert n2 > n1


# --- #328: git-of-theseus walks committed history only (never the working
# tree), so a second survival.analyze at the SAME HEAD must skip the tool
# entirely, and reflect the same head_sha-keyed cache the trend/jscpd engines
# use — but keyed on HEAD, not manifest content, per the module docstring.


def test_survival_second_run_same_head_reuses_cache(tmp_path, monkeypatch):
    payload = {"deadbeef": [[1700000000, 10], [1700950000, 8]]}
    tool = _fake_theseus_tool(tmp_path, returncode=0, survival_payload=payload)
    _patch_theseus_which(monkeypatch, tool)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "--initial-branch=main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

    calls: list[str] = []
    real_run = survival._run_git_of_theseus

    def spy(repo_arg, outdir):
        calls.append(outdir)
        return real_run(repo_arg, outdir)

    monkeypatch.setattr(survival, "_run_git_of_theseus", spy)

    r1 = survival.analyze(str(repo), workdir=str(tmp_path / "wd1"))
    assert len(calls) == 1
    r2 = survival.analyze(str(repo), workdir=str(tmp_path / "wd2"))
    assert len(calls) == 1, "second run at the SAME HEAD must be a cache hit"
    # r2 came back through the on-disk JSON cache, so int dict keys (the AGES
    # bins) round-trip as strings — the same ambiguity report.py already
    # tolerates (`by_age.get(14) or by_age.get("14")`). Compare via the same
    # normalization rather than raw dict equality.
    assert json.loads(json.dumps(r1)) == r2


def test_survival_no_cache_env_forces_a_second_real_run(tmp_path, monkeypatch):
    payload = {"deadbeef": [[1700000000, 10]]}
    tool = _fake_theseus_tool(tmp_path, returncode=0, survival_payload=payload)
    _patch_theseus_which(monkeypatch, tool)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "--initial-branch=main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

    calls: list[str] = []
    real_run = survival._run_git_of_theseus

    def spy(repo_arg, outdir):
        calls.append(outdir)
        return real_run(repo_arg, outdir)

    monkeypatch.setattr(survival, "_run_git_of_theseus", spy)
    survival.analyze(str(repo), workdir=str(tmp_path / "wd1"))
    monkeypatch.setenv("CW_QUALITY_NO_CACHE", "1")
    survival.analyze(str(repo), workdir=str(tmp_path / "wd2"))
    assert len(calls) == 2


# --- #328: complexity.tracked_files shells `git ls-files` on every call, with
# ~8 callers across bucket/population/hotspots/trend/dead_code/test_health/
# markers — a process-lifetime lru_cache means the SAME repo path only ever
# pays for one subprocess.


def test_tracked_files_is_cached_per_process(synth_repo, monkeypatch):
    complexity._tracked_files_at.cache_clear()
    calls = []
    real_run = complexity.run

    def spy(*a, **kw):
        calls.append(a)
        return real_run(*a, **kw)

    monkeypatch.setattr(complexity, "run", spy)
    first = complexity.tracked_files(str(synth_repo))
    second = complexity.tracked_files(str(synth_repo))
    assert first == second
    assert len(calls) == 1, "one git ls-files per repo per process"


def test_tracked_files_reflects_a_mutation_between_calls(synth_repo, monkeypatch):
    """The naive fix (cache keyed on bare repo path) is WRONG: a caller that
    rescans the SAME repo path after a real git mutation (debt_inventory's
    own rename-probe acceptance test does exactly this — build_inventory
    called twice across a commit) must see the new tracked set, not a
    process-lifetime-stale one."""
    complexity._tracked_files_at.cache_clear()
    before = complexity.tracked_files(str(synth_repo))
    assert "new_file.py" not in before
    (synth_repo / "new_file.py").write_text("z = 1\n")
    _git(synth_repo, "add", "-A")
    after = complexity.tracked_files(str(synth_repo))
    assert "new_file.py" in after

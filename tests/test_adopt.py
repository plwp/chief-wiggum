"""Tests for /adopt — brownfield entry: survey, elect, baseline, grandfather,
record (chief-wiggum#215).

Fixture idioms follow tests/test_sidecar_roundtrip.py: an isolated
``CHIEF_WIGGUM_USER_DIR``, a tmp target git repo with one passing pytest test,
and (for sidecar runs) an assertion that the target tree never gains a CW meta
file at any step — /adopt on a brownfield repo must never write the tree it is
adopting.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import adopt
import artifacts
import debt_inventory
import pytest
import quality_slop_gate
import ratchet
import status

# CW meta filenames that must never appear in a sidecar-adopted target tree
# (superset of test_sidecar_roundtrip's — /adopt also writes adoption records).
CW_META_NAMES = {
    "election.json", "scope.json", "ratchet.json", "ratchet-journal.jsonl",
    "ratchet-highwater.json", "ratchet-scorecard.json", "debt.json",
    "survey.json", "grandfathered.json", "adoption.json",
    "review-authorities.json",
}

APP_PY = """\
def add_order(orders, start_date, end_date):
    # TODO: validate timezone handling
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    orders.append((start_date, end_date))
    return orders
"""

TEST_APP_PY = """\
from app import add_order


def test_add_order():
    orders = add_order([], "2026-01-01", "2026-01-02")
    assert orders == [("2026-01-01", "2026-01-02")]
"""


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    """Isolated ~/.chief-wiggum — tests must never touch the real home dir."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_target(path: Path, *, with_tests: bool = True, with_pyproject: bool = True) -> Path:
    """A tmp brownfield target: source + (optionally) a passing pytest test,
    NO chief-wiggum artifacts anywhere — the /adopt starting state."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text(APP_PY)
    if with_tests:
        (path / "tests").mkdir()
        (path / "tests" / "test_app.py").write_text(TEST_APP_PY)
    if with_pyproject:
        (path / "pyproject.toml").write_text('[project]\nname = "t"\nversion = "0"\n')
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def assert_no_cw_meta(target: Path) -> None:
    assert not (target / "docs").exists()
    offenders = [p for p in target.rglob("*") if p.name in CW_META_NAMES]
    assert offenders == [], f"CW meta leaked into the adopted target: {offenders}"


def _tracked_clean(target: Path) -> bool:
    """STRICT byte-clean check (#215 F3): NO exclusions. The baseline/survey
    runs suppress bytecode + the pytest cache in the child env
    (PYTHONDONTWRITEBYTECODE / PYTEST_ADDOPTS), so __pycache__ and
    .pytest_cache must genuinely not appear — the mechanism is real, the test
    is not lenient."""
    out = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()] == []


# --- survey ----------------------------------------------------------------------


def test_survey_verdicts_and_persistence(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "sidecar", backing="local")
    rc = adopt.main(["survey", "--repo", str(target)])
    assert rc == 0
    resolver = artifacts.Resolver.resolve(target)
    doc = json.loads((resolver.meta_root / "adoption" / "survey.json").read_text())
    assert doc["schema"] == "adoption-survey/1"
    assert doc["target_sha"] == artifacts.head_sha(target)
    # shape facts
    assert doc["size"]["languages"] == {"python": 2}
    assert doc["tests"]["test_files"] == {"python": 1}
    assert doc["age"]["first_commit"]
    assert doc["ci"]["present"] is False
    # real coverage-baseline attempt: the tmp repo's one test passes
    run = doc["test_run"]
    assert run["detected"] is True
    step = next(s for s in run["steps"] if s["tool"] == "python")
    assert step["ok"] is True
    assert step["passed"] == 1
    assert step["failed"] == 0
    # per-gate verdicts
    g = doc["gates"]
    assert set(g) == set(adopt.SHIPPED_GATES) | {"debt_inventory"}
    assert g["saas_gate"]["verdict"] == "inapplicable"
    assert "base URL" in g["saas_gate"]["reason"]
    assert g["check_traceability"]["verdict"] == "report-only"
    assert g["check_single_writer"]["verdict"] == "inapplicable"
    assert g["ratchet"]["verdict"] == "applicable"
    assert g["debt_inventory"]["verdict"] == "applicable"
    assert g["quality_slop_gate"]["verdict"] == "applicable"
    assert g["ci_scaffold"]["verdict"] == "report-only"
    assert g["check_architecture"]["verdict"] == "inapplicable"
    for v in g.values():  # every verdict carries a one-line reason
        assert v["verdict"] in ("applicable", "report-only", "inapplicable")
        assert v["reason"]
    assert_no_cw_meta(target)


def test_survey_no_runner_says_so(user_dir, tmp_path):
    """No detected test runner: the survey SAYS so — it never fabricates a
    coverage baseline — and the ratchet verdict degrades to report-only."""
    target = make_target(tmp_path / "t", with_tests=False, with_pyproject=False)
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["survey", "--repo", str(target)]) == 0
    resolver = artifacts.Resolver.resolve(target)
    doc = json.loads((resolver.meta_root / "adoption" / "survey.json").read_text())
    assert doc["test_run"]["detected"] is False
    assert "no test runner detected" in doc["test_run"]["note"]
    assert doc["gates"]["ratchet"]["verdict"] == "report-only"


def test_survey_explicit_embedded_election_writes_target_docs(user_dir, tmp_path):
    """With an EXPLICIT embedded election, survey.json goes to
    <target>/docs/adoption (the documented embedded contract)."""
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "embedded")
    assert adopt.main(["survey", "--repo", str(target)]) == 0
    assert (target / "docs" / "adoption" / "survey.json").is_file()


@pytest.mark.parametrize("subcmd", ["survey", "baseline", "grandfather", "record"])
def test_standalone_subcommands_refuse_without_election(user_dir, tmp_path, capsys, subcmd):
    """F4 (#215): with NO election file, standalone subcommands refuse (exit 2)
    instead of silently defaulting to embedded and writing the target tree —
    embedded mode is an explicit choice."""
    target = make_target(tmp_path / "t")
    assert adopt.main([subcmd, "--repo", str(target)]) == 2
    err = capsys.readouterr().err
    assert "no footprint election" in err
    assert "embedded mode is an explicit choice" in err
    assert_no_cw_meta(target)


# --- elect -----------------------------------------------------------------------


def test_elect_defaults_sidecar_whole_repo(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    assert adopt.main(["elect", "--repo", str(target)]) == 0
    resolver = artifacts.Resolver.resolve(target)
    assert resolver.mode == "sidecar"
    assert not resolver.scope_path().is_file()  # whole-repo scope by default
    assert_no_cw_meta(target)


def test_elect_scope_from_codeowners(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    (target / ".github").mkdir()
    (target / ".github" / "CODEOWNERS").write_text(
        "# comment\n"
        "* @org/everyone\n"          # catch-all — must not narrow the scope
        "/src/ @org/team-a\n"
        "docs/ @org/team-b\n"
        "*.proto @org/team-c\n"
    )
    assert adopt.main(["elect", "--repo", str(target), "--scope-from-codeowners"]) == 0
    resolver = artifacts.Resolver.resolve(target)
    scope = json.loads(resolver.scope_path().read_text())
    assert scope["include"] == ["src/*", "docs/*", "*.proto"]
    assert "CODEOWNERS" in scope["$comment"]
    assert resolver.in_scope("src/a.py")
    assert resolver.in_scope("proto/x.proto")
    assert not resolver.in_scope("other/b.py")


def test_elect_codeowners_absent_skips_with_note(user_dir, tmp_path, capsys):
    target = make_target(tmp_path / "t")
    assert adopt.main(["elect", "--repo", str(target), "--scope-from-codeowners"]) == 0
    assert "no CODEOWNERS" in capsys.readouterr().out
    assert not artifacts.Resolver.resolve(target).scope_path().is_file()


# --- baseline --------------------------------------------------------------------


def test_baseline_real_test_run_and_journal(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["baseline", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    qd = resolver.quality_dir()
    sc = json.loads((qd / ratchet.SCORECARD_NAME).read_text())
    # REAL pass-set from a real suite run — never the --no-tests empty baseline
    assert sc["tests_run"] is True
    assert len(sc["pass_set"]) == 1
    assert "test_add_order" in sc["pass_set"][0]
    cfg = ratchet.load_config(Path(target))
    records = ratchet.load_journal(cfg)
    assert records[-1]["event"] == "baseline"
    assert records[-1]["merged"] is True
    assert records[-1]["notes"] == "adoption baseline"
    # debt inventory baseline written to the resolver quality dir
    debt = json.loads((qd / "debt.json").read_text())
    ids = [i["id"] for i in debt["items"]]
    assert any(i.startswith("DEBT-") for i in ids)  # the TODO marker
    # the target tree stays clean: no junit report, no docs/, nothing CW —
    # and genuinely byte-clean (F3): no bytecode, no pytest cache anywhere
    assert_no_cw_meta(target)
    assert not (target / ".ratchet-junit.xml").exists()
    assert not list(target.rglob("__pycache__"))
    assert not list(target.rglob(".pytest_cache"))
    assert _tracked_clean(target)
    # ratchet check holds against its own fresh baseline
    rc = ratchet.cmd_check(argparse.Namespace(
        repo=str(target), format="text", gate_verifier_tests=False, gate_quality=False))
    assert rc == 0


# --- _retarget_suite_reports: report/cmd mismatch must not silently no-op (#271) --


def test_retarget_raises_when_relative_report_absent_from_cmd(tmp_path):
    """A repo-relative `report` that does NOT appear in `cmd` can never produce a
    correct baseline: the runner writes results wherever `cmd` directs while the
    scorer looks at `report`. This must fail loudly, AT RETARGET TIME, naming both
    values — not silently no-op and surface later as a misleading downstream
    "no results written" error (#271)."""
    cfg_path = tmp_path / "ratchet.json"
    cfg_path.write_text(json.dumps({
        "suites": [{
            "name": "dotnet",
            "cmd": "dotnet test",  # no results-directory token at all
            "cwd": ".",
            "parser": "trx",
            "report": "stale-results-dir",  # left over from a previous cmd
        }]
    }))
    with pytest.raises(adopt.AdoptError) as exc_info:
        adopt._retarget_suite_reports(cfg_path, tmp_path / "wd")
    msg = str(exc_info.value)
    assert "dotnet" in msg
    assert "stale-results-dir" in msg
    assert "dotnet test" in msg
    # must not have rewritten the config on disk when it raised
    assert json.loads(cfg_path.read_text())["suites"][0]["report"] == "stale-results-dir"


def test_retarget_still_rewrites_when_report_matches_cmd(tmp_path):
    """Regression: the normal case — report token verbatim in cmd — still gets
    re-pointed outside the target tree exactly as before."""
    cfg_path = tmp_path / "ratchet.json"
    cfg_path.write_text(json.dumps({
        "suites": [{
            "name": "js",
            "cmd": "npm test -- --reporter junit --outputFile junit.xml",
            "cwd": ".",
            "parser": "junit-xml",
            "report": "junit.xml",
        }]
    }))
    workdir = tmp_path / "wd"
    adopt._retarget_suite_reports(cfg_path, workdir)
    cfg = json.loads(cfg_path.read_text())
    suite = cfg["suites"][0]
    assert suite["report"] == str(workdir / "js-junit.xml")
    # the stale bare token is gone; the new quoted absolute path took its place
    # (note: not an exact-string compare — pytest's own tmp_path fixture path
    # contains the substring "pytest", which legitimately triggers the
    # unrelated `-p no:cacheprovider` suffix branch below)
    assert "--outputFile junit.xml" not in suite["cmd"]
    assert f'"{workdir / "js-junit.xml"}"' in suite["cmd"]


def test_retarget_never_second_guesses_absolute_report(tmp_path):
    """A deliberately-absolute report path is never rewritten or flagged, even
    when it does not appear in cmd — the existing intent (a hand-authored suite
    is trusted) must hold (#271 AC)."""
    abs_report = str(tmp_path / "elsewhere" / "results.trx")
    cfg_path = tmp_path / "ratchet.json"
    cfg_path.write_text(json.dumps({
        "suites": [{
            "name": "dotnet",
            "cmd": "dotnet test",
            "cwd": ".",
            "parser": "trx",
            "report": abs_report,
        }]
    }))
    adopt._retarget_suite_reports(cfg_path, tmp_path / "wd")  # must not raise
    cfg = json.loads(cfg_path.read_text())
    assert cfg["suites"][0]["report"] == abs_report
    assert cfg["suites"][0]["cmd"] == "dotnet test"


def test_baseline_raises_and_leaves_target_tree_clean_on_report_cmd_mismatch(
    user_dir, tmp_path, monkeypatch, capsys,
):
    """Integration-level AC: a malformed suite config (report/cmd drifted apart)
    must fail the baseline before any suite ever runs, leaving the target tree
    byte-clean — not writing test-runner output into the tree being adopted
    (the violation sidecar mode exists to prevent) (#271 AC3). The suite's cmd
    genuinely writes into a repo-relative results dir when run (mirroring the
    real `dotnet test --results-directory` shape) — the malformed `report`
    field disagrees with it, so without the fix the cmd would actually run and
    dirty the tree before the downstream parser ever notices."""
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(target)

    def fake_init(args):
        cfg_path = resolver.quality_dir() / ratchet.CONFIG_NAME
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({
            "suites": [{
                "name": "dotnet",
                # writes for real into a repo-relative dir if it ever runs —
                # this is NOT the same token as `report` below.
                "cmd": "mkdir -p testresults && date > testresults/out.trx",
                "cwd": ".",
                "parser": "trx",
                "report": "stale-results-dir",  # left over from a previous cmd
            }]
        }, indent=2))
        return 0

    monkeypatch.setattr(adopt.ratchet, "cmd_init", fake_init)

    rc = adopt.main(["baseline", "--repo", str(target), "--workdir", str(tmp_path / "wd")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "dotnet" in err
    assert "stale-results-dir" in err
    assert "testresults" in err
    # the suite cmd never ran: no results dir materialized in the target tree
    assert not (target / "testresults").exists()
    assert_no_cw_meta(target)
    assert _tracked_clean(target)


# --- grandfather -----------------------------------------------------------------


def _run_through_baseline(target: Path, tmp_path: Path) -> artifacts.Resolver:
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["survey", "--repo", str(target)]) == 0
    assert adopt.main(["baseline", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    return artifacts.Resolver.resolve(target)


def test_grandfather_waiver_shape(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    resolver = _run_through_baseline(target, tmp_path)
    assert adopt.main(["grandfather", "--repo", str(target), "--owner", "pat"]) == 0
    doc = json.loads((resolver.meta_root / "adoption" / "grandfathered.json").read_text())
    assert doc["schema"] == "grandfather/1"
    debt = json.loads((resolver.quality_dir() / "debt.json").read_text())
    debt_ids = {i["id"] for i in debt["items"]}
    entry_ids = {e["id"] for e in doc["entries"]}
    assert debt_ids <= entry_ids
    expected_expiry = (date.today() + timedelta(days=90)).isoformat()
    for e in doc["entries"]:
        # modeled on the JUSTIFIED-waiver shape: reason/owner/expiry + source
        assert e["reason"] == "pre-adoption baseline"
        assert e["owner"] == "pat"
        assert e["expiry"] == expected_expiry
        assert e["source_engine"]
        # #216 F8: per-entry timestamp — what lets plan_from_debt verify
        # demand a waiver POSTDATE the plan (entries without one never waive)
        assert e["created_at"]
    assert_no_cw_meta(target)


def test_grandfather_expiry_flag(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    resolver = _run_through_baseline(target, tmp_path)
    assert adopt.main(["grandfather", "--repo", str(target), "--expiry", "2030-01-31"]) == 0
    doc = json.loads((resolver.meta_root / "adoption" / "grandfathered.json").read_text())
    assert all(e["expiry"] == "2030-01-31" for e in doc["entries"])


def test_grandfather_requires_baseline(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["grandfather", "--repo", str(target)]) == 2


# --- record + run ----------------------------------------------------------------


def test_record_writes_adoption_record(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    resolver = _run_through_baseline(target, tmp_path)
    assert adopt.main(["grandfather", "--repo", str(target)]) == 0
    assert adopt.main(["record", "--repo", str(target)]) == 0
    doc = json.loads((resolver.meta_root / "adoption" / "adoption.json").read_text())
    assert doc["schema"] == "adoption/1"
    assert doc["brownfield"] is True
    assert doc["mode"] == "sidecar"
    assert doc["adopted_at"]
    assert doc["scope"]
    assert doc["target_sha"] == artifacts.head_sha(target)
    assert set(doc["gates"]) == set(adopt.SHIPPED_GATES) | {"debt_inventory"}
    # baseline refs are real: the journaled record id + the debt.json sha
    cfg = ratchet.load_config(Path(target))
    records = ratchet.load_journal(cfg)
    rid = next(r["record_id"] for r in reversed(records) if r["event"] == "baseline")
    assert doc["baseline"]["ratchet_record_id"] == rid
    assert doc["baseline"]["debt_sha256"]
    assert doc["grandfather"]["entries"] >= 1
    assert doc["grandfather"]["nearest_expiry"]


def test_record_requires_survey(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["record", "--repo", str(target)]) == 2


def test_run_full_sequence(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    assert resolver.mode == "sidecar"
    adoption_dir = resolver.meta_root / "adoption"
    for name in ("survey.json", "grandfathered.json", "adoption.json"):
        assert (adoption_dir / name).is_file(), name
    assert (resolver.quality_dir() / "debt.json").is_file()
    assert_no_cw_meta(target)
    assert _tracked_clean(target)


# --- run executes the target's test command exactly once (#334) -----------------


def test_run_executes_test_suite_exactly_once(user_dir, tmp_path, monkeypatch):
    """`adopt.py run` used to run the detected test command TWICE against the
    same unchanged HEAD: once for the survey's coverage-baseline attempt
    (`coverage_baseline`), once for the ratchet-scored baseline
    (`ratchet.run_suite_measured`, inside `cmd_baseline`). Both are REAL
    subprocess executions of the same suite — only one may survive `run`."""
    target = make_target(tmp_path / "t")
    coverage_calls: list[int] = []
    orig_coverage_baseline = adopt.coverage_baseline

    def spy_coverage_baseline(*a, **k):
        coverage_calls.append(1)
        return orig_coverage_baseline(*a, **k)

    monkeypatch.setattr(adopt, "coverage_baseline", spy_coverage_baseline)

    suite_run_calls: list[int] = []
    orig_run_suite_measured = ratchet.run_suite_measured

    def spy_run_suite_measured(*a, **k):
        suite_run_calls.append(1)
        return orig_run_suite_measured(*a, **k)

    monkeypatch.setattr(ratchet, "run_suite_measured", spy_run_suite_measured)

    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    assert coverage_calls == [], (
        "survey must NOT run the suite a second time when `run`'s baseline "
        "step already scored it this HEAD — it must reuse the scorecard")
    assert suite_run_calls == [1], (
        "the ratchet-scored baseline must still be the ONE real execution")


def test_survey_coverage_numbers_equal_scorecard_derived_values(user_dir, tmp_path):
    """Acceptance criterion: survey's coverage numbers, when derived from a
    just-computed baseline scorecard, are test-pinned to that scorecard —
    not an independently re-parsed number that could silently drift."""
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    survey = json.loads((resolver.meta_root / "adoption" / "survey.json").read_text())
    sc = json.loads((resolver.quality_dir() / ratchet.SCORECARD_NAME).read_text())
    run = survey["test_run"]
    assert run["detected"] is True
    assert run["source"] == "ratchet-scorecard"
    smeas = sc["suite_measurement"]
    total_passed = sum(s["passed"] for s in run["steps"])
    assert total_passed == smeas["total_passing"] == sc["passed"]
    for step, suite_entry in zip(run["steps"], smeas["suites"], strict=True):
        assert step["passed"] == suite_entry["passing_cases"]
        assert step["exit_code"] == suite_entry["exit_code"]


def test_baseline_pass_set_identical_with_and_without_reuse(user_dir, tmp_path):
    """Acceptance criterion: the baseline record's pass-set is IDENTICAL
    whether reached via `run` (survey reuses the scorecard) or via the old
    two-real-runs path (standalone survey THEN standalone baseline) — the
    dedup changes nothing about what gets journaled as the pass-set."""
    target_a = make_target(tmp_path / "a")
    assert adopt.main(["run", "--repo", str(target_a),
                       "--workdir", str(tmp_path / "wd-a")]) == 0
    resolver_a = artifacts.Resolver.resolve(target_a)
    sc_a = json.loads((resolver_a.quality_dir() / ratchet.SCORECARD_NAME).read_text())

    target_b = make_target(tmp_path / "b")
    artifacts.elect(target_b, "sidecar", backing="local")
    assert adopt.main(["survey", "--repo", str(target_b)]) == 0
    assert adopt.main(["baseline", "--repo", str(target_b),
                       "--workdir", str(tmp_path / "wd-b")]) == 0
    resolver_b = artifacts.Resolver.resolve(target_b)
    sc_b = json.loads((resolver_b.quality_dir() / ratchet.SCORECARD_NAME).read_text())

    assert sc_a["pass_set"] == sc_b["pass_set"]
    assert sc_a["tests_run"] == sc_b["tests_run"] is True


def test_survey_falls_back_to_real_run_when_reused_scorecard_is_stale(user_dir, tmp_path, monkeypatch):
    """The reuse is provably-fresh only: a scorecard whose `target_sha` does
    not match the CURRENT HEAD (e.g. a commit landed between two steps) must
    never be silently trusted — build_survey re-runs for real instead,
    mirroring `ratchet.py score --reuse-report`'s loud-fail-on-stale
    discipline (never SERVE a stale measurement as current)."""
    target = make_target(tmp_path / "t")
    resolver = artifacts.elect(target, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(target)
    calls: list[int] = []
    orig_coverage_baseline = adopt.coverage_baseline

    def spy(*a, **k):
        calls.append(1)
        return orig_coverage_baseline(*a, **k)

    monkeypatch.setattr(adopt, "coverage_baseline", spy)
    stale_scorecard = {
        "target_sha": "0" * 40,  # provably not this repo's real HEAD sha
        "passed": 0,
        "pass_set": [],
        "tests_run": True,
        "suite_measurement": {"status": "applicable", "suites": [], "total_passing": 0},
    }
    doc = adopt.build_survey(target, resolver, reuse_scorecard=stale_scorecard)
    assert calls == [1], "a stale (mismatched-HEAD) scorecard must trigger a REAL fallback run"
    assert doc["test_run"]["detected"] is True
    assert doc["test_run"].get("source") != "ratchet-scorecard"


# --- gates honor the grandfather file --------------------------------------------


def test_debt_inventory_marks_grandfathered(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    out_path = resolver.quality_dir() / "debt.json"
    envelope = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd2"), out_path, resolver=resolver)
    marked = [i for i in envelope["items"] if i.get("grandfathered")]
    assert marked, "grandfathered items must stay IN the inventory, labeled"
    for i in marked:
        assert i["grandfather_expiry"]
        assert i["grandfather_expired"] is False
        # #216 F8: the entry's own timestamp rides on the item so verify can
        # require a waiver to postdate the plan
        assert i["grandfather_created_at"]
    gf = envelope["grandfather"]
    assert gf["count"] == len(marked)
    assert gf["expired"] == []
    assert "grandfathered" in debt_inventory.format_report(envelope)


def test_debt_inventory_flags_expired_grandfathers(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    gf_path = resolver.meta_root / "adoption" / "grandfathered.json"
    doc = json.loads(gf_path.read_text())
    for e in doc["entries"]:
        e["expiry"] = "2020-01-01"
    gf_path.write_text(json.dumps(doc))
    out_path = resolver.quality_dir() / "debt.json"
    envelope = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd2"), out_path, resolver=resolver)
    expired = [i for i in envelope["items"] if i.get("grandfather_expired")]
    assert expired
    assert envelope["grandfather"]["expired"] == sorted(i["id"] for i in expired)
    assert "EXPIRED grandfather" in debt_inventory.format_report(envelope)


def test_slop_gate_debt_block_labels_grandfathers():
    debt = {
        "counts": {"markers": {"low": 2}},
        "target_sha": "abc",
        "items": [
            {"id": "DEBT-1111111111", "grandfathered": True,
             "grandfather_expiry": "2030-01-01", "grandfather_expired": False},
            {"id": "DEBT-2222222222", "grandfathered": True,
             "grandfather_expiry": "2020-01-01", "grandfather_expired": True},
        ],
    }
    block = quality_slop_gate.format_debt_block(debt)
    assert "2 grandfathered" in block
    assert "EXPIRED grandfather: DEBT-2222222222" in block


def test_status_shows_adoption_section(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    st = status.gather(target)
    a = st["adoption"]
    assert a["brownfield"] is True
    assert a["mode"] == "sidecar"
    assert a["grandfathered"] >= 1
    assert a["nearest_expiry"]
    assert a["expired"] == []
    text = status.render_text(st)
    assert "## Adoption" in text
    assert "brownfield" in text
    assert "nearest expiry" in text

    # expire the grandfathers -> /status warns prominently
    resolver = artifacts.Resolver.resolve(target)
    gf_path = resolver.meta_root / "adoption" / "grandfathered.json"
    doc = json.loads(gf_path.read_text())
    for e in doc["entries"]:
        e["expiry"] = "2020-01-01"
    gf_path.write_text(json.dumps(doc))
    text = status.render_text(status.gather(target))
    assert "EXPIRED" in text


def test_status_without_adoption_record(user_dir, tmp_path):
    target = make_target(tmp_path / "t")
    st = status.gather(target)
    assert st["adoption"] is None
    assert "no adoption record" in status.render_text(st)


# --- F7: empty pass-set baseline is recorded as not-run --------------------------


def test_baseline_no_test_repo_records_not_run(user_dir, tmp_path, capsys):
    """A repo with no test runner must NOT journal an empty pass-set as a real
    test run: the score falls back to --no-tests semantics (tests_run: false),
    the message says so, and the journal note says exactly that (F7)."""
    target = make_target(tmp_path / "t", with_tests=False, with_pyproject=False)
    artifacts.elect(target, "sidecar", backing="local")
    assert adopt.main(["survey", "--repo", str(target)]) == 0
    capsys.readouterr()
    assert adopt.main(["baseline", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    out = capsys.readouterr().out
    assert "no test suites ran — pass-set baseline EMPTY (recorded as not-run)" in out
    # survey verdict cross-check: the survey said no runner, the baseline agrees
    assert "consistent with the survey verdict" in out
    resolver = artifacts.Resolver.resolve(target)
    sc = json.loads((resolver.quality_dir() / ratchet.SCORECARD_NAME).read_text())
    assert sc["tests_run"] is False
    assert sc["pass_set"] == []
    records = ratchet.load_journal(ratchet.load_config(Path(target)))
    assert records[-1]["event"] == "baseline"
    assert "EMPTY" in records[-1]["notes"]
    assert "not-run" in records[-1]["notes"]


# --- F1: re-grandfather amnesty + re-adoption guards -----------------------------


def test_grandfather_refuses_regrandfather_by_default(user_dir, tmp_path, capsys):
    target = make_target(tmp_path / "t")
    _run_through_baseline(target, tmp_path)
    assert adopt.main(["grandfather", "--repo", str(target)]) == 0
    capsys.readouterr()
    assert adopt.main(["grandfather", "--repo", str(target)]) == 2
    err = capsys.readouterr().err
    assert "refusing to re-grandfather" in err
    assert "0 finding(s) would be newly added: (none)" in err
    assert "--extend" in err


def test_grandfather_extend_amnesties_delta_preserving_originals(user_dir, tmp_path, capsys):
    target = make_target(tmp_path / "t")
    resolver = _run_through_baseline(target, tmp_path)
    assert adopt.main(["grandfather", "--repo", str(target)]) == 0
    gf_path = resolver.meta_root / "adoption" / "grandfathered.json"
    original = json.loads(gf_path.read_text())
    original_expiries = {e["id"]: e["expiry"] for e in original["entries"]}

    # a POST-adoption finding appears: new TODO marker -> new DEBT- id
    (target / "app.py").write_text((target / "app.py").read_text()
                                   + "\n# TODO: post-adoption debt\n")
    out_path = resolver.quality_dir() / "debt.json"
    envelope = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd2"), out_path, resolver=resolver)
    out_path.write_text(json.dumps(envelope, indent=2) + "\n")
    new_ids = {i["id"] for i in envelope["items"]} - set(original_expiries)
    assert len(new_ids) == 1
    new_id = new_ids.pop()

    # refusal prints the EXACT delta (the post-adoption finding's id)
    capsys.readouterr()
    assert adopt.main(["grandfather", "--repo", str(target)]) == 2
    err = capsys.readouterr().err
    assert "1 finding(s) would be newly added" in err
    assert new_id in err

    # --extend performs it, loudly, preserving created_at + original expiries
    assert adopt.main(["grandfather", "--repo", str(target), "--extend",
                       "--expiry", "2031-06-30"]) == 0
    out = capsys.readouterr().out
    assert "amnestying 1 POST-adoption finding(s)" in out
    assert new_id in out
    doc = json.loads(gf_path.read_text())
    assert doc["created_at"] == original["created_at"]
    assert doc["extended_at"]
    by_id = {e["id"]: e for e in doc["entries"]}
    for eid, expiry in original_expiries.items():
        assert by_id[eid]["expiry"] == expiry  # originals keep their expiry
    assert by_id[new_id]["expiry"] == "2031-06-30"  # new entry: fresh expiry


def test_run_refuses_readoption_without_flag(user_dir, tmp_path, capsys):
    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    capsys.readouterr()
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 2
    err = capsys.readouterr().err
    assert "already adopted (adopted_at" in err
    assert "explicit operator act" in err
    # --re-adopt (+ --extend for the grandfather step) re-runs the arrow
    assert adopt.main(["run", "--repo", str(target), "--re-adopt", "--extend",
                       "--workdir", str(tmp_path / "wd")]) == 0


# --- F5: the blocking gates read the grandfather file ----------------------------


def _gf_file(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "grandfathered.json"
    p.write_text(json.dumps({"schema": "grandfather/1", "entries": entries}))
    return p


def _future() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def test_traceability_gate_honors_grandfathers(tmp_path):
    import check_traceability as ct
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "## Contracts\n\n**CTR-app-001**: first\n\n**CTR-app-002**: second\n")
    entries = [
        {"id": "check_traceability:uncovered:CTR-app-001", "expiry": _future()},
        {"id": "check_traceability:untested:CTR-app-001", "expiry": _future()},
    ]
    report = ct.check(epic, grandfather_path=_gf_file(tmp_path, entries))
    # the pre-existing (grandfathered) gap is waived out of the blocking lists
    assert "CTR-app-001" not in report.uncovered_contracts
    assert "CTR-app-001" not in report.untested_contracts
    assert {g["id"] for g in report.grandfathered_contracts} == {"CTR-app-001"}
    assert report.counts["grandfathered"] == 2  # uncovered + untested
    # the NEW gap (absent from the file) still blocks
    assert "CTR-app-002" in report.uncovered_contracts
    assert not report.coverage_ok
    assert "Grandfathered (pre-adoption baseline" in ct.render_markdown(report)

    # waiving every gap satisfies coverage
    entries += [
        {"id": "check_traceability:uncovered:CTR-app-002", "expiry": _future()},
        {"id": "check_traceability:untested:CTR-app-002", "expiry": _future()},
    ]
    report = ct.check(epic, grandfather_path=_gf_file(tmp_path, entries))
    assert report.coverage_ok

    # EXPIRED entries do NOT waive: the gaps block again, labeled
    for e in entries:
        e["expiry"] = "2020-01-01"
    report = ct.check(epic, grandfather_path=_gf_file(tmp_path, entries))
    assert not report.coverage_ok
    assert "CTR-app-001" in report.uncovered_contracts
    assert {g["id"] for g in report.expired_grandfathers} == {"CTR-app-001", "CTR-app-002"}
    assert "EXPIRED grandfather" in ct.render_markdown(report)


def test_traceability_cli_grandfather_gate(tmp_path, capsys):
    import check_traceability as ct
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("**CTR-app-001**: only\n")
    gf = _gf_file(tmp_path, [
        {"id": "check_traceability:uncovered:CTR-app-001", "expiry": _future()},
        {"id": "check_traceability:untested:CTR-app-001", "expiry": _future()},
    ])
    assert ct.main([str(epic), "--gate", "coverage", "--grandfather", str(gf)]) == 0
    capsys.readouterr()
    # expired -> blocks again through the CLI too
    gf = _gf_file(tmp_path, [
        {"id": "check_traceability:uncovered:CTR-app-001", "expiry": "2020-01-01"},
        {"id": "check_traceability:untested:CTR-app-001", "expiry": "2020-01-01"},
    ])
    assert ct.main([str(epic), "--gate", "coverage", "--grandfather", str(gf)]) == 1


def test_single_writer_gate_honors_grandfathers(tmp_path):
    import check_single_writer as sw
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text(
        "**INV-bil-001**: single write path\n"
        "<!-- @cw-writes INV-bil-001 controls_field=provider.stripe_plan "
        "sanctioned_writers=ReconcileStripe -->\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "admin.go").write_text(
        "func ChangePlan(p *Provider, v string) {\n\tp.StripePlan = v\n}\n")
    key = "check_single_writer:INV-bil-001:provider.stripe_plan:admin.go"
    gf = _gf_file(tmp_path, [{"id": key, "expiry": _future()}])
    report = sw.check(epic, src, grandfather_path=gf)
    # the pre-existing violation is waived: reported, never blocking
    assert report.violations == []
    assert len(report.grandfathered) == 1
    assert report.grandfathered[0]["file"] == "admin.go"
    assert report.coverage_ok
    assert "Grandfathered writers" in sw.render_text(report)

    # a NEW violation (a second writer absent from the file) still blocks
    (src / "sneaky.go").write_text(
        "func Sneaky(p *Provider, v string) {\n\tp.StripePlan = v\n}\n")
    report = sw.check(epic, src, grandfather_path=gf)
    assert [v["file"] for v in report.violations] == ["sneaky.go"]
    assert not report.coverage_ok

    # EXPIRED entry does NOT waive: the old violation blocks again, labeled
    gf = _gf_file(tmp_path, [{"id": key, "expiry": "2020-01-01"}])
    report = sw.check(epic, src, grandfather_path=gf)
    assert {v["file"] for v in report.violations} == {"admin.go", "sneaky.go"}
    expired = next(v for v in report.violations if v["file"] == "admin.go")
    assert expired["grandfather_expired"] is True
    assert "EXPIRED grandfather" in sw.render_text(report)


# --- F8: expiry is computed LIVE at render time ----------------------------------


def test_render_surfaces_compute_expiry_live(user_dir, tmp_path, monkeypatch):
    """An inventory built with a FUTURE expiry (stored grandfather_expired:
    false) must still surface EXPIRED once the clock passes the expiry — the
    slop-gate debt block, the quality-report debt section, and code_query's
    debt facts all overlay the stored snapshot with a live compare."""
    import code_query
    from chief_wiggum import grandfather as cw_gf
    from quality import report as quality_report

    target = make_target(tmp_path / "t")
    assert adopt.main(["run", "--repo", str(target),
                       "--workdir", str(tmp_path / "wd")]) == 0
    resolver = artifacts.Resolver.resolve(target)
    gf_path = resolver.meta_root / "adoption" / "grandfathered.json"
    doc = json.loads(gf_path.read_text())
    for e in doc["entries"]:
        e["expiry"] = "2030-06-30"  # future at build time
    gf_path.write_text(json.dumps(doc))
    out_path = resolver.quality_dir() / "debt.json"
    envelope = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd2"), out_path, resolver=resolver)
    out_path.write_text(json.dumps(envelope, indent=2) + "\n")
    assert all(i["grandfather_expired"] is False
               for i in envelope["items"] if i.get("grandfathered"))
    # built before expiry: nothing renders EXPIRED yet
    assert "EXPIRED grandfather" not in quality_slop_gate.format_debt_block(envelope)

    # the clock passes the expiry (build-time snapshot now stale)
    class _Future(date):
        @classmethod
        def today(cls):
            return cls(2031, 1, 1)

    monkeypatch.setattr(cw_gf, "date", _Future)
    assert "EXPIRED grandfather" in debt_inventory.format_report(envelope)
    assert "EXPIRED grandfather" in quality_slop_gate.format_debt_block(envelope)
    md = quality_report.render_markdown({"debt": envelope}, {"summary": {}}, [])
    assert "EXPIRED grandfather" in md
    facts = code_query._debt_facts_for_file(Path(target), "app.py")
    assert facts, "app.py's TODO marker must produce a debt fact"
    assert any(f.statement.startswith("[EXPIRED grandfather]") for f in facts)
    assert all(f.extra["grandfather_expired"] for f in facts
               if f.extra.get("grandfathered"))


# --- verdict logic (pure) --------------------------------------------------------


def test_gate_verdicts_pure():
    v = adopt.gate_verdicts(
        has_epic_ids=False, has_single_writer_invariants=False,
        has_architecture_model=False, suites=[], ci_present=False,
        source_files=0, unknown_extensions={".zig": 3}, age_days=3,
    )
    assert v["check_traceability"]["verdict"] == "report-only"
    assert v["ratchet"]["verdict"] == "report-only"
    assert v["debt_inventory"]["verdict"] == "report-only"
    assert ".zig" in v["debt_inventory"]["reason"]
    assert "git history present" in v["quality_slop_gate"]["reason"]
    assert "14 days" in v["quality_slop_gate"]["reason"]

    # F9 (#215): "git history present" only when a commit exists — with no
    # readable history the slop gate degrades to report-only and says why.
    v = adopt.gate_verdicts(
        has_epic_ids=False, has_single_writer_invariants=False,
        has_architecture_model=False, suites=[], ci_present=False,
        source_files=0, unknown_extensions={}, age_days=None,
    )
    assert v["quality_slop_gate"]["verdict"] == "report-only"
    assert "git history present" not in v["quality_slop_gate"]["reason"]
    assert "no readable commit history" in v["quality_slop_gate"]["reason"]

    v = adopt.gate_verdicts(
        has_epic_ids=True, has_single_writer_invariants=True,
        has_architecture_model=True, suites=[{"name": "pytest"}], ci_present=True,
        source_files=10, unknown_extensions={}, age_days=400,
    )
    assert v["check_traceability"]["verdict"] == "applicable"
    assert v["check_single_writer"]["verdict"] == "applicable"
    assert v["check_architecture"]["verdict"] == "applicable"
    assert v["ratchet"]["verdict"] == "applicable"
    assert v["ci_scaffold"]["verdict"] == "applicable"
    assert v["saas_gate"]["verdict"] == "inapplicable"  # never survey-derivable

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
    out = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    # __pycache__/.pytest_cache from the target's own test run are not CW writes
    lines = [
        ln for ln in out.splitlines()
        if ln.strip() and "__pycache__" not in ln and ".pytest_cache" not in ln
    ]
    return lines == []


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


def test_survey_embedded_default_writes_target_docs(user_dir, tmp_path):
    """Without an election the resolver default is embedded — survey.json goes
    to <target>/docs/adoption (the documented embedded contract)."""
    target = make_target(tmp_path / "t")
    assert adopt.main(["survey", "--repo", str(target)]) == 0
    assert (target / "docs" / "adoption" / "survey.json").is_file()


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
    # the target tree stays clean: no junit report, no docs/, nothing CW
    assert_no_cw_meta(target)
    assert not (target / ".ratchet-junit.xml").exists()
    assert _tracked_clean(target)
    # ratchet check holds against its own fresh baseline
    rc = ratchet.cmd_check(argparse.Namespace(
        repo=str(target), format="text", gate_verifier_tests=False, gate_quality=False))
    assert rc == 0


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
    assert "14 days" in v["quality_slop_gate"]["reason"]

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

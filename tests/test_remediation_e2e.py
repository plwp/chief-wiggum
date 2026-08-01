"""End-to-end remediation exercise (#216) — a full miniature remediation epic
against a tmp fixture repo, exercising every mechanic the issue ships:

  adopt-shaped setup (sidecar election + adoption record + debt baseline)
    → plan_from_debt plan (budgeted, 1 ticket)
    → CHARACTERIZATION tests pinning current behavior (all green BEFORE the
      refactor — the refactor ticket-kind's inverted TDD objective)
    → the refactor itself (resolves the ticketed DEBT- ids)
    → ratchet pathset check (ticket-scoped scope back-pressure, report-only)
    → prevention_signals over the diff (report-only, exit 0)
    → fresh inventory + plan_from_debt verify (the epic's acceptance test)

The real-repo counterpart (mcprelay, read-only, isolated user dir) is run
manually per the issue's AC; this test keeps the whole loop executable in CI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import artifacts
import debt_inventory
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _commit_all(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, "--no-verify")


MODULE_V1 = """\
def price_with_tax(amount):
    return round(amount * 1.1, 2)


def legacy_discount(amount):
    return amount * 0.9


# TODO: fold tax rate into config
"""

# The refactor: dead export removed, marker resolved, behavior PRESERVED.
MODULE_V2 = """\
TAX_RATE = 1.1  # folded from the old inline constant


def price_with_tax(amount):
    return round(amount * TAX_RATE, 2)
"""

HOLLOW_TEST = """\
def test_pricing_smoke():
    import pricing
    pricing.price_with_tax(1)
"""

CHARACTERIZATION_TEST = """\
# Characterization tests: pin CURRENT observed behavior before refactoring
# (golden-master style — values captured from the running code, not derived
# from a spec).
import pricing


def test_price_with_tax_pins_current_values():
    assert pricing.price_with_tax(100) == 110.0
    assert pricing.price_with_tax(0) == 0.0
    assert pricing.price_with_tax(9.99) == 10.99


def test_pricing_smoke():
    assert pricing.price_with_tax(1) == 1.1
"""


@pytest.fixture()
def adopted(tmp_path, monkeypatch):
    """A brownfield fixture repo, adopted sidecar-style with an isolated CW
    user dir: election + adoption record + debt baseline, target tree clean."""
    cw_home = tmp_path / "cw-home"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(cw_home))
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    (repo / "pricing.py").write_text(MODULE_V1)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_pricing.py").write_text(HOLLOW_TEST)
    _commit_all(repo, "legacy pricing module")
    # Real co-change history: pricing.py and its test move together, so the
    # coupling engine (min 4 co-changes) links them and the planner's
    # precedence (c) merges the two directory clusters into ONE ticket.
    for i in range(4):
        with (repo / "pricing.py").open("a") as f:
            f.write(f"# rev {i}\n")
        with (tests_dir / "test_pricing.py").open("a") as f:
            f.write(f"# rev {i}\n")
        _commit_all(repo, f"co-change {i}")

    # sidecar election + adoption record = the brownfield switch (#215)
    artifacts.elect(repo, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(str(repo))
    adoption_dir = resolver.meta_root / "adoption"
    adoption_dir.mkdir(parents=True, exist_ok=True)
    (adoption_dir / "adoption.json").write_text(json.dumps(resolver.stamp({
        "schema": "adoption/1", "brownfield": True, "mode": "sidecar",
    }), indent=2))

    # debt baseline into the sidecar quality dir
    quality_dir = resolver.quality_dir()
    quality_dir.mkdir(parents=True, exist_ok=True)
    env = debt_inventory.build_inventory(
        str(repo), str(tmp_path / "wd"), quality_dir / "debt.json",
        resolver=resolver)
    (quality_dir / "debt.json").write_text(json.dumps(env, indent=2))
    return repo, resolver, env


def _pytest_in(repo: Path, *paths: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
        cwd=str(repo), capture_output=True, text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin",
             "PYTHONPATH": str(repo), "HOME": str(repo.parent)},
    )


def test_full_miniature_remediation_epic(adopted, tmp_path):
    repo, resolver, baseline = adopted
    quality_dir = resolver.quality_dir()

    # The seeded fixture yields real findings: a dead export, a hollow test,
    # a TODO marker — all in the root module cluster.
    baseline_ids = {i["id"] for i in baseline["items"]}
    assert baseline_ids, "fixture must produce debt findings"

    # ---- plan: budget REQUIRED, one ticket, derived pathset -----------------
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "plan",
         "--repo", str(repo), "--budget-count", "1"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads((quality_dir / "remediation-plan.json").read_text())
    assert len(plan["tickets"]) == 1
    ticket = plan["tickets"][0]
    assert ticket["kind"] == "refactor"
    ticketed_ids = set(ticket["debt_ids"])
    assert ticketed_ids <= baseline_ids
    assert "pricing.py" in ticket["pathset"]["paths"]

    # ---- pathset derivation with declared collateral (the tests that move) --
    pathset_file = tmp_path / "pathset.json"
    subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "pathset",
         "--plan", str(quality_dir / "remediation-plan.json"), "--id", ticket["id"],
         "--collateral", "tests/*.py", "-o", str(pathset_file)],
        capture_output=True, text=True, check=True,
    )
    spec = json.loads(pathset_file.read_text())
    assert "pricing.py" in spec["paths"] and "tests/*.py" in spec["paths"]

    # ---- refactor branch: characterization FIRST, all green, then refactor --
    _git(repo, "checkout", "-q", "-b", "refactor/RT-001")
    (repo / "tests" / "test_pricing.py").write_text(CHARACTERIZATION_TEST)
    green_before = _pytest_in(repo, "tests/test_pricing.py")
    assert green_before.returncode == 0, (
        "characterization tests must pass BEFORE the refactor:\n" + green_before.stdout)
    _commit_all(repo, "test: characterization baseline for RT-001")

    (repo / "pricing.py").write_text(MODULE_V2)
    green_after = _pytest_in(repo, "tests/test_pricing.py")
    assert green_after.returncode == 0, (
        "behavior must be preserved against the pinned baseline:\n" + green_after.stdout)
    _commit_all(repo, "refactor: RT-001 — remove dead export, fold tax rate")

    # ---- scope back-pressure: ratchet pathset (report-only, then teeth) -----
    in_scope = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "pathset",
         "--repo", str(repo), "--base", "main",
         "--pathset-file", str(pathset_file), "--report-only"],
        capture_output=True, text=True,
    )
    assert in_scope.returncode == 0
    assert "within the sanctioned pathset" in in_scope.stdout

    # an out-of-pathset collateral edit is flagged (report-only exits 0 but
    # names the file; blocking mode exits 1 — the parking machinery)
    (repo / "README.md").write_text("drive-by improvement\n")
    _commit_all(repo, "collateral creep")
    flagged = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "pathset",
         "--repo", str(repo), "--base", "main",
         "--pathset-file", str(pathset_file), "--report-only"],
        capture_output=True, text=True,
    )
    assert flagged.returncode == 0 and "README.md" in flagged.stderr
    blocking = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "pathset",
         "--repo", str(repo), "--base", "main",
         "--pathset-file", str(pathset_file)],
        capture_output=True, text=True,
    )
    assert blocking.returncode == 1
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # undo the creep, stay clean

    # ---- found ≠ fixed: file a mid-ticket discovery, leave the diff alone ---
    # (F2: it lands in the MODE-INDEPENDENT pending store under the isolated
    # user dir — never the target tree, never any debt.json directly)
    cand_env = {"CHIEF_WIGGUM_USER_DIR": str(tmp_path / "cw-home"),
                "PATH": __import__("os").environ["PATH"]}
    cand_out = subprocess.run(
        [sys.executable, str(SCRIPTS / "debt_inventory.py"), "append-candidate",
         "--repo", str(repo), "--engine", "manual", "--path", "pricing.py",
         "--note", "rounding rule duplicated in the invoicing sheet"],
        capture_output=True, text=True, env=cand_env,
    )
    assert cand_out.returncode == 0 and "candidate DEBT-" in cand_out.stdout
    pending = debt_inventory.load_pending(resolver)
    assert len(pending) == 1 and pending[0]["candidate"] is True
    cand_id = pending[0]["id"]

    # ---- prevention signals over the refactor diff (report-only) ------------
    signals = subprocess.run(
        [sys.executable, str(SCRIPTS / "prevention_signals.py"),
         "--repo", str(repo), "--base", "main",
         "--workdir", str(tmp_path / "sig-wd"), "--format", "json"],
        capture_output=True, text=True,
    )
    assert signals.returncode == 0, signals.stderr
    sig = json.loads(signals.stdout)
    assert sig["signals"]["dead_code_introduced"]["findings"] == []
    assert sig["signals"]["assertion_free_tests_added"]["findings"] == []

    # ---- acceptance: fresh inventory; verify fails BEFORE merge-state fix ---
    # (fresh run on the refactor branch: the ticketed ids must be gone; the
    # scratch-dir inventory still carries the pending candidate — F2)
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_env = debt_inventory.build_inventory(
        str(repo), str(tmp_path / "wd2"), fresh_dir / "debt.json", resolver=resolver)
    (fresh_dir / "debt.json").write_text(json.dumps(fresh_env, indent=2))
    assert cand_id in {i["id"] for i in fresh_env["items"]}, (
        "scratch-dir inventory must retain pending candidates")

    verify = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "verify",
         "--repo", str(repo),
         "--plan", str(quality_dir / "remediation-plan.json"),
         "--debt", str(fresh_dir / "debt.json"), "--format", "json"],
        capture_output=True, text=True, env=cand_env,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    result = json.loads(verify.stdout)
    assert result["ok"] is True
    assert result["unresolved"] == [] and result["moved"] == []
    assert set(result["resolved"]) == ticketed_ids

    # negative control: verified against the STALE baseline inventory, the
    # ticketed ids are still present -> exit 1 listing them
    stale = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "verify",
         "--repo", str(repo),
         "--plan", str(quality_dir / "remediation-plan.json"),
         "--debt", str(quality_dir / "debt.json"), "--format", "json"],
        capture_output=True, text=True, env=cand_env,
    )
    assert stale.returncode == 1
    assert {u["id"] for u in json.loads(stale.stdout)["unresolved"]} == ticketed_ids

    # ---- sidecar hygiene: the whole loop wrote NOTHING into the target ------
    status = _git(repo, "status", "--porcelain").stdout
    assert status.strip() == "", f"target tree must stay clean, got:\n{status}"
    assert not (repo / "docs").exists()

"""Sidecar round-trip proof (chief-wiggum#213 acceptance criterion).

Runs the SAME gate sequence twice — ratchet init/score/check, traceability
coverage, single-writer, /status — once against an embedded target (artifacts
in ``<target>/docs``, in-source ``@cw-trace`` annotations) and once operated
from a sidecar against a target with ZERO chief-wiggum files in-tree (artifacts
in the sidecar meta root, the external symbol-anchored link store instead of
in-source annotations), and asserts:

(a) the sidecar target tree never gains a CW meta file at any step;
(b) the gate verdicts/applicability are identical between the two modes;
(c) the external-link store satisfies traceability coverage exactly where the
    embedded run used in-source annotations (and without it, coverage fails —
    the store is load-bearing, not decorative).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import artifacts
import check_single_writer
import check_traceability as ct
import pytest
import ratchet
import status
from chief_wiggum import external_links

IDS = ["CTR-app-001", "CTR-app-002", "INV-app-003"]

APP_PY = """\
def add_order(orders, start_date, end_date):
    {annotation}if start_date > end_date:
        raise ValueError("CTR-app-001: start_date must be <= end_date")
    orders.append((start_date, end_date))
    return orders
"""

TEST_APP_PY = """\
from app import add_order


def test_add_order():
    {annotation}orders = add_order([], "2026-01-01", "2026-01-02")
    assert orders == [("2026-01-01", "2026-01-02")]
"""

CONTRACTS_MD = """\
### CTR-app-001 — valid date range
REQUIRES: start_date <= end_date
"""

INVARIANTS_MD = """\
- **INV-app-003**: orders list only ever grows
"""

MODELS_CONTRACTS_JSON = {"id": "CTR-app-002", "description": "orders append-only"}

# Filenames that are chief-wiggum meta, wherever they appear. The sidecar
# target must never contain ANY of these (nor a docs/ tree at all).
CW_META_NAMES = {
    "election.json", "scope.json", "ratchet.json", "ratchet-journal.jsonl",
    "ratchet-highwater.json", "ratchet-scorecard.json", "trace-links.json",
    "external-links.json", "contracts.md", "invariants.md", "adopted.json",
}


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    """Isolated ~/.chief-wiggum — tests must never touch the real home dir."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_target(path: Path, *, annotated: bool) -> Path:
    """A tmp target git repo: a source file, a passing pytest test, one commit.

    ``annotated=True`` (embedded mode) puts ``@cw-trace`` comments in-source;
    ``annotated=False`` (sidecar mode) leaves the source completely clean —
    the external link store carries the claims instead.
    """
    (path / "tests").mkdir(parents=True)
    guard = "# @cw-trace guards CTR-app-001 CTR-app-002 INV-app-003\n    " if annotated else ""
    verify = "# @cw-trace verifies CTR-app-001 CTR-app-002 INV-app-003\n    " if annotated else ""
    (path / "app.py").write_text(APP_PY.format(annotation=guard))
    (path / "tests" / "test_app.py").write_text(TEST_APP_PY.format(annotation=verify))
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def write_epic(epic_dir: Path) -> Path:
    """The architect-produced artifact set — identical bytes in both modes."""
    (epic_dir / "models").mkdir(parents=True)
    (epic_dir / "contracts.md").write_text(CONTRACTS_MD)
    (epic_dir / "invariants.md").write_text(INVARIANTS_MD)
    (epic_dir / "models" / "contracts.json").write_text(json.dumps(MODELS_CONTRACTS_JSON))
    return epic_dir


def configure_ratchet(target: Path, junit: Path, epic_docs: str) -> Path:
    """ratchet init, then pin the suite + epic_docs. The junit report is an
    ABSOLUTE path outside the target so the suite run leaves no artifact
    in-tree; in sidecar mode ``epic_docs`` is the absolute sidecar epics path
    (docs/sidecar.md — the embedded default 'docs/epics' is target-relative)."""
    assert ratchet.cmd_init(argparse.Namespace(repo=str(target), force=False)) == 0
    config_path = ratchet.default_state_dir(target) / ratchet.CONFIG_NAME
    assert config_path.is_file()
    cfg = json.loads(config_path.read_text())
    cfg["suites"] = [{
        "name": "py",
        "cmd": f'"{sys.executable}" -m pytest tests -q -p no:cacheprovider --junitxml="{junit}"',
        "cwd": ".",
        "parser": "junit-xml",
        "report": str(junit),
    }]
    cfg["epic_docs"] = epic_docs
    config_path.write_text(json.dumps(cfg))
    return config_path


def run_gates(target: Path, epic_dir: Path, junit: Path, epic_docs: str,
              external_store: Path | None = None,
              step_check=lambda: None) -> dict:
    """The shared sequence, returning only mode-independent salient results.
    ``step_check`` runs after every step (the sidecar run passes the
    zero-CW-files assertion; the embedded run a no-op)."""
    configure_ratchet(target, junit, epic_docs)
    step_check()
    score_rc = ratchet.cmd_score(argparse.Namespace(
        repo=str(target), no_tests=False, no_quality=True, venv=None, gobin=None))
    step_check()
    check_rc = ratchet.cmd_check(argparse.Namespace(
        repo=str(target), format="text", gate_verifier_tests=False, gate_quality=False))
    step_check()
    scorecard = json.loads((ratchet.default_state_dir(target) / ratchet.SCORECARD_NAME).read_text())

    trace = ct.check(epic_dir, target, external_links_path=external_store)
    step_check()
    sw = check_single_writer.check(epic_dir, target)
    step_check()
    st = status.gather(target)
    step_check()

    return {
        "ratchet_score_rc": score_rc,
        "ratchet_check_rc": check_rc,
        "pass_set": scorecard["pass_set"],
        "contract_hashes": scorecard["contract_hashes"],
        "trace": {
            "soundness_ok": trace.soundness_ok,
            "coverage_ok": trace.coverage_ok,
            "applicability": trace.applicability,
            "defined": sorted(trace.defined),
            "uncovered": trace.uncovered_contracts,
            "untested": trace.untested_contracts,
            "dangling": trace.dangling,
            "invalid_links": trace.invalid_links,
        },
        "single_writer": {
            "applicability": sw.applicability,
            "violations": sw.violations,
            "malformed": sw.malformed,
        },
        # verifier_hashes is deliberately NOT compared: the verifier-hash
        # dimension (#206) reads in-source annotations, which sidecar mode
        # removes by design — embedded scores 1, sidecar 0.
        "status_ratchet": {k: st["ratchet"][k] for k in
                           ("configured", "scorecard", "pass_set", "contracts")},
        "status_gates": st["gates"],
    }


def assert_no_cw_meta(target: Path) -> None:
    """(a) ZERO chief-wiggum files in the sidecar target tree — no docs/ at
    all, and no CW meta filename anywhere (a target-native .pytest_cache or
    __pycache__ from its own test run is not CW meta)."""
    assert not (target / "docs").exists()
    offenders = [p for p in target.rglob("*") if p.name in CW_META_NAMES]
    assert offenders == [], f"CW meta leaked into the sidecar target: {offenders}"


def test_sidecar_roundtrip_matches_embedded(user_dir, tmp_path):
    # --- embedded: status quo — artifacts in <target>/docs, in-source annotations
    embedded = make_target(tmp_path / "embedded", annotated=True)
    write_epic(embedded / "docs" / "epics" / "order-app")
    _git(embedded, "add", ".")
    _git(embedded, "commit", "-q", "-m", "epic artifacts")
    embedded_result = run_gates(
        embedded, embedded / "docs" / "epics" / "order-app",
        tmp_path / "junit-embedded.xml", "docs/epics",
    )

    # --- sidecar: clean target, everything CW lives in the elected meta root
    sidecar_target = make_target(tmp_path / "sidecar", annotated=False)
    artifacts.elect(sidecar_target, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(sidecar_target)
    assert resolver.mode == "sidecar"
    assert str(resolver.meta_root).startswith(str(user_dir))
    epic_dir = write_epic(resolver.epic_dir("order-app"))
    store = resolver.quality_dir() / external_links.STORE_NAME
    resolver.quality_dir().mkdir(parents=True, exist_ok=True)
    assert_no_cw_meta(sidecar_target)

    # (c) precondition: WITHOUT the external store the same clean target fails
    # coverage — proving the store, not an accident, satisfies it below.
    bare = ct.check(epic_dir, sidecar_target)
    assert not bare.coverage_ok
    assert set(bare.uncovered_contracts) == set(IDS)

    # External symbol-anchored links replace the in-source annotations: same
    # verbs, same IDs, anchored to the same functions (Python ast tier).
    for file, symbol, verb in (
        ("app.py", "add_order", "guards"),
        ("tests/test_app.py", "test_add_order", "verifies"),
    ):
        entry, warning = external_links.add_link(
            store, sidecar_target, file, symbol, verb, IDS, use_lsp=False)
        assert warning is None, warning
        assert entry["symbol_hash"]  # anchored, not recorded-unresolved
        assert entry["target_sha"] == artifacts.head_sha(sidecar_target)  # version binding
    assert_no_cw_meta(sidecar_target)

    sidecar_result = run_gates(
        sidecar_target, epic_dir, tmp_path / "junit-sidecar.xml",
        str(resolver.epics_dir()), external_store=store,
        step_check=lambda: assert_no_cw_meta(sidecar_target),
    )

    # (b) identical high-level gate results across the two modes.
    assert embedded_result == sidecar_result

    # ...and they passed, rather than matching in some degraded state:
    assert sidecar_result["ratchet_score_rc"] == 0
    assert sidecar_result["ratchet_check_rc"] == 0
    assert sidecar_result["pass_set"] == ["py::tests.test_app::test_add_order"]
    assert sorted(sidecar_result["contract_hashes"]) == ["CTR-app-001", "CTR-app-002", "INV-app-003"]
    assert sidecar_result["trace"]["coverage_ok"] is True
    assert sidecar_result["trace"]["soundness_ok"] is True
    assert sidecar_result["trace"]["applicability"] == "applicable"
    assert sidecar_result["trace"]["uncovered"] == []
    assert sidecar_result["trace"]["untested"] == []
    assert sidecar_result["single_writer"]["applicability"] == "inapplicable"
    assert sidecar_result["status_ratchet"] == {
        "configured": True, "scorecard": True, "pass_set": 1, "contracts": 3,
    }

    # /status renders both without error and names the mode truthfully.
    assert "Footprint: embedded" in status.render_text(status.gather(embedded))
    sidecar_text = status.render_text(status.gather(sidecar_target))
    assert "Footprint: sidecar" in sidecar_text
    assert str(user_dir) in sidecar_text  # meta root points outside the target
    assert_no_cw_meta(sidecar_target)

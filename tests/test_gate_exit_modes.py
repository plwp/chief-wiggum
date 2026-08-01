"""IT-fh-09 — report-only vs --gate exit-mode semantics across the five #184
gates (docs/epics/epic-factory-hardening/integration-tests.md).

The exit-code contract every gate but the ratchet shares (docs/gate-rollout.md):

- report-only (no ``--gate``): findings are PRINTED but the exit code stays 0,
- ``--gate``: the SAME findings exit 1,
- usage errors (bad flags/paths): exit 2 — never conflated with findings.

The ratchet's pass-set/contract-hash check is the documented exception: it
predates the report-only doctrine and blocks by default (exit 1 on violations,
exit 4 fail-closed on journal tamper); its blocking authority is now backed by
its #184 validation record rather than a report-only rollout. Its row here
documents that difference instead of pretending uniformity.

Separately, ``check_gate_validation`` run report-only on a NOT-validated gate
exits 0 while its JSON envelope says ``passing == false`` — the reason
workflows must parse the envelope, never the exit code (CTR-fh-043's
silent-false-'validated' error case; INV-fh-003).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import quality_slop_gate as slop

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gate_validation"

_spec = importlib.util.spec_from_file_location(
    "saas_gate_server_exit_modes", FIXTURES / "saas_gate_clean" / "saas_gate_server.py")
_saas_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_saas_server)


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True)


def _copy_fixture(name: str, tmp_path: Path, dest: str) -> Path:
    out = tmp_path / dest
    shutil.copytree(FIXTURES / name, out, ignore=shutil.ignore_patterns("__pycache__"))
    return out


# --- check_architecture ------------------------------------------------------


def _arch_with_findings(tmp_path: Path) -> Path:
    corpus = _copy_fixture("check_architecture_clean", tmp_path, "arch")
    doc = json.loads((corpus / "architecture.json").read_text())
    edge = next(e for e in doc["edges"] if e["id"] == "EDG-gateway-analytics-001")
    edge["to"] = "ARC-ghost-999"  # a dangling endpoint — a KNOWN finding
    (corpus / "architecture.json").write_text(json.dumps(doc, indent=2))
    return corpus


def test_check_architecture_exit_mode_matrix(tmp_path):
    # @cw-trace verifies CTR-fh-042 INV-fh-003
    corpus = _arch_with_findings(tmp_path)
    arch = str(corpus / "architecture.json")
    script = str(SCRIPTS / "check_architecture.py")

    report_only = _run(script, arch)
    assert report_only.returncode == 0, report_only.stderr
    assert "dangling-endpoint" in report_only.stdout  # findings printed, not swallowed
    assert "does not prove the code matches the model" in report_only.stdout  # authority line

    gated = _run(script, arch, "--gate")
    assert gated.returncode == 1
    assert "does not prove the code matches the model" in gated.stdout

    usage = _run(script, arch, "--no-such-flag")
    assert usage.returncode == 2


# --- ci_scaffold -------------------------------------------------------------


def test_ci_scaffold_exit_mode_matrix(tmp_path):
    # @cw-trace verifies CTR-fh-042
    corpus = _copy_fixture("ci_scaffold_clean", tmp_path, "ci")
    (corpus / ".github" / "workflows" / "ci.yml").unlink()  # a KNOWN finding: CI missing
    script = str(SCRIPTS / "ci_scaffold.py")

    report_only = _run(script, "--repo", str(corpus), "--report")
    assert report_only.returncode == 0
    assert "MISSING" in report_only.stdout

    gated = _run(script, "--repo", str(corpus), "--gate")
    assert gated.returncode == 1

    usage = _run(script, "--repo", str(tmp_path / "does-not-exist"))
    assert usage.returncode == 2


# --- saas_gate ---------------------------------------------------------------


def test_saas_gate_exit_mode_matrix():
    # @cw-trace verifies CTR-fh-042
    script = str(SCRIPTS / "saas_gate.py")
    repo = str(FIXTURES / "saas_gate_clean" / "repo")
    with _saas_server.fixture_server("missing_headers") as base_url:
        report_only = _run(script, "--repo", repo, "--base-url", base_url)
        gated = _run(script, "--repo", repo, "--base-url", base_url, "--gate")
    assert report_only.returncode == 0
    assert json.loads(report_only.stdout)["ok"] is False  # findings present, exit still 0
    assert gated.returncode == 1

    usage = _run(script, "--no-such-flag")
    assert usage.returncode == 2


# --- quality_slop_gate -------------------------------------------------------


def test_quality_slop_gate_exit_mode_matrix(tmp_path, monkeypatch, capsys):
    """The findings channel is driven through the fixture band file (the same
    recorded target the gate's validation record pins) by patching the engine
    seams — the verdict/exit path exercised is the real ``run()``."""
    # @cw-trace verifies CTR-fh-042 CTR-fh-044
    band = json.loads(
        (FIXTURES / "quality_slop_gate_clean" / "survival_past_ai.json").read_text())
    monkeypatch.setattr(slop.survival, "analyze", lambda repo, workdir: band["survival_result"])
    monkeypatch.setattr(slop.duplication, "analyze", lambda repo, workdir: band["duplication_result"])
    monkeypatch.setattr(slop, "resolve_target", lambda owner_repo, repo: str(tmp_path))

    def args(gate: bool) -> Namespace:
        return Namespace(owner_repo=None, repo=str(tmp_path), workdir=str(tmp_path / "wd"),
                         gate=gate, report=not gate)

    assert slop.run(args(gate=False)) == 0  # report-only: findings printed, exit 0
    assert slop.run(args(gate=True)) == 1   # --gate: the same findings exit 1
    capsys.readouterr()  # drain the reports

    usage = _run(str(SCRIPTS / "quality_slop_gate.py"), "--no-such-flag")
    assert usage.returncode == 2


# --- ratchet (the documented exception: blocking by default) ------------------


def test_ratchet_exit_modes_are_blocking_by_default(tmp_path):
    """ratchet check predates the report-only doctrine: violations exit 1 with
    no report-only escape hatch, and usage errors exit 2. Documented here so
    the matrix states the real semantics instead of a false uniformity."""
    # @cw-trace verifies CTR-fh-042
    script = str(SCRIPTS / "ratchet.py")
    corpus = _copy_fixture("ratchet_clean", tmp_path, "ratchet")
    contracts = corpus / "docs" / "epics" / "gv-ratchet" / "contracts.md"
    contracts.write_text(contracts.read_text().replace(
        "no longer than 64 characters", "of any length whatsoever"))

    scored = _run(script, "score", "--repo", str(corpus), "--no-quality")
    assert scored.returncode == 0, scored.stderr
    violated = _run(script, "check", "--repo", str(corpus), "--format", "json")
    assert violated.returncode == 1
    assert json.loads(violated.stdout)["weakened_contracts"] == ["CTR-rt-001"]

    usage = _run(script, "no-such-subcommand")
    assert usage.returncode == 2


# --- check_gate_validation: the envelope, never the exit code -----------------


def test_gate_validation_report_only_exit_0_with_failing_envelope():
    """A NOT-validated gate run report-only exits 0 while the JSON envelope
    says passing == false — why workflows must parse the envelope, never the
    exit code (CTR-fh-043's silent-false-'validated' error case)."""
    # @cw-trace verifies CTR-fh-043 INV-fh-003
    script = str(SCRIPTS / "check_gate_validation.py")
    report_only = _run(script, "gate_with_no_record", "--format", "json")
    assert report_only.returncode == 0
    envelope = json.loads(report_only.stdout)
    assert envelope["passing"] is False
    assert envelope["record_found"] is False

    gated = _run(script, "gate_with_no_record", "--format", "json", "--gate")
    assert gated.returncode == 1

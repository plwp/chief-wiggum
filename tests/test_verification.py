"""Tests for the project verification runner (P1-9)."""

from __future__ import annotations

import json
from pathlib import Path

import run_verification
from chief_wiggum import verification as v


def _cmds(steps):
    return [" ".join(s.command) for s in steps]


# --- detection matrix -------------------------------------------------------


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    det = v.detect_project(tmp_path)
    assert det.has_go and not det.has_python and not det.has_node


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert v.detect_project(tmp_path).has_python is True


def test_detect_setup_py_counts_as_python(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert v.detect_project(tmp_path).has_python is True


def test_detect_node_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "build": "tsc"}}))
    det = v.detect_project(tmp_path)
    assert det.has_node and set(det.node_scripts) == {"test", "build"}


def test_detect_makefile_targets(tmp_path):
    (tmp_path / "Makefile").write_text("ci: test lint\n\ntest:\n\tpytest\n\nlint:\n\truff check\n")
    det = v.detect_project(tmp_path)
    assert det.has_makefile
    assert {"ci", "test", "lint"}.issubset(set(det.make_targets))


def test_detect_docker_and_playwright(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "playwright.config.ts").write_text("export default {}\n")
    det = v.detect_project(tmp_path)
    assert det.has_docker_compose and det.has_playwright


def test_malformed_package_json_does_not_crash(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    det = v.detect_project(tmp_path)
    assert det.has_node and det.node_scripts == ()


# --- command planning (no execution) ----------------------------------------


def test_plan_prefers_makefile_target(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    (tmp_path / "go.mod").write_text("module x\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test"], det)
    # Makefile target wins over go test.
    assert _cmds(steps) == ["make test"]


def test_plan_go_commands_per_profile(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test", "lint", "build"], det)
    assert _cmds(steps) == ["go test ./...", "go vet ./...", "go build ./..."]


def test_plan_node_gated_on_script_presence(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test", "build"], det)
    # Only 'test' script exists -> no build step planned.
    assert _cmds(steps) == ["npm test"]


def test_plan_smoke_profile(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "playwright.config.ts").write_text("export default {}\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["smoke"], det)
    assert _cmds(steps) == ["docker compose up -d --wait", "npx --no-install playwright test"]


def test_plan_empty_when_nothing_detected(tmp_path):
    det = v.detect_project(tmp_path)
    assert v.plan_steps(tmp_path, list(v.PROFILES), det) == []


def test_empty_plan_is_not_ok(tmp_path):
    # Nothing detected -> nothing verified -> must NOT green-light a ship.
    report = v.verify(tmp_path, ["test"])
    assert report.steps == []
    assert report.ok is False
    assert report.warnings


def test_grouped_and_assignment_makefile_lines(tmp_path):
    (tmp_path / "Makefile").write_text("FLAGS := -x\n\ntest lint:\n\techo hi\n")
    det = v.detect_project(tmp_path)
    assert "test" in det.make_targets and "lint" in det.make_targets
    assert "FLAGS" not in det.make_targets


def test_lowercase_makefile_name_detected(tmp_path):
    (tmp_path / "makefile").write_text("build:\n\tgo build\n")
    det = v.detect_project(tmp_path)
    assert det.has_makefile and "build" in det.make_targets


def test_duplicate_profiles_run_once(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    runs = []
    report = v.verify(tmp_path, ["test", "test"], runner=lambda c, w: runs.append(c) or (0, "ok"))
    assert len(report.steps) == 1


def test_log_tail_zero_returns_empty(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    report = v.verify(tmp_path, ["test"], runner=lambda c, w: (1, "noisy output"), log_tail_lines=0)
    assert report.steps[0].log_tail == ""


# --- execution with injected runner -----------------------------------------


def test_verify_dry_run_does_not_execute(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    calls = []

    def runner(cmd, cwd):
        calls.append(cmd)
        return 0, ""

    report = v.verify(tmp_path, ["test"], dry_run=True, runner=runner)
    assert calls == []
    assert report.steps[0].planned_only is True
    assert report.ok is True


def test_verify_runs_and_collects_evidence(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    ticks = iter([10.0, 12.5])

    def runner(cmd, cwd):
        return 0, "PASS\nall good"

    report = v.verify(tmp_path, ["test"], runner=runner, clock=lambda: next(ticks))
    step = report.steps[0]
    assert step.ok is True
    assert step.exit_code == 0
    assert step.duration_s == 2.5
    assert "all good" in step.log_tail


def test_verify_failure_captures_log_tail_and_not_ok(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")

    def runner(cmd, cwd):
        return 1, "\n".join(f"line {i}" for i in range(100))

    report = v.verify(tmp_path, ["test"], runner=runner, log_tail_lines=10)
    step = report.steps[0]
    assert step.ok is False
    assert report.ok is False
    assert len(step.log_tail.splitlines()) == 10
    assert "line 99" in step.log_tail


def test_verify_runner_error_is_captured(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")

    def runner(cmd, cwd):
        raise FileNotFoundError("go not installed")

    report = v.verify(tmp_path, ["test"], runner=runner)
    assert report.ok is False
    assert "runner error" in report.steps[0].log_tail


# --- serialization / CLI ----------------------------------------------------


def test_report_json_and_markdown(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    report = v.verify(tmp_path, ["test"], dry_run=True)
    json.loads(json.dumps(report.to_dict()))
    md = report.render_markdown()
    assert "# Verification Report" in md
    assert "go test ./..." in md


def test_cli_dry_run_json(tmp_path, capsys):
    (tmp_path / "go.mod").write_text("module x\n")
    rc = run_verification.main(["--repo", str(tmp_path), "--profile", "test,build", "--dry-run"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert {s["tool"] for s in data["steps"]} == {"go"}


def test_cli_rejects_unknown_profile(tmp_path, capsys):
    rc = run_verification.main(["--repo", str(tmp_path), "--profile", "bogus"])
    assert rc == 2
    assert "unknown profile" in capsys.readouterr().err


# --- #284: junit report emission so `ratchet score` can reuse this run ------
#
# /implement Step 8 runs the full test suite via run_verification.py, then
# Step 8b's `ratchet.py score` re-runs the SAME suite from scratch to hash the
# pass-set. For a pytest-based repo this pays for the suite twice. The fix:
# a "test"-profile step that's plausibly pytest (tool == "python", or a
# Makefile target on a repo that also has_python) gets a known junit-xml
# report path attached, and `verify()` sets PYTEST_ADDOPTS so pytest writes
# it — transparently, whether invoked directly or via `make test` — so
# `ratchet score --reuse-report` (see test_ratchet.py) has something to read.


def test_plan_test_step_records_report_for_python_tool(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test"], det)
    assert len(steps) == 1 and steps[0].tool == "python"
    assert steps[0].report == v.PYTEST_JUNIT_REPORT


def test_plan_test_step_records_report_for_makefile_python(tmp_path):
    """A Makefile 'test' target wins over the python step (existing
    precedence), but on a repo that also has_python it is presumptively
    pytest underneath (chief-wiggum's own `make test` is a bare pytest) — the
    report path is still attached so PYTEST_ADDOPTS has somewhere to write."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test"], det)
    assert len(steps) == 1 and steps[0].tool == "make"
    assert steps[0].report == v.PYTEST_JUNIT_REPORT


def test_plan_test_step_no_report_for_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test"], det)
    assert steps[0].report is None


def test_plan_non_test_profile_never_gets_a_report(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["lint", "build"], det)
    assert all(s.report is None for s in steps)


def test_verify_sets_pytest_addopts_env_and_clears_stale_report(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    report_path = tmp_path / v.PYTEST_JUNIT_REPORT
    report_path.write_text("STALE — from an earlier, unrelated run\n")

    seen = {}

    def runner(cmd, cwd, env=None):
        # The stale report must be gone BEFORE this runs (never survive as a
        # false pass-set if this runner "fails" to rewrite it).
        seen["report_existed_at_call"] = Path(cwd, v.PYTEST_JUNIT_REPORT).exists()
        seen["env"] = env
        return 0, "ok"

    report = v.verify(tmp_path, ["test"], runner=runner)
    assert seen["report_existed_at_call"] is False
    assert seen["env"] is not None
    addopts = seen["env"].get("PYTEST_ADDOPTS", "")
    assert f"--junit-xml={tmp_path / v.PYTEST_JUNIT_REPORT}" in addopts
    assert report.steps[0].report == v.PYTEST_JUNIT_REPORT


def test_verify_falls_back_when_runner_lacks_env_kwarg(tmp_path):
    """A pre-existing 2-arg runner (every test above, and every real caller
    before #284) must keep working unchanged — env plumbing is additive."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    def runner(cmd, cwd):
        return 0, "ok"

    report = v.verify(tmp_path, ["test"], runner=runner)
    assert report.ok is True
    assert report.steps[0].report == v.PYTEST_JUNIT_REPORT

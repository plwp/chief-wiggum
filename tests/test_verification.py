"""Tests for the project verification runner (P1-9)."""

from __future__ import annotations

import json

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
    assert _cmds(steps) == ["docker compose up -d", "npx playwright test"]


def test_plan_empty_when_nothing_detected(tmp_path):
    det = v.detect_project(tmp_path)
    assert v.plan_steps(tmp_path, list(v.PROFILES), det) == []


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

"""Tests for scripts/ci_scaffold.py — detect + scaffold a minimal CI workflow."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ci_scaffold  # noqa: E402

yaml = pytest.importorskip("yaml")


# --- detect_ci ------------------------------------------------------------


def test_detect_ci_absent_on_empty_repo(tmp_path):
    present, workflows = ci_scaffold.detect_ci(tmp_path)
    assert present is False
    assert workflows == []


def test_detect_ci_present_with_yml(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("name: ci\n")
    present, workflows = ci_scaffold.detect_ci(tmp_path)
    assert present is True
    assert workflows == [".github/workflows/ci.yml"]


def test_detect_ci_present_with_yaml_extension(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "build.yaml").write_text("name: build\n")
    (wf / "release.yml").write_text("name: release\n")
    present, workflows = ci_scaffold.detect_ci(tmp_path)
    assert present is True
    assert sorted(workflows) == [
        ".github/workflows/build.yaml",
        ".github/workflows/release.yml",
    ]


def test_detect_ci_ignores_non_yaml_files(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "README.md").write_text("# workflows\n")
    present, workflows = ci_scaffold.detect_ci(tmp_path)
    assert present is False
    assert workflows == []


# --- detect_stack ---------------------------------------------------------


def test_detect_stack_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    assert ci_scaffold.detect_stack(tmp_path) == ["go"]


def test_detect_stack_python_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
    assert ci_scaffold.detect_stack(tmp_path) == ["python"]


def test_detect_stack_python_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    assert ci_scaffold.detect_stack(tmp_path) == ["python"]


def test_detect_stack_python_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert ci_scaffold.detect_stack(tmp_path) == ["python"]


def test_detect_stack_node(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    assert ci_scaffold.detect_stack(tmp_path) == ["node"]


def test_detect_stack_multiple(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
    assert set(ci_scaffold.detect_stack(tmp_path)) == {"go", "node", "python"}


def test_detect_stack_empty(tmp_path):
    assert ci_scaffold.detect_stack(tmp_path) == []


# --- scaffold_ci ----------------------------------------------------------


def _load_ci(tmp_path):
    path = tmp_path / ".github" / "workflows" / "ci.yml"
    assert path.is_file(), "scaffold_ci must write .github/workflows/ci.yml"
    return path, yaml.safe_load(path.read_text())


def test_scaffold_ci_writes_parseable_yaml(tmp_path):
    written = ci_scaffold.scaffold_ci(tmp_path, ["go"])
    path, doc = _load_ci(tmp_path)
    assert str(path) in [str(p) for p in written]
    assert isinstance(doc, dict)
    # a workflow must have jobs and a trigger
    assert "jobs" in doc
    assert doc.get("on") or doc.get(True)  # PyYAML parses bare `on:` as True key


def _all_run_steps(doc):
    runs = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            if isinstance(step, dict) and "run" in step:
                runs.append(step["run"])
    return "\n".join(runs)


def test_scaffold_go_has_build_test_vet(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["go"])
    _, doc = _load_ci(tmp_path)
    runs = _all_run_steps(doc)
    assert "go build ./..." in runs
    assert "go test ./..." in runs
    assert "go vet" in runs


def test_scaffold_python_has_install_test_lint(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["python"])
    _, doc = _load_ci(tmp_path)
    runs = _all_run_steps(doc)
    # dependency-install sanity step
    assert "pip install" in runs
    assert "pytest" in runs
    assert "ruff" in runs


def test_scaffold_node_has_ci_install_test(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["node"])
    _, doc = _load_ci(tmp_path)
    runs = _all_run_steps(doc)
    assert "npm ci" in runs
    assert "npm test" in runs


def test_scaffold_multi_stack_covers_each(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["go", "python", "node"])
    _, doc = _load_ci(tmp_path)
    runs = _all_run_steps(doc)
    assert "go test ./..." in runs
    assert "pytest" in runs
    assert "npm ci" in runs


def test_scaffold_is_idempotent_without_force(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["go"])
    first = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    # a second call must NOT overwrite an existing workflow
    written = ci_scaffold.scaffold_ci(tmp_path, ["python"])
    second = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    assert first == second
    assert written == []


def test_scaffold_force_overwrites(tmp_path):
    ci_scaffold.scaffold_ci(tmp_path, ["go"])
    written = ci_scaffold.scaffold_ci(tmp_path, ["python"], force=True)
    assert written
    _, doc = _load_ci(tmp_path)
    runs = _all_run_steps(doc)
    assert "pytest" in runs


def test_scaffold_unknown_stack_still_valid(tmp_path):
    # no recognized stack: still produce a valid, parseable workflow skeleton
    ci_scaffold.scaffold_ci(tmp_path, [])
    _, doc = _load_ci(tmp_path)
    assert "jobs" in doc


# --- CLI ------------------------------------------------------------------


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "ci_scaffold.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_report_missing_exits_zero(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    result = _run("--repo", str(tmp_path), "--report")
    assert result.returncode == 0, result.stderr
    assert "go" in result.stdout.lower()
    assert "missing" in result.stdout.lower() or "no ci" in result.stdout.lower()


def test_cli_report_is_default(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    result = _run("--repo", str(tmp_path))
    assert result.returncode == 0, result.stderr


def test_cli_gate_missing_exits_nonzero(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    result = _run("--repo", str(tmp_path), "--gate")
    assert result.returncode != 0


def test_cli_gate_present_exits_zero(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("name: ci\n")
    result = _run("--repo", str(tmp_path), "--gate")
    assert result.returncode == 0, result.stderr


def test_cli_scaffold_writes_file(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    result = _run("--repo", str(tmp_path), "--scaffold")
    assert result.returncode == 0, result.stderr
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    assert ci.is_file()
    assert yaml.safe_load(ci.read_text())


def test_cli_scaffold_idempotent(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    _run("--repo", str(tmp_path), "--scaffold")
    before = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    _run("--repo", str(tmp_path), "--scaffold")
    after = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    assert before == after


def test_cli_json_report(tmp_path):
    import json
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    result = _run("--repo", str(tmp_path), "--report", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ci_present"] is False
    assert data["stack"] == ["node"]

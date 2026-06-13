from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "claude-interactive-delegate" / "scripts" / "claude_delegate.py"


def load_module():
    spec = importlib.util.spec_from_file_location("claude_delegate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_task_dir_creates_unique_task_directory(tmp_path):
    delegate = load_module()

    paths = delegate.create_task(tmp_path, "task-123")

    assert paths.task_id == "task-123"
    assert paths.task_dir == tmp_path / "task-123"
    assert paths.prompt == tmp_path / "task-123" / "prompt.md"
    assert paths.result == tmp_path / "task-123" / "result.md"
    assert paths.done == tmp_path / "task-123" / "DONE"
    assert paths.error == tmp_path / "task-123" / "ERROR"
    assert paths.task_dir.is_dir()
    with pytest.raises(FileExistsError):
        delegate.create_task(tmp_path, "task-123")


def test_build_delegate_message_uses_file_handoff_contract(tmp_path):
    delegate = load_module()
    paths = delegate.create_task(tmp_path, "task-123")
    cwd = tmp_path / "repo"
    cwd.mkdir()

    message = delegate.build_delegate_message(paths, str(cwd))

    assert str(paths.prompt) in message
    assert str(paths.result) in message
    assert f"touch {paths.done}" in message
    assert str(paths.error) in message
    assert str(cwd.resolve()) in message
    assert "Do not answer login, billing, subscription, payment, or API-credit consent prompts" in message


def test_script_bootstraps_shared_protocol_without_pytest_pythonpath(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = ""

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Drive an interactive Claude Code session" in result.stdout

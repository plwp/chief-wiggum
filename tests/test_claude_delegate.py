from __future__ import annotations

import importlib.util
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

    task_id, task_dir = delegate.make_task_dir(tmp_path, "task-123")

    assert task_id == "task-123"
    assert task_dir == tmp_path / "task-123"
    assert task_dir.is_dir()
    with pytest.raises(FileExistsError):
        delegate.make_task_dir(tmp_path, "task-123")


def test_build_delegate_message_uses_file_handoff_contract(tmp_path):
    delegate = load_module()
    task_dir = tmp_path / "task-123"
    task_dir.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()

    message = delegate.build_delegate_message(task_dir, str(cwd))

    assert str(task_dir / "prompt.md") in message
    assert str(task_dir / "result.md") in message
    assert f"touch {task_dir / 'DONE'}" in message
    assert str(task_dir / "ERROR") in message
    assert str(cwd.resolve()) in message
    assert "Do not answer login, billing, subscription, payment, or API-credit consent prompts" in message

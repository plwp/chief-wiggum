from __future__ import annotations

import pytest
from delegates import task_protocol


def test_create_task_writes_prompt_and_paths(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1", "Review this diff.")

    assert paths.task_id == "worker-1"
    assert paths.task_dir == tmp_path / "worker-1"
    assert paths.prompt.read_text() == "Review this diff."
    assert paths.result == paths.task_dir / "result.md"
    assert paths.done == paths.task_dir / "DONE"
    assert paths.error == paths.task_dir / "ERROR"
    assert paths.log == paths.task_dir / "worker.log"
    assert paths.metadata == paths.task_dir / "metadata.json"


def test_wait_for_completion_detects_done(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    paths.done.touch()

    assert task_protocol.wait_for_completion(paths, timeout_seconds=1, poll_seconds=0.01) == "done"


def test_wait_for_completion_detects_error(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    paths.error.write_text("blocked")

    assert task_protocol.wait_for_completion(paths, timeout_seconds=1, poll_seconds=0.01) == "error"


def test_wait_for_completion_times_out(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")

    with pytest.raises(TimeoutError):
        task_protocol.wait_for_completion(paths, timeout_seconds=0, poll_seconds=0.01)

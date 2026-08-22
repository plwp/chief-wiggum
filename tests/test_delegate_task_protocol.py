from __future__ import annotations

import json
import threading

import pytest
from delegates import task_protocol


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status", "result_sha256", "log_sha256"],
    "properties": {
        "status": {"const": "done"},
        "result_sha256": {"type": "string"},
        "log_sha256": {"type": "string"},
    },
    "additionalProperties": False,
}


def test_publish_success_hashes_artifacts_and_commits_done_last(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1", "do work")
    metadata = {
        "status": "done",
        "result_sha256": task_protocol.sha256_digest(b"answer\n"),
        "log_sha256": task_protocol.sha256_digest(b'{"event":"ok"}\n'),
    }

    task_protocol.publish_success(
        paths,
        result="answer\n",
        log=b'{"event":"ok"}\n',
        metadata=metadata,
        schema=SCHEMA,
    )

    assert task_protocol.completion_status(paths).state == "done"
    assert json.loads(paths.metadata.read_text()) == metadata
    assert paths.done.read_text().strip() == "done"
    assert not paths.error.exists()
    assert paths.result.stat().st_mode & 0o777 == 0o600


def test_invalid_metadata_never_publishes_done(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")

    with pytest.raises(task_protocol.MetadataValidationError):
        task_protocol.publish_success(
            paths, result="answer", log=b"{}\n", metadata={}, schema=SCHEMA
        )

    assert task_protocol.completion_status(paths).state == "pending"


def test_error_is_terminal_and_success_cannot_overwrite_it(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    task_protocol.publish_error(paths, reason="HARNESS_EXIT_NONZERO", detail="exit 3")

    with pytest.raises(task_protocol.TerminalStateError):
        task_protocol.publish_success(
            paths,
            result="answer",
            log=b"{}\n",
            metadata={
                "status": "done",
                "result_sha256": task_protocol.sha256_digest(b"answer"),
                "log_sha256": task_protocol.sha256_digest(b"{}\n"),
            },
            schema=SCHEMA,
        )

    assert task_protocol.completion_status(paths).state == "error"
    assert not paths.done.exists()


def test_both_sentinels_are_conflict_never_success(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    paths.done.touch()
    paths.error.touch()

    assert task_protocol.completion_status(paths).state == "conflict"
    with pytest.raises(task_protocol.ProtocolConflictError):
        task_protocol.wait_for_completion(paths, timeout_seconds=1, poll_seconds=0.01)


def test_concurrent_publishers_create_exactly_one_terminal_state(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    barrier = threading.Barrier(2)
    outcomes = []

    def success():
        barrier.wait()
        try:
            task_protocol.publish_success(
                paths,
                result="answer",
                log=b"{}\n",
                metadata={
                    "status": "done",
                    "result_sha256": task_protocol.sha256_digest(b"answer"),
                    "log_sha256": task_protocol.sha256_digest(b"{}\n"),
                },
                schema=SCHEMA,
            )
            outcomes.append("success")
        except task_protocol.TerminalStateError:
            outcomes.append("lost")

    def error():
        barrier.wait()
        try:
            task_protocol.publish_error(paths, reason="WORKER_TIMEOUT")
            outcomes.append("error")
        except task_protocol.TerminalStateError:
            outcomes.append("lost")

    threads = [threading.Thread(target=success), threading.Thread(target=error)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("lost") == 1
    assert task_protocol.completion_status(paths).state in {"done", "error"}
    assert not (paths.done.exists() and paths.error.exists())


def test_symlinked_artifact_path_is_rejected(tmp_path):
    paths = task_protocol.create_task(tmp_path, "worker-1")
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    paths.result.symlink_to(outside)

    with pytest.raises(task_protocol.UnsafeTaskPathError):
        task_protocol.publish_success(
            paths,
            result="answer",
            log=b"{}\n",
            metadata={
                "status": "done",
                "result_sha256": task_protocol.sha256_digest(b"answer"),
                "log_sha256": task_protocol.sha256_digest(b"{}\n"),
            },
            schema=SCHEMA,
        )

    assert outside.read_text() == "untouched"

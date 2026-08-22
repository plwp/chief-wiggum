#!/usr/bin/env python3
"""Shared file-based task protocol for delegated workers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

DEFAULT_TASK_ROOT = Path.home() / ".chief-wiggum" / "delegates"


class TaskProtocolError(RuntimeError):
    """Base error for durable delegate task publication."""


class TerminalStateError(TaskProtocolError):
    """A terminal result already exists and cannot be replaced."""


class ProtocolConflictError(TerminalStateError):
    """Both mutually-exclusive terminal sentinels exist."""


class MetadataValidationError(TaskProtocolError):
    """Delegate metadata does not conform to its declared schema."""


class UnsafeTaskPathError(TaskProtocolError):
    """A protocol artifact is symlinked or escapes its task directory."""


@dataclass(frozen=True)
class TerminalStatus:
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class TaskPaths:
    """Paths for a delegated worker task directory."""

    task_id: str
    task_dir: Path
    prompt: Path
    result: Path
    done: Path
    error: Path
    log: Path
    metadata: Path


def new_task_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def task_paths(task_root: Path, task_id: str) -> TaskPaths:
    if not task_id or task_id in {".", ".."} or Path(task_id).name != task_id:
        raise UnsafeTaskPathError(f"invalid task id: {task_id!r}")
    task_dir = task_root.expanduser().resolve() / task_id
    return TaskPaths(
        task_id=task_id,
        task_dir=task_dir,
        prompt=task_dir / "prompt.md",
        result=task_dir / "result.md",
        done=task_dir / "DONE",
        error=task_dir / "ERROR",
        log=task_dir / "worker.log",
        metadata=task_dir / "metadata.json",
    )


def create_task(
    task_root: Path, task_id: str | None = None, prompt: str | None = None
) -> TaskPaths:
    paths = task_paths(task_root, task_id or new_task_id())
    paths.task_dir.mkdir(parents=True, exist_ok=False)
    if prompt is not None:
        atomic_write(paths.prompt, prompt.encode())
    return paths


def sha256_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def file_sha256(path: Path) -> str:
    return sha256_digest(path.read_bytes())


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write bytes durably using a same-directory temporary replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _artifact_paths(paths: TaskPaths) -> tuple[Path, ...]:
    return (paths.prompt, paths.result, paths.done, paths.error, paths.log, paths.metadata)


def _validate_paths(paths: TaskPaths) -> None:
    task_dir = paths.task_dir.resolve()
    if paths.task_dir.is_symlink() or not paths.task_dir.is_dir():
        raise UnsafeTaskPathError(f"unsafe task directory: {paths.task_dir}")
    for path in _artifact_paths(paths):
        if path.is_symlink():
            raise UnsafeTaskPathError(f"symlinked task artifact: {path.name}")
        if path.parent.resolve() != task_dir:
            raise UnsafeTaskPathError(f"task artifact escapes directory: {path.name}")


def completion_status(paths: TaskPaths) -> TerminalStatus:
    has_done = paths.done.exists()
    has_error = paths.error.exists()
    if has_done and has_error:
        return TerminalStatus("conflict", "both DONE and ERROR exist")
    if has_done:
        return TerminalStatus("done")
    if has_error:
        reason = None
        with contextlib.suppress(OSError, json.JSONDecodeError, TypeError):
            reason = json.loads(paths.error.read_text()).get("reason")
        return TerminalStatus("error", reason)
    return TerminalStatus("pending")


@contextlib.contextmanager
def _terminal_lock(paths: TaskPaths):
    _validate_paths(paths)
    lock_path = paths.task_dir / ".terminal.lock"
    if lock_path.is_symlink():
        raise UnsafeTaskPathError("symlinked terminal lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise UnsafeTaskPathError("unsafe terminal lock") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        status = completion_status(paths)
        if status.state == "conflict":
            raise ProtocolConflictError(status.reason or "terminal conflict")
        if status.state != "pending":
            raise TerminalStateError(f"task is already terminal: {status.state}")
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def publish_success(
    paths: TaskPaths,
    *,
    result: str,
    log: bytes,
    metadata: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(metadata),
        key=lambda e: list(e.path),
    )
    if errors:
        raise MetadataValidationError(errors[0].message)
    result_bytes = result.encode()
    if metadata.get("result_sha256") != sha256_digest(result_bytes):
        raise MetadataValidationError("result_sha256 does not match result bytes")
    if metadata.get("log_sha256") != sha256_digest(log):
        raise MetadataValidationError("log_sha256 does not match log bytes")
    with _terminal_lock(paths):
        atomic_write(paths.result, result_bytes)
        atomic_write(paths.log, log)
        atomic_write(
            paths.metadata, (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
        )
        atomic_write(paths.done, b"done\n")


def publish_error(
    paths: TaskPaths,
    *,
    reason: str,
    detail: str | None = None,
    log: bytes | None = None,
    metadata: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> None:
    record = {"reason": reason}
    if detail:
        record["detail"] = detail[:500]
    if metadata is not None and schema is not None:
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(metadata),
            key=lambda error: list(error.path),
        )
        if errors:
            raise MetadataValidationError(errors[0].message)
        if metadata.get("status") != "error" or metadata.get("reason_code") != reason:
            raise MetadataValidationError("error metadata status/reason does not match sentinel")
        if log is not None and metadata.get("log_sha256") != sha256_digest(log):
            raise MetadataValidationError("log_sha256 does not match log bytes")
    with _terminal_lock(paths):
        if log is not None:
            atomic_write(paths.log, log)
        if metadata is not None:
            atomic_write(
                paths.metadata,
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(),
            )
        atomic_write(paths.error, (json.dumps(record, sort_keys=True) + "\n").encode())


def wait_for_completion(paths: TaskPaths, timeout_seconds: int, poll_seconds: float = 2.0) -> str:
    """Wait for DONE or ERROR and return 'done' or 'error'.

    Raises TimeoutError if neither sentinel appears before the timeout.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = completion_status(paths)
        if status.state == "conflict":
            raise ProtocolConflictError(status.reason or "terminal conflict")
        if status.state in {"done", "error"}:
            return status.state
        time.sleep(poll_seconds)
    raise TimeoutError(f"no DONE or ERROR after {timeout_seconds}s: {paths.task_dir}")

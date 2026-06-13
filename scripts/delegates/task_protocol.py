#!/usr/bin/env python3
"""Shared file-based task protocol for delegated workers."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TASK_ROOT = Path.home() / ".chief-wiggum" / "delegates"


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


def create_task(task_root: Path, task_id: str | None = None, prompt: str | None = None) -> TaskPaths:
    paths = task_paths(task_root, task_id or new_task_id())
    paths.task_dir.mkdir(parents=True, exist_ok=False)
    if prompt is not None:
        paths.prompt.write_text(prompt)
    return paths


def wait_for_completion(paths: TaskPaths, timeout_seconds: int, poll_seconds: float = 2.0) -> str:
    """Wait for DONE or ERROR and return 'done' or 'error'.

    Raises TimeoutError if neither sentinel appears before the timeout.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if paths.done.exists():
            return "done"
        if paths.error.exists():
            return "error"
        time.sleep(poll_seconds)
    raise TimeoutError(f"no DONE or ERROR after {timeout_seconds}s: {paths.task_dir}")

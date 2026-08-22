#!/usr/bin/env python3
"""Agentic OpenRouter coding worker behind the durable delegate protocol.

This module is deliberately separate from ``consult_ai``. Consultation is a
prompt-only completion; this adapter gives an open-source harness repository
tools inside an already-isolated worktree.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chief_wiggum import gitops  # noqa: E402
from keychain import get_secret  # noqa: E402
from providers import ExecutionProvider, execution_providers_from_config, load_config  # noqa: E402

from delegates import task_protocol  # noqa: E402

ADAPTER_VERSION = 1
REDACTION_POLICY_VERSION = 1
SCHEMA = Path(__file__).resolve().parents[2] / "templates" / "delegate-worker-metadata-schema.json"
REQUIRED_CAPABILITIES = {"responses", "repo-read", "shell-tools", "workspace-write"}


@dataclass(frozen=True)
class WorkerResult:
    status: str
    reason_code: str | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()


def _sanitize_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ}
    for name, value in (extra or {}).items():
        upper = name.upper()
        if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "CREDENTIAL")):
            continue
        allowed[name] = value
    return allowed


def _harness_version(command: str, environment: dict[str, str]) -> str | None:
    completed = subprocess.run(
        [command, "--version"],
        text=True,
        capture_output=True,
        timeout=5,
        env=environment,
    )
    if completed.returncode != 0:
        return None
    version = completed.stdout.strip().splitlines()
    return version[0][:100] if version else None


class _OneShotBroker:
    """Single-use owner-only Unix socket; it has no keychain capability."""

    def __init__(self, directory: Path, secret: str, timeout_seconds: int):
        self.socket_path = directory / "auth.sock"
        self.state_path = directory / ".auth-state"
        self.capability = secrets.token_urlsafe(32)
        self._secret = secret
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._server.listen(1)
        self._server.settimeout(timeout_seconds)
        self.consumed = False
        task_protocol.atomic_write(
            self.state_path,
            json.dumps({"socket": str(self.socket_path), "capability": self.capability}).encode(),
        )
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self._server.accept()
            with connection:
                request = connection.recv(4096).decode(errors="replace")
                if secrets.compare_digest(request, self.capability):
                    connection.sendall(self._secret.encode())
                    self.consumed = True
        except (OSError, TimeoutError):
            pass
        finally:
            self._secret = ""
            self._server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass

    def join(self) -> None:
        self._thread.join(timeout=2)

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass
        self.join()


_AUTH_HELPER = """#!/usr/bin/env python3
import json, pathlib, socket, sys
try:
    state = json.loads(pathlib.Path(__file__).with_name(".auth-state").read_text())
except (OSError, ValueError):
    raise SystemExit(2)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.settimeout(2)
    sock.connect(state["socket"])
    sock.sendall(state["capability"].encode())
    value = sock.recv(1024)
    if not value:
        raise SystemExit(3)
    sys.stdout.buffer.write(value)
except OSError:
    raise SystemExit(4)
finally:
    sock.close()
"""


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _event_log(stdout: bytes, stderr: bytes, *, secret: str) -> tuple[bytes, list[dict], bool]:
    events: list[dict] = []
    records: list[str] = []
    malformed = False
    sequence = 0
    for stream, payload in (("stdout", stdout), ("stderr", stderr)):
        for raw in payload.decode(errors="replace").splitlines():
            sequence += 1
            exposure = bool(secret and secret in raw)
            sanitized = raw.replace(secret, "[REDACTED]") if secret else raw
            sanitized = sanitized.replace(str(Path.home()), "$HOME")
            record: dict = {"seq": sequence, "stream": stream, "observed_at": _utc_now()}
            if stream == "stdout":
                try:
                    parsed = json.loads(sanitized)
                    if not isinstance(parsed, dict):
                        raise ValueError("event is not an object")
                    record["parsed_event"] = parsed
                    events.append(parsed)
                except (json.JSONDecodeError, ValueError):
                    malformed = True
                    record["raw_if_unparsed"] = sanitized
            else:
                record["raw_if_unparsed"] = sanitized
            record["secret_exposure"] = exposure
            records.append(json.dumps(record, sort_keys=True))
    return (("\n".join(records) + ("\n" if records else "")).encode(), events, malformed)


def _resolved_model(events: list[dict]) -> str | None:
    for event in events:
        for key in ("resolved_model", "model"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _usage(events: list[dict], limit: int | None) -> dict:
    raw = next(
        (event.get("usage") for event in reversed(events) if isinstance(event.get("usage"), dict)),
        None,
    )
    if raw is None:
        return {
            "status": "unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "within_limit": None,
        }
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    reasoning = raw.get("reasoning_output_tokens")
    measured = isinstance(input_tokens, int) and isinstance(output_tokens, int)
    if not measured:
        input_tokens = None
        output_tokens = None
    return {
        "status": "measured" if measured else "unavailable",
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
        "reasoning_output_tokens": reasoning if isinstance(reasoning, int) else None,
        "within_limit": (input_tokens <= limit)
        if isinstance(input_tokens, int) and limit
        else None,
    }


def _publish_failure(
    paths: task_protocol.TaskPaths, reason: str, detail: str | None = None, log: bytes | None = None
) -> WorkerResult:
    # @cw-trace ensures CTR-dag-021
    task_protocol.publish_error(paths, reason=reason, detail=detail, log=log)
    return WorkerResult("error", reason)


def _publish_failure_with_metadata(
    paths: task_protocol.TaskPaths,
    reason: str,
    metadata: dict,
    log: bytes,
    detail: str | None = None,
) -> WorkerResult:
    failure_metadata = {**metadata, "status": "error", "reason_code": reason}
    try:
        task_protocol.publish_error(
            paths,
            reason=reason,
            detail=detail,
            log=log,
            metadata=failure_metadata,
            schema=json.loads(SCHEMA.read_text()),
        )
    except task_protocol.MetadataValidationError:
        return _publish_failure(paths, "METADATA_INVALID")
    return WorkerResult("error", reason)


def _execution_metadata(
    *,
    paths: task_protocol.TaskPaths,
    provider: ExecutionProvider,
    harness_command: str,
    harness_version: str | None,
    sandbox: str,
    start_sha: str,
    end_sha: str,
    started_at: str,
    started: float,
    exit_status: int,
    prompt: str,
    result: str | None,
    log: bytes,
    events: list[dict],
    tool_behavior: str,
) -> dict:
    resolved = _resolved_model(events)
    provider_public = {
        "name": provider.name,
        "adapter": provider.execution_adapter,
        "model": provider.model,
        "base_url": provider.base_url,
        "tier": provider.capability_tier,
        "capabilities": provider.capabilities,
        "max_input_tokens": provider.max_input_tokens,
    }
    return {
        "schema_version": 1,
        "task_id": paths.task_id,
        "status": "done",
        "reason_code": None,
        "harness": {"name": Path(harness_command).name, "version": harness_version},
        "execution_adapter": provider.execution_adapter,
        "provider_name": provider.name,
        "provider_capability_tier": provider.capability_tier,
        "requested_model": provider.model,
        "resolved_model": resolved,
        "model_resolution_status": "observed" if resolved else "unavailable",
        "sandbox": sandbox,
        "tool_behavior": tool_behavior,
        "worktree_start_sha": start_sha,
        "worktree_end_sha": end_sha,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "exit_status": exit_status,
        "usage": _usage(events, provider.max_input_tokens),
        "prompt_sha256": task_protocol.sha256_digest(prompt.encode()),
        "provider_config_sha256": task_protocol.sha256_digest(
            json.dumps(provider_public, sort_keys=True).encode()
        ),
        "result_sha256": task_protocol.sha256_digest(result.encode()) if result else None,
        "log_sha256": task_protocol.sha256_digest(log),
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "adapter_version": ADAPTER_VERSION,
    }


def run_task(
    *,
    paths: task_protocol.TaskPaths,
    provider: ExecutionProvider,
    worktree: Path,
    main_checkout: Path,
    timeout_seconds: int,
    harness_command: str = "codex",
    secret_loader: Callable[[], str | None] = lambda: get_secret("OPENROUTER_API_KEY"),
    extra_env: dict[str, str] | None = None,
    sandbox: str = "workspace-write",
    admit_disabled: bool = False,
) -> WorkerResult:
    """Run one configured coding delegate and publish one terminal result."""
    try:
        # @cw-trace guards CTR-dag-020
        worktree = gitops.assert_worktree(worktree, main_checkout)
    except (gitops.GitSafetyError, subprocess.SubprocessError):
        return _publish_failure(paths, "UNSAFE_WORKTREE")
    if not provider.enabled and not admit_disabled:
        return _publish_failure(paths, "PROVIDER_DISABLED")
    if (
        provider.execution_adapter != "codex-responses"
        or not provider.model
        or not provider.base_url
        or provider.max_input_tokens is None
    ):
        return _publish_failure(paths, "PROVIDER_CONFIG_INVALID")
    required_capabilities = REQUIRED_CAPABILITIES - (
        {"workspace-write"} if sandbox == "read-only" else set()
    )
    if sandbox not in {"read-only", "workspace-write"} or not required_capabilities <= set(
        provider.capabilities
    ):
        return _publish_failure(paths, "UNSUPPORTED_CAPABILITY")
    if timeout_seconds <= 0:
        return _publish_failure(paths, "PROVIDER_CONFIG_INVALID", "timeout must be positive")

    environment = _sanitize_environment(extra_env)
    try:
        harness_version = _harness_version(harness_command, environment)
    except (OSError, subprocess.SubprocessError):
        return _publish_failure(paths, "HARNESS_UNAVAILABLE")

    try:
        prompt = paths.prompt.read_text()
    except OSError:
        return _publish_failure(paths, "MISSING_PROMPT")
    try:
        start_sha = _git_sha(worktree)
    except subprocess.SubprocessError:
        return _publish_failure(paths, "GIT_UNAVAILABLE")
    for env_name, config_key in (
        ("GIT_AUTHOR_NAME", "user.name"),
        ("GIT_AUTHOR_EMAIL", "user.email"),
        ("GIT_COMMITTER_NAME", "user.name"),
        ("GIT_COMMITTER_EMAIL", "user.email"),
    ):
        value = subprocess.run(
            ["git", "config", "--get", config_key],
            cwd=worktree,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if value:
            environment[env_name] = value
    secret = secret_loader()
    if not secret:
        return _publish_failure(paths, "CREDENTIAL_UNAVAILABLE")
    started_at = _utc_now()
    started = time.monotonic()

    try:
        temporary_context = tempfile.TemporaryDirectory(prefix="cw-openrouter-worker-")
    except OSError:
        return _publish_failure(paths, "CREDENTIAL_BOUNDARY_UNSAFE")
    with temporary_context as temporary_name:
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o700)
            helper = temporary / "auth-helper"
            helper.write_text(_AUTH_HELPER)
            helper.chmod(0o700)
            last_message = temporary / "last-message.md"
            codex_home = temporary / "codex-home"
            codex_home.mkdir(mode=0o700)
            broker = _OneShotBroker(temporary, secret, timeout_seconds)
        except OSError:
            return _publish_failure(paths, "CREDENTIAL_BOUNDARY_UNSAFE")
        broker.start()

        provider_key = "cw_openrouter"
        auth_args: list[str] = []
        argv = [
            harness_command,
            "exec",
            "--cd",
            str(worktree),
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            "--output-last-message",
            str(last_message),
            "--model",
            provider.model,
            "-c",
            f'model_provider="{provider_key}"',
            "-c",
            f'model_providers.{provider_key}.name="OpenRouter"',
            "-c",
            f"model_providers.{provider_key}.base_url={json.dumps(provider.base_url)}",
            "-c",
            f'model_providers.{provider_key}.wire_api="responses"',
            "-c",
            f"model_providers.{provider_key}.auth.command={json.dumps(str(helper))}",
            "-c",
            f"model_providers.{provider_key}.auth.args={json.dumps(auth_args)}",
            "-c",
            f"model_providers.{provider_key}.auth.timeout_ms=3000",
            "-c",
            f"model_providers.{provider_key}.auth.refresh_interval_ms=0",
            "-",
        ]
        environment["CODEX_HOME"] = str(codex_home)
        try:
            process = subprocess.Popen(
                argv,
                cwd=worktree,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            broker.close()
            return _publish_failure(paths, "HARNESS_UNAVAILABLE")
        try:
            stdout, stderr = process.communicate(prompt.encode(), timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            stdout, stderr = process.communicate()
            broker.close()
            log, _, _ = _event_log(stdout, stderr, secret=secret)
            return _publish_failure(paths, "WORKER_TIMEOUT", log=log)
        broker.close()

        if not broker.consumed:
            log, _, _ = _event_log(stdout, stderr, secret=secret)
            return _publish_failure(
                paths, "CREDENTIAL_BOUNDARY_UNSAFE", "pre-turn auth was not consumed", log
            )

        # Active canary: if auth was never consumed, or remained reusable, this
        # call succeeds and the write-enabled result is rejected.
        canary = subprocess.run(
            [str(helper)], capture_output=True, timeout=3, env=_sanitize_environment()
        )
        if canary.returncode == 0 or canary.stdout:
            return _publish_failure(paths, "CREDENTIAL_BOUNDARY_UNSAFE")

        log, events, malformed = _event_log(stdout, stderr, secret=secret)
        if secret.encode() in stdout or secret.encode() in stderr:
            return _publish_failure(paths, "SECRET_EXPOSURE_DETECTED", log=log)
        try:
            result = last_message.read_text() if last_message.exists() else None
        except OSError:
            result = None
        tool_observed = any(
            event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "command_execution"
            for event in events
        )
        try:
            end_sha = _git_sha(worktree)
        except subprocess.SubprocessError:
            return _publish_failure(paths, "GIT_UNAVAILABLE", log=log)
        metadata = _execution_metadata(
            paths=paths,
            provider=provider,
            harness_command=harness_command,
            harness_version=harness_version,
            sandbox=sandbox,
            start_sha=start_sha,
            end_sha=end_sha,
            started_at=started_at,
            started=started,
            exit_status=process.returncode,
            prompt=prompt,
            result=result,
            log=log,
            events=events,
            tool_behavior="observed" if tool_observed else "not-observed",
        )
        if process.returncode != 0:
            return _publish_failure_with_metadata(
                paths, "HARNESS_EXIT_NONZERO", metadata, log, f"exit {process.returncode}"
            )
        if malformed:
            return _publish_failure_with_metadata(paths, "MALFORMED_EVENT_STREAM", metadata, log)
        if any(event.get("type") == "error" for event in events):
            return _publish_failure_with_metadata(paths, "HARNESS_REPORTED_ERROR", metadata, log)
        if not any(event.get("type") == "turn.completed" for event in events):
            return _publish_failure_with_metadata(
                paths, "MALFORMED_EVENT_STREAM", metadata, log, "missing terminal event"
            )
        if not tool_observed:
            metadata["tool_behavior"] = "unsupported"
            return _publish_failure_with_metadata(
                paths, "UNSUPPORTED_CAPABILITY", metadata, log, "no tool call observed"
            )
        if not result or not result.strip():
            return _publish_failure_with_metadata(paths, "MISSING_RESULT", metadata, log)
        if secret in result:
            return _publish_failure_with_metadata(paths, "SECRET_EXPOSURE_DETECTED", metadata, log)

        if metadata["usage"]["within_limit"] is False:
            return _publish_failure_with_metadata(paths, "USAGE_LIMIT_EXCEEDED", metadata, log)
        try:
            # @cw-trace ensures CTR-dag-021
            task_protocol.publish_success(
                paths,
                result=result,
                log=log,
                metadata=metadata,
                schema=json.loads(SCHEMA.read_text()),
            )
        except task_protocol.MetadataValidationError:
            return _publish_failure(paths, "METADATA_INVALID")
        return WorkerResult("done")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "probe"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--provider", required=True)
        command_parser.add_argument("--task-root", type=Path, required=True)
        command_parser.add_argument("--task-id", required=True)
        command_parser.add_argument("--worktree", type=Path, required=True)
        command_parser.add_argument("--main", type=Path, required=True)
        command_parser.add_argument("--timeout-seconds", type=int, default=900)
        command_parser.add_argument(
            "--admit-disabled",
            action="store_true",
            help="Explicitly admit a disabled provider for a bounded experiment/probe",
        )
    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config else load_config()
    providers = execution_providers_from_config(config)
    if args.provider not in providers:
        parser.error(f"unknown execution provider: {args.provider}")
    result = run_task(
        paths=task_protocol.task_paths(args.task_root, args.task_id),
        provider=providers[args.provider],
        worktree=args.worktree,
        main_checkout=args.main,
        timeout_seconds=args.timeout_seconds,
        sandbox="read-only" if args.command == "probe" else "workspace-write",
        admit_disabled=args.admit_disabled,
    )
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import stat
import subprocess

import pytest
from delegates import openrouter_worker, task_protocol
from providers import ExecutionProvider

FAKE_HARNESS = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("codex 0.test")
    raise SystemExit(0)
capture = os.environ["CW_FAKE_CAPTURE"]
prompt = sys.stdin.read()
configs = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
def config_value(suffix):
    value = next(v.split("=", 1)[1] for v in configs if v.split("=", 1)[0].endswith(suffix))
    return json.loads(value)
helper = config_value("auth.command")
helper_args = config_value("auth.args")
first = subprocess.run([helper, *helper_args], text=True, capture_output=True)
second = subprocess.run([helper, *helper_args], text=True, capture_output=True)
last_message = args[args.index("--output-last-message") + 1]
Path = __import__("pathlib").Path
Path(capture).write_text(json.dumps({
    "argv": args, "prompt": prompt,
    "environment": dict(os.environ),
    "first_ok": first.returncode == 0 and bool(first.stdout.strip()),
    "second_ok": second.returncode == 0 or bool(second.stdout.strip()),
}))
mode = os.environ.get("CW_FAKE_MODE", "success")
if mode == "timeout":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(60)
if mode == "nonzero":
    print(json.dumps({"type":"turn.completed","usage":{}}))
    raise SystemExit(7)
if mode == "malformed":
    print("not-json")
    raise SystemExit(0)
if mode == "reported-error":
    print(json.dumps({"type":"error","message":"upstream failed"}))
    raise SystemExit(0)
if mode == "secret-echo":
    print(first.stdout)
    raise SystemExit(0)
if mode != "missing-result":
    Path(last_message).write_text("worker answer\n")
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"worker answer"}}))
if mode != "unsupported":
    print(json.dumps({"type":"item.completed","item":{"type":"command_execution","status":"completed"}}))
usage = {"input_tokens": 2000 if mode == "high-usage" else 11, "output_tokens": 5}
terminal = {"type":"turn.completed", "usage": usage}
if mode == "resolved":
    terminal["resolved_model"] = "served/model-id"
print(json.dumps(terminal))
"""


@pytest.fixture
def git_paths(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=main, check=True)
    (main / "README").write_text("x")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=main, check=True)
    worktree = tmp_path / "worker"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "worker", str(worktree)], cwd=main, check=True
    )
    return main, worktree


@pytest.fixture
def fake_harness(tmp_path):
    path = tmp_path / "codex"
    path.write_text(FAKE_HARNESS)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def provider():
    return ExecutionProvider(
        name="preview-worker",
        enabled=True,
        execution_adapter="codex-responses",
        delegate="codex-responses",
        model="vendor/preview-model",
        base_url="https://openrouter.ai/api/v1",
        capability_tier="external-preview-tier",
        capabilities=("responses", "repo-read", "shell-tools", "workspace-write"),
        max_input_tokens=1000,
    )


def run_worker(tmp_path, git_paths, fake_harness, provider, *, mode="success", secret_loader=None):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "Make a harmless edit")
    capture = tmp_path / "capture.json"
    env = {"CW_FAKE_CAPTURE": str(capture), "CW_FAKE_MODE": mode, "SHOULD_BE_SECRET_TOKEN": "drop"}
    result = openrouter_worker.run_task(
        paths=paths,
        provider=provider,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=1 if mode == "timeout" else 10,
        harness_command=str(fake_harness),
        secret_loader=secret_loader or (lambda: "seeded-super-secret"),
        extra_env=env,
    )
    return paths, capture, result


def test_success_uses_responses_workspace_write_and_one_shot_auth(
    tmp_path, git_paths, fake_harness, provider
):
    paths, capture_path, result = run_worker(tmp_path, git_paths, fake_harness, provider)
    capture = json.loads(capture_path.read_text())
    durable = "\n".join(path.read_text() for path in paths.task_dir.iterdir() if path.is_file())

    assert result.status == "done"
    assert paths.done.exists() and not paths.error.exists()
    assert paths.result.read_text() == "worker answer\n"
    assert "--sandbox" in capture["argv"] and "workspace-write" in capture["argv"]
    assert "--ephemeral" in capture["argv"] and "--ignore-user-config" in capture["argv"]
    assert any('wire_api="responses"' in item for item in capture["argv"])
    assert capture["prompt"] == "Make a harmless edit"
    assert capture["first_ok"] is True
    assert capture["second_ok"] is False
    assert "SHOULD_BE_SECRET_TOKEN" not in capture["environment"]
    assert "seeded-super-secret" not in json.dumps(capture)
    assert "seeded-super-secret" not in durable
    metadata = json.loads(paths.metadata.read_text())
    schema = json.loads(openrouter_worker.SCHEMA.read_text())
    __import__("jsonschema").Draft202012Validator(schema).validate(metadata)
    assert metadata["requested_model"] == "vendor/preview-model"
    assert metadata["harness"]["version"] == "codex 0.test"
    assert metadata["resolved_model"] is None
    assert metadata["usage"]["input_tokens"] == 11


def test_main_checkout_refused_before_secret_or_harness(
    tmp_path, git_paths, fake_harness, provider
):
    main, _ = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "do it")
    called = []

    # @cw-trace verifies CTR-dag-020
    result = openrouter_worker.run_task(
        paths=paths,
        provider=provider,
        worktree=main,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(fake_harness),
        secret_loader=lambda: called.append(True) or "secret",
    )

    assert result.reason_code == "UNSAFE_WORKTREE"
    assert called == []
    assert paths.error.exists() and not paths.done.exists()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("nonzero", "HARNESS_EXIT_NONZERO"),
        ("malformed", "MALFORMED_EVENT_STREAM"),
        ("reported-error", "HARNESS_REPORTED_ERROR"),
        ("missing-result", "MISSING_RESULT"),
        ("timeout", "WORKER_TIMEOUT"),
        ("unsupported", "UNSUPPORTED_CAPABILITY"),
        ("secret-echo", "SECRET_EXPOSURE_DETECTED"),
        ("high-usage", "USAGE_LIMIT_EXCEEDED"),
    ],
)
def test_failures_publish_stable_exclusive_error(
    tmp_path, git_paths, fake_harness, provider, mode, reason
):
    paths, _, result = run_worker(tmp_path, git_paths, fake_harness, provider, mode=mode)

    assert result.reason_code == reason
    assert paths.error.exists() and not paths.done.exists()
    assert json.loads(paths.error.read_text())["reason"] == reason
    if mode == "high-usage":
        metadata = json.loads(paths.metadata.read_text())
        assert metadata["status"] == "error"
        assert metadata["reason_code"] == "USAGE_LIMIT_EXCEEDED"
        assert metadata["usage"]["input_tokens"] == 2000


def test_missing_key_fails_closed(tmp_path, git_paths, fake_harness, provider):
    paths, _, result = run_worker(
        tmp_path, git_paths, fake_harness, provider, secret_loader=lambda: None
    )
    assert result.reason_code == "CREDENTIAL_UNAVAILABLE"
    assert paths.error.exists() and not paths.done.exists()


def test_missing_prompt_publishes_stable_error(tmp_path, git_paths, fake_harness, provider):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1")
    result = openrouter_worker.run_task(
        paths=paths,
        provider=provider,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(fake_harness),
    )

    assert result.reason_code == "MISSING_PROMPT"
    assert paths.error.exists() and not paths.done.exists()


def test_disabled_provider_is_not_admitted(tmp_path, git_paths, fake_harness, provider):
    disabled = ExecutionProvider(**{**provider.__dict__, "enabled": False})
    paths, _, result = run_worker(tmp_path, git_paths, fake_harness, disabled)
    assert result.reason_code == "PROVIDER_DISABLED"
    assert paths.error.exists() and not paths.done.exists()


def test_provider_without_budget_is_invalid_before_secret(
    tmp_path, git_paths, fake_harness, provider
):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "do it")
    unbounded = ExecutionProvider(**{**provider.__dict__, "max_input_tokens": None})
    called = []
    result = openrouter_worker.run_task(
        paths=paths,
        provider=unbounded,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(fake_harness),
        secret_loader=lambda: called.append(True) or "secret",
    )

    assert result.reason_code == "PROVIDER_CONFIG_INVALID"
    assert called == []


def test_invalid_success_metadata_becomes_stable_error(
    tmp_path, git_paths, fake_harness, provider, monkeypatch
):
    def reject(*args, **kwargs):
        raise task_protocol.MetadataValidationError("bad metadata")

    monkeypatch.setattr(task_protocol, "publish_success", reject)
    paths, _, result = run_worker(tmp_path, git_paths, fake_harness, provider)

    assert result.reason_code == "METADATA_INVALID"
    assert paths.error.exists() and not paths.done.exists()


def test_read_only_probe_uses_read_only_sandbox(tmp_path, git_paths, fake_harness, provider):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "Inspect only")
    capture = tmp_path / "capture.json"
    result = openrouter_worker.run_task(
        paths=paths,
        provider=provider,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(fake_harness),
        secret_loader=lambda: "seeded-super-secret",
        extra_env={"CW_FAKE_CAPTURE": str(capture)},
        sandbox="read-only",
    )

    assert result.status == "done"
    invocation = json.loads(capture.read_text())["argv"]
    assert invocation[invocation.index("--sandbox") + 1] == "read-only"
    assert json.loads(paths.metadata.read_text())["sandbox"] == "read-only"


def test_harness_unavailable_is_stable_error(tmp_path, git_paths, provider):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "do it")
    result = openrouter_worker.run_task(
        paths=paths,
        provider=provider,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(tmp_path / "not-installed"),
    )

    assert result.reason_code == "HARNESS_UNAVAILABLE"
    assert paths.error.exists() and not paths.done.exists()


def test_capability_mismatch_refuses_before_secret(tmp_path, git_paths, fake_harness, provider):
    main, worktree = git_paths
    paths = task_protocol.create_task(tmp_path / "tasks", "task-1", "do it")
    incapable = ExecutionProvider(
        **{**provider.__dict__, "capabilities": ("responses", "repo-read", "workspace-write")}
    )
    called = []
    result = openrouter_worker.run_task(
        paths=paths,
        provider=incapable,
        worktree=worktree,
        main_checkout=main,
        timeout_seconds=10,
        harness_command=str(fake_harness),
        secret_loader=lambda: called.append(True) or "secret",
    )

    assert result.reason_code == "UNSUPPORTED_CAPABILITY"
    assert called == []


def test_reusable_auth_canary_fails_closed(
    tmp_path, git_paths, fake_harness, provider, monkeypatch
):
    original_run = openrouter_worker.subprocess.run

    def canary_leaks(argv, *args, **kwargs):
        if str(argv[0]).endswith("auth-helper"):
            return subprocess.CompletedProcess(argv, 0, b"still-secret", b"")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(openrouter_worker.subprocess, "run", canary_leaks)
    paths, _, result = run_worker(tmp_path, git_paths, fake_harness, provider)

    # @cw-trace verifies INV-dag-019
    assert result.reason_code == "CREDENTIAL_BOUNDARY_UNSAFE"
    assert paths.error.exists() and not paths.done.exists()


def test_resolved_identity_is_recorded_only_when_observed(
    tmp_path, git_paths, fake_harness, provider
):
    paths, _, result = run_worker(tmp_path, git_paths, fake_harness, provider, mode="resolved")
    metadata = json.loads(paths.metadata.read_text())
    assert result.status == "done"
    assert metadata["resolved_model"] == "served/model-id"
    assert metadata["model_resolution_status"] == "observed"

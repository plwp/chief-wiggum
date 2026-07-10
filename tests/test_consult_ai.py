from __future__ import annotations

import json
import subprocess
import sys

import consult_ai
import pytest


def write_config(path, *, optional_enabled=True):
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "codex": {"type": "tool", "tool": "codex", "enabled": True},
                    "gemini": {"type": "tool", "tool": "gemini", "enabled": optional_enabled},
                },
                "roles": {"reviewer": {"required": ["codex"], "optional": ["gemini"]}},
            }
        )
    )


def test_role_consult_writes_required_and_optional_outputs(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this.")
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)

    def fake_consult_provider(provider, prompt_text, model, cwd):
        return f"{provider.name}: {prompt_text}"

    monkeypatch.setattr(consult_ai, "consult_provider", fake_consult_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py",
            "--role",
            "reviewer",
            str(prompt),
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
            "--min-bytes",
            "1",
        ],
    )

    consult_ai.main()

    assert (output_dir / "reviewer-codex.md").read_text() == "codex: Review this."
    assert (output_dir / "reviewer-gemini.md").read_text() == "gemini: Review this."


def test_role_consult_does_not_fail_when_optional_provider_is_disabled(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this.")
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config, optional_enabled=False)
    called: list[str] = []

    def fake_consult_provider(provider, prompt_text, model, cwd):
        called.append(provider.name)
        return provider.name

    monkeypatch.setattr(consult_ai, "consult_provider", fake_consult_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py",
            "--role",
            "reviewer",
            str(prompt),
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
            "--min-bytes",
            "1",
        ],
    )

    consult_ai.main()

    assert called == ["codex"]
    assert (output_dir / "reviewer-codex.md").read_text() == "codex"
    assert not (output_dir / "reviewer-gemini.md").exists()


def test_role_consult_fails_when_required_provider_is_disabled(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this.")
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py",
            "--role",
            "reviewer",
            str(prompt),
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
            "--disable-provider",
            "codex",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1


def test_role_consult_fails_cleanly_for_unknown_role(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this.")
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py",
            "--role",
            "missing",
            str(prompt),
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1


def test_role_consult_rejects_single_output_path(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this.")
    config = tmp_path / "providers.json"
    write_config(config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py",
            "--role",
            "reviewer",
            str(prompt),
            "--config",
            str(config),
            "-o",
            str(tmp_path / "out.md"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 2


def test_claude_interactive_uses_delegate_submit_without_precreating_task(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    calls = []

    def fake_run_capture(cmd, **kwargs):
        calls.append(cmd)
        return f"RESULT={result_file}\n"

    # consult_* now route through _run_capture (process-group-aware runner, #95),
    # not subprocess.run — mock at that seam.
    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)

    output = consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    assert output == "delegate response"
    cmd = calls[0]
    assert "submit" in cmd
    assert "--prompt-file" in cmd
    assert "--task-id" not in cmd


# --- _run_capture: hard-timeout process-group runner (#95) -------------------

import time as _time  # noqa: E402


def test_run_capture_returns_stdout():
    assert consult_ai._run_capture(
        ["sh", "-c", "printf hello"], input_text=None, timeout=10, cwd=None, tool="t"
    ) == "hello"


def test_run_capture_passes_stdin():
    assert consult_ai._run_capture(
        ["cat"], input_text="piped-in", timeout=10, cwd=None, tool="t"
    ) == "piped-in"


def test_run_capture_raises_calledprocesserror_on_nonzero():
    with pytest.raises(subprocess.CalledProcessError):
        consult_ai._run_capture(
            ["sh", "-c", "exit 3"], input_text=None, timeout=10, cwd=None, tool="t"
        )


def test_run_capture_timeout_does_not_hang_on_surviving_grandchild():
    # A grandchild inherits the stdout pipe and outlives the timeout. subprocess.run
    # would block in communicate() draining that pipe for the grandchild's full 30s;
    # _run_capture must kill the whole process group and return promptly.
    start = _time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        consult_ai._run_capture(
            ["sh", "-c", "sleep 30 & sleep 30"],
            input_text=None, timeout=2, cwd=None, tool="t",
        )
    elapsed = _time.monotonic() - start
    assert elapsed < 15, f"timeout did not return promptly ({elapsed:.1f}s) — pipe hang not fixed"

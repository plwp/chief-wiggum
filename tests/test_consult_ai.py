from __future__ import annotations

import json
import subprocess
import sys

import consult_ai
import pytest

# A realistic-length prompt (>= consult_ai.MIN_PROMPT_BYTES) so tests that
# exercise the role-quorum machinery don't trip the short-prompt guard
# (chief-wiggum#163) — that guard has its own dedicated tests below.
PROMPT_TEXT = (
    "Review this change for correctness, safety, and completeness before "
    "merging. Consider edge cases, error handling, and how it interacts "
    "with existing code paths in the surrounding module. Call out anything "
    "that looks unsound or incomplete."
)
assert len(PROMPT_TEXT.encode("utf-8")) >= consult_ai.MIN_PROMPT_BYTES


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


def write_config_with_lenses(path, *, lenses=None):
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "codex": {"type": "tool", "tool": "codex", "enabled": True},
                    "gemini": {"type": "tool", "tool": "gemini", "enabled": True},
                },
                "roles": {
                    "reviewer": {
                        "required": ["codex", "gemini"],
                        "optional": [],
                        "lenses": lenses
                        if lenses is not None
                        else {"codex": "refute-soundness", "gemini": "completeness"},
                    }
                },
            }
        )
    )


def write_lenses(path):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "lenses": {
                    "refute-soundness": {
                        "goal": "Find the strongest reason this proposal is wrong.",
                        "exclusions": [
                            "Do NOT evaluate adoption cost.",
                            "Do NOT evaluate style or naming.",
                        ],
                    },
                    "completeness": {
                        "goal": "Check whether every case and actor is covered.",
                        "exclusions": ["Do NOT evaluate whether covered cases are correct."],
                    },
                },
            }
        )
    )


def test_role_consult_writes_required_and_optional_outputs(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
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

    assert (output_dir / "reviewer-codex.md").read_text() == f"codex: {PROMPT_TEXT}"
    assert (output_dir / "reviewer-gemini.md").read_text() == f"gemini: {PROMPT_TEXT}"


def test_role_consult_does_not_fail_when_optional_provider_is_disabled(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
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
    prompt.write_text(PROMPT_TEXT)
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
    prompt.write_text(PROMPT_TEXT)
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


# --- review lenses: bounded charters per provider (chief-wiggum#163) --------


def test_role_consult_appends_lens_charter_with_byte_identical_shared_body(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    lenses = tmp_path / "lenses.json"
    output_dir = tmp_path / "out"
    write_config_with_lenses(config)
    write_lenses(lenses)

    captured: dict[str, str] = {}

    def fake_consult_provider(provider, prompt_text, model, cwd):
        captured[provider.name] = prompt_text
        return f"{provider.name} response: {prompt_text}"

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
            "--lenses-config",
            str(lenses),
            "--output-dir",
            str(output_dir),
            "--min-bytes",
            "1",
        ],
    )

    consult_ai.main()

    codex_prompt = captured["codex"]
    gemini_prompt = captured["gemini"]

    # Both charters are appended, clearly delimited.
    assert "## Your charter" in codex_prompt
    assert "## Your charter" in gemini_prompt
    assert "Find the strongest reason this proposal is wrong." in codex_prompt
    assert "Check whether every case and actor is covered." in gemini_prompt
    # Each provider's own charter, not the other's.
    assert "Find the strongest reason" not in gemini_prompt
    assert "Check whether every case" not in codex_prompt

    # The shared body (everything before the charter section) is
    # byte-identical across every provider in the role.
    shared_codex = codex_prompt.split("## Your charter")[0]
    shared_gemini = gemini_prompt.split("## Your charter")[0]
    assert shared_codex == shared_gemini
    assert shared_codex == f"{PROMPT_TEXT}\n\n---\n\n"


def test_role_consult_leaves_unlensed_provider_prompt_unchanged(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    lenses = tmp_path / "lenses.json"
    output_dir = tmp_path / "out"
    # Only codex is mapped to a lens; gemini is unmapped and must be untouched.
    write_config_with_lenses(config, lenses={"codex": "refute-soundness"})
    write_lenses(lenses)

    captured: dict[str, str] = {}

    def fake_consult_provider(provider, prompt_text, model, cwd):
        captured[provider.name] = prompt_text
        return f"{provider.name} response: {prompt_text}"

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
            "--lenses-config",
            str(lenses),
            "--output-dir",
            str(output_dir),
            "--min-bytes",
            "1",
        ],
    )

    consult_ai.main()

    assert captured["gemini"] == PROMPT_TEXT
    assert captured["codex"] != PROMPT_TEXT
    assert "## Your charter" in captured["codex"]


def test_role_consult_fails_cleanly_for_unknown_lens(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    lenses = tmp_path / "lenses.json"
    output_dir = tmp_path / "out"
    write_config_with_lenses(config, lenses={"codex": "no-such-lens"})
    write_lenses(lenses)
    called: list[str] = []

    def fake_consult_provider(provider, prompt_text, model, cwd):
        called.append(provider.name)
        return "should never run"

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
            "--lenses-config",
            str(lenses),
            "--output-dir",
            str(output_dir),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1
    assert called == []


# --- robustness: short-prompt refusal (chief-wiggum#163) --------------------


def test_role_consult_refuses_short_prompt_before_any_provider_call(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("too short")
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)
    called: list[str] = []

    def fake_consult_provider(provider, prompt_text, model, cwd):
        called.append(provider.name)
        return "should never run"

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
        ],
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1
    assert called == []
    assert not output_dir.exists()


def test_single_tool_consult_refuses_empty_prompt(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("")
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt)])

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1


def test_single_tool_consult_accepts_prompt_at_the_floor(tmp_path, monkeypatch):
    # A prompt exactly at MIN_PROMPT_BYTES must be accepted, not rejected.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x" * consult_ai.MIN_PROMPT_BYTES)

    monkeypatch.setitem(consult_ai.TOOLS, "codex", lambda prompt, model=None, cwd=None: "ok response")
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt)])

    consult_ai.main()  # must not raise


# --- robustness: -o creates missing parent directories (chief-wiggum#163) --


def test_output_flag_creates_missing_parent_directories_on_success(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    out_path = tmp_path / "nested" / "deep" / "response.md"

    monkeypatch.setitem(
        consult_ai.TOOLS, "codex", lambda prompt, model=None, cwd=None: "a substantive response"
    )
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt), "-o", str(out_path)])

    consult_ai.main()

    assert out_path.read_text() == "a substantive response"


def test_output_flag_creates_missing_parent_directories_on_provider_error(tmp_path, monkeypatch):
    # This is the actual bug (chief-wiggum#163): previously the parent directory
    # was only created on the success path, so a provider failure with a missing
    # -o parent crashed with an unhandled FileNotFoundError instead of exiting
    # cleanly with the error message written to the requested path.
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    out_path = tmp_path / "nested" / "response.md"

    def failing_tool(prompt, model=None, cwd=None):
        raise subprocess.CalledProcessError(1, ["codex"], stderr="boom")

    monkeypatch.setitem(consult_ai.TOOLS, "codex", failing_tool)
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt), "-o", str(out_path)])

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1
    assert "boom" in out_path.read_text()

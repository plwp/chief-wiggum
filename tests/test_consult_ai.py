from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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

# Canned per-adapter usage payloads (chief-wiggum#134, IT-fh-05). codex_ok and
# claude_ok are VERBATIM captures from a live probe of the installed CLIs
# (codex-cli 0.142.5 / Claude Code 2.1.210); the gemini fixtures are derived
# from the installed @google/gemini-cli 0.36.0 bundle's own JsonFormatter /
# UiTelemetryService source (live probing was impossible — the free-tier
# Gemini Code Assist auth this machine has is dead, chief-wiggum memory
# env_gemini_cli_dead.md).
FIXTURES = Path(__file__).parent / "fixtures" / "consult_usage"


def _read(name: str) -> str:
    path = FIXTURES / name
    return path.read_text() if path.exists() else ""


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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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
    # @cw-trace verifies CTR-fh-010
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    calls = []

    def fake_run_capture(cmd, **kwargs):
        calls.append(cmd)
        return f"RESULT={result_file}\n", ""

    # consult_* now route through _run_capture (process-group-aware runner, #95),
    # not subprocess.run — mock at that seam. _run_capture now returns
    # (stdout, stderr) (CTR-fh-012, chief-wiggum#134).
    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)

    output, usage = consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    assert output == "delegate response"
    # claude-interactive's RESULT file carries no usage by construction —
    # always 'unavailable' (ADR-fh-05).
    assert usage.usage_status == "unavailable"
    assert usage.tokens_in is None and usage.tokens_out is None
    cmd = calls[0]
    assert "submit" in cmd
    assert "--prompt-file" in cmd
    assert "--task-id" not in cmd


# --- _run_capture: hard-timeout process-group runner (#95) -------------------

import time as _time  # noqa: E402


def test_run_capture_returns_stdout():
    assert consult_ai._run_capture(
        ["sh", "-c", "printf hello"], input_text=None, timeout=10, cwd=None, tool="t"
    ) == ("hello", "")


def test_run_capture_passes_stdin():
    assert consult_ai._run_capture(
        ["cat"], input_text="piped-in", timeout=10, cwd=None, tool="t"
    ) == ("piped-in", "")


def test_run_capture_returns_stderr_too():
    # @cw-trace verifies CTR-fh-012
    # some CLIs print usage-bearing JSON to stderr rather than
    # stdout — _run_capture must return both, never stdout alone.
    out, err = consult_ai._run_capture(
        ["sh", "-c", "printf out; printf err >&2"], input_text=None, timeout=10, cwd=None, tool="t"
    )
    assert out == "out"
    assert err == "err"


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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
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

    monkeypatch.setitem(
        consult_ai.TOOLS, "codex",
        lambda prompt, model=None, cwd=None: ("ok response", consult_ai.Usage()),
    )
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt)])

    consult_ai.main()  # must not raise


def test_short_prompt_with_substantive_context_is_accepted(tmp_path, monkeypatch):
    # The guard applies to the FINAL assembled prompt (prompt file + --context),
    # so a legitimately small prompt file paired with substantive context must
    # not be rejected.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review the attached context.")
    context = tmp_path / "context.md"
    context.write_text(PROMPT_TEXT)

    sent = {}

    def fake_tool(prompt, model=None, cwd=None):
        sent["prompt"] = prompt
        return "ok response", consult_ai.Usage()

    monkeypatch.setitem(consult_ai.TOOLS, "codex", fake_tool)
    monkeypatch.setattr(
        sys, "argv", ["consult_ai.py", "codex", str(prompt), "--context", str(context)]
    )

    consult_ai.main()  # must not raise

    assert "Review the attached context." in sent["prompt"]
    assert PROMPT_TEXT in sent["prompt"]


def test_short_prompt_with_short_context_is_still_refused(tmp_path, monkeypatch):
    # Context counts toward the size check, but if prompt + context together
    # are still under the floor, the refusal must still fire before any call.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("tiny")
    context = tmp_path / "context.md"
    context.write_text("also tiny")
    called: list[str] = []

    def never_called_tool(prompt, model=None, cwd=None):
        called.append("codex")
        return "x", consult_ai.Usage()

    monkeypatch.setitem(consult_ai.TOOLS, "codex", never_called_tool)
    monkeypatch.setattr(
        sys, "argv", ["consult_ai.py", "codex", str(prompt), "--context", str(context)]
    )

    with pytest.raises(SystemExit) as exc:
        consult_ai.main()

    assert exc.value.code == 1
    assert called == []


# --- robustness: -o creates missing parent directories (chief-wiggum#163) --


def test_output_flag_creates_missing_parent_directories_on_success(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    out_path = tmp_path / "nested" / "deep" / "response.md"

    monkeypatch.setitem(
        consult_ai.TOOLS, "codex",
        lambda prompt, model=None, cwd=None: ("a substantive response", consult_ai.Usage()),
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


def test_called_process_error_falls_back_to_stdout_when_stderr_is_empty(tmp_path, monkeypatch):
    # In --json mode a provider CLI can report its error via stdout (e.g.
    # codex exec --json emits an {"type":"error",...} event there, not on
    # stderr) — the error message must not go blank just because .stderr is
    # empty.
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    out_path = tmp_path / "response.md"

    def failing_tool(prompt, model=None, cwd=None):
        raise subprocess.CalledProcessError(1, ["codex"], output="stdout-side error detail", stderr="")

    monkeypatch.setitem(consult_ai.TOOLS, "codex", failing_tool)
    monkeypatch.setattr(sys, "argv", ["consult_ai.py", "codex", str(prompt), "-o", str(out_path)])

    with pytest.raises(SystemExit):
        consult_ai.main()

    assert "stdout-side error detail" in out_path.read_text()


# --- per-adapter usage capture (chief-wiggum#134, IT-fh-05) ------------------
#
# For each adapter: a usage-bearing ("ok") sample, a usage-absent ("missing")
# sample, a partial (one-sided token count) sample, and a stderr-only sample
# proving both streams are scanned (CTR-fh-012).


def test_codex_usage_ok_resolves_tokens_and_model(monkeypatch):
    # @cw-trace verifies CTR-fh-010 CTR-fh-013
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: "gpt-5.5")
    stdout = _read("codex_ok.stdout.jsonl")
    text, usage = consult_ai._codex_agent_text(stdout), consult_ai._parse_codex_usage(stdout, "", None)
    assert text == "PONG"
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 12844 and usage.tokens_out == 19
    assert usage.resolved_model == "gpt-5.5"
    assert usage.resolved_model not in consult_ai.ADAPTER_BY_TOOL  # never a bare alias


def test_codex_usage_missing_is_unavailable_never_zero(monkeypatch):
    # @cw-trace verifies CTR-fh-011 CTR-fh-015
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: "gpt-5.5")
    stdout = _read("codex_missing.stdout.jsonl")
    usage = consult_ai._parse_codex_usage(stdout, "", None)
    assert usage.usage_status == "unavailable"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_codex_usage_partial_nulls_both_tokens(monkeypatch):
    # @cw-trace verifies CTR-fh-015
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: None)
    stdout = _read("codex_partial.stdout.jsonl")
    usage = consult_ai._parse_codex_usage(stdout, "", None)
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None  # both-tokens-or-null


def test_codex_usage_reads_stderr_when_stdout_lacks_it(monkeypatch):
    # @cw-trace verifies CTR-fh-012
    # A stdout-only parser would report 'unavailable' here — proves CTR-fh-012.
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: None)
    stdout = _read("codex_stderr_only.stdout.jsonl")
    stderr = _read("codex_stderr_only.stderr.jsonl")
    usage = consult_ai._parse_codex_usage(stdout, stderr, None)
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 8000 and usage.tokens_out == 25


def test_codex_model_override_takes_precedence_over_config(monkeypatch):
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: "gpt-5.5")
    stdout = _read("codex_ok.stdout.jsonl")
    usage = consult_ai._parse_codex_usage(stdout, "", "gpt-5.4")
    assert usage.resolved_model == "gpt-5.4"


def test_codex_configured_model_reads_config_toml(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert consult_ai._codex_configured_model() == "gpt-5.5"


def test_codex_configured_model_none_when_config_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "does-not-exist"))
    assert consult_ai._codex_configured_model() is None


def test_gemini_usage_ok_resolves_tokens_and_model():
    # @cw-trace verifies CTR-fh-010 CTR-fh-013
    stdout = _read("gemini_ok.stdout.json")
    text, usage = consult_ai._parse_gemini_output(stdout, "")
    assert text == "PONG"
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 450 and usage.tokens_out == 12
    assert usage.resolved_model == "gemini-3.1-pro-preview"


def test_gemini_usage_missing_is_unavailable_never_zero():
    # @cw-trace verifies CTR-fh-011 CTR-fh-015
    stdout = _read("gemini_missing.stdout.json")
    text, usage = consult_ai._parse_gemini_output(stdout, "")
    assert text == "PONG"
    assert usage.usage_status == "unavailable"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_gemini_usage_partial_nulls_both_tokens():
    # @cw-trace verifies CTR-fh-015
    stdout = _read("gemini_partial.stdout.json")
    _text, usage = consult_ai._parse_gemini_output(stdout, "")
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_gemini_usage_reads_stderr_when_stdout_lacks_it():
    # @cw-trace verifies CTR-fh-012
    stderr = _read("gemini_stderr_only.stderr.json")
    text, usage = consult_ai._parse_gemini_output("not json at all", stderr)
    assert text == "PONG"
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 450 and usage.tokens_out == 12


def test_claude_usage_ok_resolves_tokens_and_single_model():
    # @cw-trace verifies CTR-fh-010 CTR-fh-013
    stdout = _read("claude_ok.stdout.json")
    text, usage = consult_ai._parse_claude_output(stdout, "", None)
    assert text == "PONG"
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 2 and usage.tokens_out == 14
    assert usage.resolved_model == "claude-fable-5"  # matches top-level usage, not the haiku title-gen call


def test_claude_usage_missing_is_unavailable_never_zero():
    # @cw-trace verifies CTR-fh-011 CTR-fh-015
    stdout = _read("claude_missing.stdout.json")
    text, usage = consult_ai._parse_claude_output(stdout, "", None)
    assert text == "PONG"
    assert usage.usage_status == "unavailable"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_claude_usage_partial_nulls_both_tokens():
    # @cw-trace verifies CTR-fh-015
    stdout = _read("claude_partial.stdout.json")
    _text, usage = consult_ai._parse_claude_output(stdout, "", None)
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_claude_usage_reads_stderr_when_stdout_lacks_it():
    # @cw-trace verifies CTR-fh-012
    stderr = _read("claude_stderr_only.stderr.json")
    text, usage = consult_ai._parse_claude_output("not json at all", stderr, None)
    assert text == "PONG"
    assert usage.usage_status == "provider-json"
    assert usage.tokens_in == 2 and usage.tokens_out == 14
    assert usage.resolved_model == "claude-fable-5"


def test_claude_usage_falls_back_to_model_override_when_unresolvable():
    stdout = json.dumps({"result": "PONG", "usage": {"input_tokens": 1, "output_tokens": 2}})
    _text, usage = consult_ai._parse_claude_output(stdout, "", "claude-sonnet-5")
    assert usage.resolved_model == "claude-sonnet-5"


class _FakeUsageMetadata:
    def __init__(self, prompt_token_count=None, candidates_token_count=None):
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count


class _FakeVertexResponse:
    def __init__(self, usage_metadata=None, model_version=None):
        self.usage_metadata = usage_metadata
        self.model_version = model_version


def test_vertex_usage_ok_wires_sdk_metadata():
    # @cw-trace verifies CTR-fh-010
    response = _FakeVertexResponse(
        usage_metadata=_FakeUsageMetadata(prompt_token_count=100, candidates_token_count=40),
        model_version="gemini-3.1-pro-preview",
    )
    usage = consult_ai._parse_vertex_usage(response, "gemini-3.1-pro-preview")
    assert usage.usage_status == "sdk-metadata"
    assert usage.tokens_in == 100 and usage.tokens_out == 40
    assert usage.resolved_model == "gemini-3.1-pro-preview"


def test_vertex_usage_missing_is_unavailable_never_zero():
    # @cw-trace verifies CTR-fh-015
    response = _FakeVertexResponse(usage_metadata=None)
    usage = consult_ai._parse_vertex_usage(response, "gemini-3.1-pro-preview")
    assert usage.usage_status == "unavailable"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_vertex_usage_partial_nulls_both_tokens():
    # @cw-trace verifies CTR-fh-015
    response = _FakeVertexResponse(
        usage_metadata=_FakeUsageMetadata(prompt_token_count=100, candidates_token_count=None),
    )
    usage = consult_ai._parse_vertex_usage(response, "gemini-3.1-pro-preview")
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


def test_vertex_usage_parse_exception_never_fails_the_consult(monkeypatch):
    # @cw-trace verifies CTR-fh-011
    # consult_gemini_vertex wraps _parse_vertex_usage in try/except (CTR-fh-011);
    # simulate a surprising SDK object that raises when read.
    class _Explodes:
        @property
        def usage_metadata(self):
            raise RuntimeError("sdk surprise")

    project_secret = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: project_secret.get(name))

    class _FakeModels:
        def generate_content(self, model, contents):
            resp = _Explodes()
            resp.text = "PONG"
            resp.model_version = model
            return resp

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    fake_genai = type("fake_genai_module", (), {"Client": _FakeClient})
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", type("fake_google_module", (), {"genai": fake_genai}))

    text, usage = consult_ai.consult_gemini_vertex("prompt")
    assert text == "PONG"
    assert usage.usage_status == "unavailable"


# --- --ticket threading + telemetry emission (chief-wiggum#134) -------------


def test_emit_consult_telemetry_threads_ticket_and_usage(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))

    usage = consult_ai.Usage(tokens_in=10, tokens_out=5, resolved_model="claude-sonnet-5",
                             usage_status="provider-json")
    consult_ai._emit_consult_telemetry("claude", "claude-sonnet-5", "/some/repo/path", usage, ticket="134")

    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["ticket"] == "134"
    assert rec["repo"] == "path"
    assert rec["adapter"] == "claude-cli"
    assert rec["requested_model"] == "claude-sonnet-5"
    assert rec["usage_status"] == "provider-json"
    assert rec["tokens_in"] == 10 and rec["tokens_out"] == 5


def test_single_tool_consult_emits_telemetry_and_threads_ticket(tmp_path, monkeypatch):
    log = tmp_path / "f.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))

    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)

    def fake_tool(prompt, model=None, cwd=None):
        return "response text", consult_ai.Usage(
            tokens_in=7, tokens_out=3, resolved_model="claude-sonnet-5", usage_status="provider-json",
        )

    monkeypatch.setitem(consult_ai.TOOLS, "codex", fake_tool)
    monkeypatch.setattr(
        sys, "argv", ["consult_ai.py", "codex", str(prompt), "--ticket", "42"],
    )

    consult_ai.main()

    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["ticket"] == "42"
    assert rec["tokens_in"] == 7 and rec["tokens_out"] == 3


def test_role_consult_threads_ticket_into_consult_provider(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)

    received_ticket = {}

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None):
        received_ticket[provider.name] = ticket
        return f"{provider.name} response"

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
            "--ticket",
            "99",
        ],
    )

    consult_ai.main()

    assert received_ticket["codex"] == "99"

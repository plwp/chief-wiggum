from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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


def test_role_consult_prints_blind_provider_warning_and_writes_it_to_manifest(tmp_path, monkeypatch, capsys):
    """chief-wiggum#319: a quorum where every required provider is "ok" must
    still surface, loudly, that one of them answered from the prompt alone."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config)  # reviewer: required=[codex], optional=[gemini]; requires_repo_read defaults True

    response_text = "A substantive response with several findings to report."

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
        if provider.name == "gemini":
            # tokens_in == exactly the prompt's own estimated size: the
            # textbook "answered from the prompt alone" signature.
            blind_tokens = len(prompt_text.strip()) // 4
            return response_text, consult_ai.Usage(tokens_in=blind_tokens, usage_status="sdk-metadata")
        return response_text, consult_ai.Usage(tokens_in=5_000_000, usage_status="provider-json")

    monkeypatch.setattr(consult_ai, "consult_provider", fake_consult_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consult_ai.py", "--role", "reviewer", str(prompt),
            "--config", str(config), "--output-dir", str(output_dir), "--min-bytes", "1",
        ],
    )

    consult_ai.main()

    captured = capsys.readouterr()
    assert "did not read the repo" in captured.err
    assert "gemini" in captured.err

    manifest = json.loads((output_dir / "reviewer-manifest.json").read_text())
    assert manifest["blindness"]["outcome"] == "findings"
    assert manifest["blindness"]["findings"][0]["provider"] == "gemini"
    assert manifest["ok"] is True  # blindness is a report, never a gate


def test_role_consult_does_not_fail_when_optional_provider_is_disabled(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config, optional_enabled=False)
    called: list[str] = []

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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


# --- optional-provider timeout knob (chief-wiggum#188) ----------------------
#
# claude-interactive timed out at its full 1800s budget on two consecutive
# large-prompt consults (round-2 design, architecture_critic) while
# contributing nothing — it is optional in every shipped role. The fix: a
# role's optional_timeout_seconds caps the delegate's wall-clock when it's
# running in the OPTIONAL slot, so an unresponsive interactive session fails
# fast and cleanly instead of holding the whole role's quorum to 1800s.


def test_claude_interactive_default_timeout_matches_tool_timeouts_budget(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    # No override given -> falls back to the full delegate budget, plus the
    # small grace buffer that lets the delegate's own internal poll loop exit
    # gracefully before the outer hard-kill would fire.
    assert captured["timeout"] == consult_ai.TOOL_TIMEOUTS["claude-interactive"] + 30
    assert "--timeout-seconds" in captured["cmd"]
    idx = captured["cmd"].index("--timeout-seconds")
    assert captured["cmd"][idx + 1] == str(consult_ai.TOOL_TIMEOUTS["claude-interactive"])


def test_claude_interactive_timeout_override_shortens_both_inner_and_outer_budget(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path), timeout=300)

    # The delegate script's own --timeout-seconds is shortened to match, so it
    # exits GRACEFULLY on its own poll loop well before the outer hard-kill.
    assert captured["timeout"] == 330  # override + grace buffer
    idx = captured["cmd"].index("--timeout-seconds")
    assert captured["cmd"][idx + 1] == "300"


def test_consult_provider_threads_timeout_override_to_delegate(monkeypatch):
    provider = consult_ai.Provider(
        name="claude-interactive", type="delegate", enabled=True, delegate="claude-interactive",
    )
    received = {}

    def fake_consult_claude_interactive(prompt, model=None, cwd=None, timeout=None, **kwargs):
        received["timeout"] = timeout
        return "response", consult_ai.Usage()

    monkeypatch.setattr(consult_ai, "consult_claude_interactive", fake_consult_claude_interactive)

    consult_ai.consult_provider(provider, "prompt", None, None, timeout_override=42)

    assert received["timeout"] == 42


def test_consult_provider_threads_timeout_override_to_tool_providers(monkeypatch):
    # chief-wiggum#330: timeout_override used to be dropped for tool
    # providers entirely (only the claude-interactive delegate branch
    # threaded it) — but codex/gemini/gemini-vertex/claude's own
    # TOOL_TIMEOUTS entries (600-1200s) are all ABOVE the 300s optional cap,
    # so an optional tool provider could hold a role's wall-clock to its
    # full budget exactly like the delegate used to. Every consult_* tool
    # function already accepts a `timeout` kwarg; consult_provider must pass
    # timeout_override straight through on the tool branch too.
    provider = consult_ai.Provider(name="codex", type="tool", enabled=True, tool="codex")
    received = {}

    def fake_codex(prompt, model=None, cwd=None, timeout=None):
        received["timeout"] = timeout
        return "a substantive codex response", consult_ai.Usage()

    monkeypatch.setitem(consult_ai.TOOLS, "codex", fake_codex)

    consult_ai.consult_provider(provider, "prompt", None, None, timeout_override=42)

    assert received["timeout"] == 42


def test_consult_provider_tool_branch_passes_none_timeout_for_a_required_provider(monkeypatch):
    # A required provider's timeout_override is None (its full budget) —
    # confirms the threading is a straight pass-through, not something that
    # invents a value when there isn't one.
    provider = consult_ai.Provider(name="codex", type="tool", enabled=True, tool="codex")
    received = {}

    def fake_codex(prompt, model=None, cwd=None, timeout=None):
        received["timeout"] = timeout
        return "a substantive codex response", consult_ai.Usage()

    monkeypatch.setitem(consult_ai.TOOLS, "codex", fake_codex)

    consult_ai.consult_provider(provider, "prompt", None, None, timeout_override=None)

    assert received["timeout"] is None


def test_consult_provider_threads_ticket_into_delegate_session_naming(monkeypatch):
    """chief-wiggum#331: consult_provider's ticket kwarg must reach the delegate
    call so a task-scoped session name can be derived from it."""
    provider = consult_ai.Provider(
        name="claude-interactive", type="delegate", enabled=True, delegate="claude-interactive",
    )
    received = {}

    def fake_consult_claude_interactive(prompt, model=None, cwd=None, timeout=None, **kwargs):
        received["ticket"] = kwargs.get("ticket")
        return "response", consult_ai.Usage()

    monkeypatch.setattr(consult_ai, "consult_claude_interactive", fake_consult_claude_interactive)

    consult_ai.consult_provider(provider, "prompt", None, None, ticket="42")

    assert received["ticket"] == "42"


# --- task-scoped delegate sessions (chief-wiggum#331) ------------------------
#
# The delegate used to always talk to the SAME shared tmux session
# ("cw-claude"), which `start_session` only creates if absent — so every
# consult after the first inherited the ENTIRE accumulated transcript of
# every prior delegated task, paid the ~N x context billing that implies, and
# serialized any two concurrent consults (a wave running tickets in parallel)
# onto the one REPL. The fix is task-scoped sessions: every call gets its own
# unique session name, so it always starts from empty context (nothing to
# "/clear" — the session never existed before), and two concurrent calls get
# two independent sessions that cannot queue behind each other. The session
# is stopped in a finally-path so nothing survives a completed call.


def test_claude_interactive_never_uses_the_shared_default_session(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")

    def fake_run_capture(cmd, **kwargs):
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: None)

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))


def test_claude_interactive_passes_a_session_flag_before_submit(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    calls = []

    def fake_run_capture(cmd, **kwargs):
        calls.append(cmd)
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: None)

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    cmd = calls[0]
    assert "--session" in cmd
    session_idx = cmd.index("--session")
    session_name = cmd[session_idx + 1]
    # Never the shared constant — every call is task-scoped.
    assert session_name != "cw-claude"
    assert session_name.startswith("cw-claude-")
    # --session (a top-level flag) must precede the "submit" subcommand.
    assert cmd.index("submit") > session_idx + 1


def test_claude_interactive_two_consecutive_calls_get_different_sessions(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    sessions = []

    def fake_run_capture(cmd, **kwargs):
        sessions.append(cmd[cmd.index("--session") + 1])
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: None)

    consult_ai.consult_claude_interactive("first prompt", cwd=str(tmp_path))
    consult_ai.consult_claude_interactive("second prompt", cwd=str(tmp_path))

    assert len(sessions) == 2
    assert sessions[0] != sessions[1]


def test_claude_interactive_session_name_incorporates_the_ticket(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    calls = []

    def fake_run_capture(cmd, **kwargs):
        calls.append(cmd)
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: None)

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path), ticket="331")

    cmd = calls[0]
    session_name = cmd[cmd.index("--session") + 1]
    assert "331" in session_name


def test_claude_interactive_stops_its_session_after_a_successful_consult(tmp_path, monkeypatch):
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    stopped = []

    def fake_run_capture(cmd, **kwargs):
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: stopped.append(session))

    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    assert len(stopped) == 1
    assert stopped[0].startswith("cw-claude-")


def test_claude_interactive_stops_its_session_even_when_the_call_times_out(tmp_path, monkeypatch):
    stopped = []

    def fake_run_capture(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: stopped.append(session))

    with pytest.raises(subprocess.TimeoutExpired):
        consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    assert len(stopped) == 1


def test_claude_interactive_stops_its_session_even_when_the_result_path_is_missing(tmp_path, monkeypatch):
    stopped = []

    def fake_run_capture(cmd, **kwargs):
        return f"RESULT={tmp_path / 'never-written.md'}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: stopped.append(session))

    with pytest.raises(RuntimeError):
        consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))

    assert len(stopped) == 1


def test_claude_interactive_two_concurrent_calls_use_distinct_sessions_and_never_block_each_other(tmp_path, monkeypatch):
    """chief-wiggum#331 AC2: two concurrent consults must not queue on one
    shared REPL. Each call's fake transport blocks on a 2-party barrier before
    returning — if the two calls were serialized (one waiting on the other
    before even reaching the transport), the barrier would never fill and the
    test would hang/time out. Session names are also asserted distinct."""
    import threading

    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    barrier = threading.Barrier(2, timeout=5)
    sessions: list[str] = []
    lock = threading.Lock()

    def fake_run_capture(cmd, **kwargs):
        with lock:
            sessions.append(cmd[cmd.index("--session") + 1])
        barrier.wait()  # both threads must arrive here concurrently
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(consult_ai, "_stop_delegate_session", lambda session: None)

    results = [None, None]
    errors = [None, None]

    def run(i, prompt):
        try:
            results[i] = consult_ai.consult_claude_interactive(prompt, cwd=str(tmp_path))
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors[i] = exc

    t1 = threading.Thread(target=run, args=(0, "prompt one"))
    t2 = threading.Thread(target=run, args=(1, "prompt two"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == [None, None]
    assert not t1.is_alive() and not t2.is_alive()
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]


def test_stop_delegate_session_is_best_effort_and_never_raises(monkeypatch):
    """A failure to tear down a stray tmux session degrades to a stray
    session, never a crashed (otherwise-successful) consult."""

    def fake_run(*args, **kwargs):
        raise OSError("tmux not installed")

    monkeypatch.setattr(consult_ai.subprocess, "run", fake_run)

    consult_ai._stop_delegate_session("cw-claude-does-not-matter")  # must not raise


def write_config_with_delegate(path, *, optional_timeout_seconds=None, claude_interactive_required=False):
    role: dict = {"required": ["codex"], "optional": []}
    if claude_interactive_required:
        role["required"].append("claude-interactive")
    else:
        role["optional"].append("claude-interactive")
    if optional_timeout_seconds is not None:
        role["optional_timeout_seconds"] = optional_timeout_seconds
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "codex": {"type": "tool", "tool": "codex", "enabled": True},
                    "claude-interactive": {
                        "type": "delegate", "delegate": "claude-interactive", "enabled": True,
                    },
                },
                "roles": {"reviewer": role},
            }
        )
    )


def test_role_quorum_skips_hung_optional_delegate_fast_and_still_succeeds(tmp_path, monkeypatch):
    # Mocks the delegate subprocess at the _run_capture seam: codex answers
    # normally, claude-interactive "hangs" (its process-group runner would
    # raise TimeoutExpired once ITS deadline elapses). The quorum must (a)
    # cap that deadline well below the delegate's 1800s default when the
    # provider is optional, and (b) still report an overall OK role because
    # the required provider (codex) succeeded.
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config_with_delegate(config)

    captured_timeouts: dict[str, int] = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured_timeouts[tool] = timeout
        if tool == "codex":
            return "a substantive codex response, long enough to pass validation", ""
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
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

    start = _time.monotonic()
    consult_ai.main()
    elapsed = _time.monotonic() - start

    manifest = json.loads((output_dir / "reviewer-manifest.json").read_text())
    assert manifest["ok"] is True
    statuses = {r["name"]: r["status"] for r in manifest["results"]}
    assert statuses == {"codex": "ok", "claude-interactive": "failed"}

    # The measurable fix (chief-wiggum#188): the optional delegate's budget is
    # far below its 1800s default, so the timed-out call never gates the
    # role's wall-clock to the full budget.
    assert captured_timeouts["claude-interactive"] < consult_ai.TOOL_TIMEOUTS["claude-interactive"]
    assert captured_timeouts["claude-interactive"] == consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS + 30
    # And since everything here is mocked (no real sleeping), the whole
    # role-quorum call returns promptly.
    assert elapsed < 5


def test_role_quorum_honors_per_role_optional_timeout_override(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config_with_delegate(config, optional_timeout_seconds=42)

    captured_timeouts: dict[str, int] = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured_timeouts[tool] = timeout
        if tool == "codex":
            return "a substantive codex response, long enough to pass validation", ""
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
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

    assert captured_timeouts["claude-interactive"] == 42 + 30


def test_role_quorum_does_not_shorten_a_required_delegates_timeout(tmp_path, monkeypatch):
    # The shortening is specific to the OPTIONAL slot (chief-wiggum#188) — a
    # role that requires claude-interactive must still get its full budget.
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config_with_delegate(config, claude_interactive_required=True)

    result_file = tmp_path / "result.md"
    result_file.write_text("a substantive claude-interactive response, long enough to pass")

    captured_timeouts: dict[str, int] = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured_timeouts[tool] = timeout
        if tool == "codex":
            return "a substantive codex response, long enough to pass validation", ""
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
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

    assert captured_timeouts["claude-interactive"] == consult_ai.TOOL_TIMEOUTS["claude-interactive"] + 30


# --- reduced retry budget after a timeout (chief-wiggum#330 AC3) ------------
#
# "A codex timeout at 600s is retried for another full 600s with the
# identical ~60k-token prompt" — the pre-#330 behavior. reduced_retry_timeout
# halves the tool's resolved budget (floored at a usable minimum) for the
# retry that follows a timeout-classified failure; the --role execute
# closure opts into providers.py's per-attempt retry context to apply it.


def test_reduced_retry_timeout_halves_the_resolved_budget(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    assert consult_ai.reduced_retry_timeout("codex", None) == consult_ai.TOOL_TIMEOUTS["codex"] // 2


def test_reduced_retry_timeout_is_floored_at_a_usable_minimum(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    assert consult_ai.reduced_retry_timeout("codex", 90) == consult_ai.MIN_RETRY_TIMEOUT_SECONDS


def test_role_quorum_gives_a_codex_retry_a_smaller_budget_after_a_timeout(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config(config, optional_enabled=False)  # reviewer: required=[codex] only

    attempts: list[int | None] = []

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
        attempts.append(timeout_override)
        if len(attempts) == 1:
            raise TimeoutError("codex did not respond in time")
        return "a substantive codex response, long enough to pass validation", consult_ai.Usage()

    monkeypatch.setattr(consult_ai, "consult_provider", fake_consult_provider)
    monkeypatch.setattr(
        sys, "argv",
        [
            "consult_ai.py", "--role", "reviewer", str(prompt),
            "--config", str(config), "--output-dir", str(output_dir),
            "--min-bytes", "1", "--max-attempts", "2",
        ],
    )

    consult_ai.main()

    assert len(attempts) == 2
    first, second = attempts
    # A REQUIRED provider's first attempt has no override (None -> full
    # budget); the retry after a timeout must be a real, smaller NUMBER.
    assert first is None
    assert second is not None
    assert second < consult_ai.TOOL_TIMEOUTS["codex"]


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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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
        lambda prompt, model=None, cwd=None, timeout=None: ("ok response", consult_ai.Usage()),
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

    def fake_tool(prompt, model=None, cwd=None, timeout=None):
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

    def never_called_tool(prompt, model=None, cwd=None, timeout=None):
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
        lambda prompt, model=None, cwd=None, timeout=None: ("a substantive response", consult_ai.Usage()),
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

    def failing_tool(prompt, model=None, cwd=None, timeout=None):
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

    def failing_tool(prompt, model=None, cwd=None, timeout=None):
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


def test_codex_drifted_event_shape_never_fails_a_successful_consult(monkeypatch):
    # @cw-trace verifies CTR-fh-011
    # P1 regression (PR #195 review): a drifted event shape (item not a dict,
    # text not a string) previously raised OUTSIDE the usage try/except in
    # consult_codex, turning a successful provider call into a failed consult.
    drifted = "\n".join([
        json.dumps({"type": "item.completed", "item": "not-a-dict"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": 12345}}),
        json.dumps({"type": "item.completed"}),  # no item at all
        json.dumps(["not", "an", "object"]),  # non-dict JSON line
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ])
    monkeypatch.setattr(consult_ai, "_run_capture", lambda cmd, **kw: (drifted, ""))
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: "gpt-5.5")

    text, usage = consult_ai.consult_codex("prompt")

    # No usable agent_message text → degrade to the raw stream, never raise.
    assert text == drifted
    # Usage parsing is independent of the drifted text events and still succeeds.
    assert usage.tokens_in == 10 and usage.tokens_out == 5


def test_codex_agent_text_reads_stderr_too():
    # @cw-trace verifies CTR-fh-012
    stderr = json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "FROM-STDERR"}})
    assert consult_ai._codex_agent_text("no json here", stderr) == "FROM-STDERR"


def test_codex_string_token_counts_are_coerced(monkeypatch):
    # P3 regression: numeric strings coerce to ints; junk degrades to partial,
    # never a crash and never a trusted non-int.
    monkeypatch.setattr(consult_ai, "_codex_configured_model", lambda: None)
    ok = json.dumps({"type": "turn.completed",
                     "usage": {"input_tokens": "12844", "output_tokens": "19"}})
    usage = consult_ai._parse_codex_usage(ok, "", None)
    assert usage.tokens_in == 12844 and usage.tokens_out == 19

    junk = json.dumps({"type": "turn.completed",
                       "usage": {"input_tokens": "lots", "output_tokens": 19}})
    usage = consult_ai._parse_codex_usage(junk, "", None)
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


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


def test_gemini_envelope_parses_but_stats_drift_keeps_answer_text(monkeypatch):
    # @cw-trace verifies CTR-fh-011
    # P2 regression (PR #195 review): when the envelope parses but stats.models
    # drifts, the caller previously received the raw JSON envelope instead of
    # the answer. Malformed usage must degrade ONLY the usage.
    for drifted_stats in (
        "not-a-dict",                                  # stats itself drifted
        {"models": "oops"},                            # models not a dict
        {"models": {"gemini-3.1-pro-preview": "hi"}},  # model entry not a dict
        {"models": {"gemini-3.1-pro-preview": {"tokens": "hi"}}},  # tokens not a dict
    ):
        envelope = json.dumps({"session_id": "s", "response": "PONG", "stats": drifted_stats})
        text, usage = consult_ai._parse_gemini_output(envelope, "")
        assert text == "PONG", f"answer lost for drifted stats {drifted_stats!r}"
        assert usage.usage_status == "unavailable"
        assert usage.tokens_in is None and usage.tokens_out is None


def test_gemini_non_string_response_degrades_to_empty_not_raw_envelope():
    envelope = json.dumps({"session_id": "s", "response": 42, "stats": {}})
    text, usage = consult_ai._parse_gemini_output(envelope, "")
    assert text == ""
    assert usage.usage_status == "unavailable"


def test_gemini_string_token_counts_are_coerced():
    # P3 regression: numeric-string counts coerce; junk degrades to partial.
    envelope = json.dumps({"response": "PONG", "stats": {"models": {
        "gemini-3.1-pro-preview": {"tokens": {"prompt": "450", "candidates": "12"}}}}})
    text, usage = consult_ai._parse_gemini_output(envelope, "")
    assert text == "PONG"
    assert usage.tokens_in == 450 and usage.tokens_out == 12

    envelope = json.dumps({"response": "PONG", "stats": {"models": {
        "gemini-3.1-pro-preview": {"tokens": {"prompt": "many", "candidates": 12}}}}})
    _text, usage = consult_ai._parse_gemini_output(envelope, "")
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


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


def test_claude_envelope_parses_but_usage_drift_keeps_answer_text():
    # @cw-trace verifies CTR-fh-011
    # P2 regression (PR #195 review): when the envelope parses but the
    # usage/modelUsage sections drift, the caller previously received the raw
    # JSON envelope instead of the answer.
    for drifted in (
        {"result": "PONG", "usage": "not-a-dict"},
        {"result": "PONG", "usage": {"input_tokens": 1, "output_tokens": 2},
         "modelUsage": "not-a-dict"},
        {"result": "PONG", "usage": {"input_tokens": 1, "output_tokens": 2},
         "modelUsage": {"claude-fable-5": "not-a-dict", "claude-haiku-4-5": "also-not"}},
    ):
        text, usage = consult_ai._parse_claude_output(json.dumps(drifted), "", None)
        assert text == "PONG", f"answer lost for drifted envelope {drifted!r}"
        # usage degrades (unavailable or tokens preserved with fallback model)
        # but the consult's product is never replaced by the raw envelope.
        assert usage.usage_status in ("provider-json", "unavailable")


def test_claude_non_string_result_degrades_to_empty_not_raw_envelope():
    envelope = json.dumps({"result": {"nested": "oops"}, "usage": {}})
    text, usage = consult_ai._parse_claude_output(envelope, "", None)
    assert text == ""
    assert usage.usage_status == "unavailable"


def test_claude_string_token_counts_are_coerced():
    # P3 regression: numeric-string counts coerce; junk degrades to partial.
    stdout = json.dumps({"result": "PONG", "usage": {"input_tokens": "2", "output_tokens": "14"}})
    text, usage = consult_ai._parse_claude_output(stdout, "", None)
    assert text == "PONG"
    assert usage.tokens_in == 2 and usage.tokens_out == 14

    stdout = json.dumps({"result": "PONG", "usage": {"input_tokens": "junk", "output_tokens": 14}})
    _text, usage = consult_ai._parse_claude_output(stdout, "", None)
    assert usage.usage_status == "partial"
    assert usage.tokens_in is None and usage.tokens_out is None


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


# --- real wall-clock deadline for the Vertex SDK call (chief-wiggum#330) -----
#
# consult_gemini_vertex used to accept `timeout` "for CLI signature parity"
# and never enforce it — the call is a synchronous SDK request with no
# subprocess to bound, so nothing stopped a hung client.models.generate_content()
# from blocking the calling thread (and thus the whole quorum, since
# gemini-vertex is REQUIRED in the `reviewer` role) forever.


def test_consult_gemini_vertex_enforces_a_wall_clock_deadline(monkeypatch):
    project_secret = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: project_secret.get(name))

    class _FakeModels:
        def generate_content(self, model, contents):
            time.sleep(30)  # simulate a hang far longer than the deadline below
            raise AssertionError("should never return — the deadline must fire first")

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    fake_genai = type("fake_genai_module", (), {"Client": _FakeClient})
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", type("fake_google_module", (), {"genai": fake_genai}))

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        consult_ai.consult_gemini_vertex("prompt", timeout=1)
    elapsed = time.monotonic() - start

    assert elapsed < 10, f"consult_gemini_vertex did not honor its deadline ({elapsed}s elapsed)"


def test_consult_gemini_vertex_returns_normally_well_within_its_deadline(monkeypatch):
    # The deadline machinery must not interfere with an ordinary fast call.
    project_secret = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: project_secret.get(name))

    class _FakeModels:
        def generate_content(self, model, contents):
            resp = _FakeVertexResponse(usage_metadata=None)
            resp.text = "PONG"
            resp.model_version = model
            return resp

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    fake_genai = type("fake_genai_module", (), {"Client": _FakeClient})
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", type("fake_google_module", (), {"genai": fake_genai}))

    text, usage = consult_ai.consult_gemini_vertex("prompt", timeout=30)
    assert text == "PONG"


# --- diff-scoped repo retrieval (chief-wiggum#319) ---------------------------
#
# consult_gemini_vertex used to call generate_content(contents=prompt) alone —
# cwd was accepted and never touched, so every call answered from the prompt
# text with zero repo access (the ticket's measured 165:1 tokens_in ratio vs
# codex on the same consult). These tests assert on what is actually SENT to
# the mocked SDK client — the one thing testable without live Vertex
# credentials — for both the diff-bearing case (retrieval should attach real
# file content) and the no-diff case (retrieval must NOT guess; it is an
# honest no-op, not a pretended fix).


def _diff_prompt(*paths: str) -> str:
    """A minimal review-shaped prompt embedding a unified diff header per
    path, matching what chief_wiggum.review.assemble_review_prompt's
    {{DIFF}} substitution actually produces."""
    header = "\n".join(f"diff --git a/{p} b/{p}\n@@ -1,1 +1,2 @@\n+touched" for p in paths)
    return "Review this change for correctness.\n\n" + header


def test_touched_files_from_diff_extracts_and_dedupes_paths():
    text = (
        "diff --git a/src/foo.py b/src/foo.py\n+x\n"
        "diff --git a/src/bar.py b/src/bar.py\n+y\n"
        "diff --git a/src/foo.py b/src/foo.py\n+z\n"  # duplicate header
    )
    assert consult_ai._touched_files_from_diff(text) == ["src/foo.py", "src/bar.py"]


def test_touched_files_from_diff_returns_empty_for_a_diffless_prompt():
    # An open-ended exploration prompt has no diff to bound retrieval from —
    # must yield nothing, never a guessed file selection.
    assert consult_ai._touched_files_from_diff("Explore this repo and report back.") == []


def test_read_touched_files_reads_current_content_from_cwd(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("SENTINEL_CONTENT_1234")

    blocks = consult_ai._read_touched_files(str(tmp_path), ["src/foo.py"])

    assert len(blocks) == 1
    assert "SENTINEL_CONTENT_1234" in blocks[0]
    assert "src/foo.py" in blocks[0]


def test_read_touched_files_skips_missing_file_without_raising(tmp_path):
    # The diff's a/-side (or a delete) may name a path gone from the worktree.
    blocks = consult_ai._read_touched_files(str(tmp_path), ["never/existed.py"])
    assert blocks == []


def test_read_touched_files_never_follows_a_path_that_escapes_cwd(tmp_path):
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("SHOULD_NEVER_BE_READ")
    repo = tmp_path / "repo"
    repo.mkdir()

    blocks = consult_ai._read_touched_files(str(repo), ["../outside_secret.txt"])

    assert blocks == []


def test_read_touched_files_truncates_an_oversized_file(tmp_path):
    big = "A" * (consult_ai.MAX_RETRIEVED_FILE_BYTES + 5_000)
    (tmp_path / "big.py").write_text(big)

    blocks = consult_ai._read_touched_files(str(tmp_path), ["big.py"])

    assert len(blocks) == 1
    assert "[truncated" in blocks[0]
    assert len(blocks[0].encode("utf-8")) < len(big)


def test_read_touched_files_caps_the_number_of_files_retrieved(tmp_path):
    paths = []
    for i in range(consult_ai.MAX_RETRIEVED_FILES + 10):
        p = tmp_path / f"f{i}.py"
        p.write_text(f"content {i}")
        paths.append(f"f{i}.py")

    blocks = consult_ai._read_touched_files(str(tmp_path), paths)

    assert len(blocks) == consult_ai.MAX_RETRIEVED_FILES


def _mock_vertex_sdk(monkeypatch, captured: dict):
    """Patch consult_gemini_vertex's google.genai import + secrets so
    generate_content's arguments can be inspected without live credentials."""
    project_secret = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: project_secret.get(name))

    class _FakeModels:
        def generate_content(self, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            resp = _FakeVertexResponse(
                usage_metadata=_FakeUsageMetadata(prompt_token_count=1, candidates_token_count=1),
                model_version=model,
            )
            resp.text = "a response"
            return resp

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    fake_genai = type("fake_genai_module", (), {"Client": _FakeClient})
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", type("fake_google_module", (), {"genai": fake_genai}))


def test_consult_gemini_vertex_attaches_touched_file_content_for_diff_bearing_prompt(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-010
    (tmp_path / "app.py").write_text("SENTINEL_REPO_CONTENT_ABC123")
    prompt = _diff_prompt("app.py")
    captured: dict = {}
    _mock_vertex_sdk(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    # The real fix: contents sent to the SDK carries the file's CURRENT
    # content, not just the prompt text (the pre-fix behavior, and the exact
    # gap the ticket's tokens_in evidence measured).
    assert "SENTINEL_REPO_CONTENT_ABC123" in captured["contents"]
    assert prompt in captured["contents"]
    assert len(captured["contents"]) > len(prompt)


def test_consult_gemini_vertex_is_an_honest_noop_without_a_diff(tmp_path, monkeypatch):
    # An open-ended prompt with no diff header has no bounded file set to
    # retrieve — contents must be the prompt UNCHANGED, never a guess, even
    # though cwd has real files sitting right there.
    (tmp_path / "app.py").write_text("unrelated content that must not leak in")
    prompt = "Explore this repo and report what you find."
    captured: dict = {}
    _mock_vertex_sdk(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    assert captured["contents"] == prompt


def test_consult_gemini_vertex_skips_retrieval_without_a_cwd(monkeypatch):
    # No cwd at all (the pre-#319 call shape, e.g. a bare CLI invocation with
    # no --cwd) — retrieval has nowhere to read from, so contents stays the
    # prompt alone rather than raising.
    prompt = _diff_prompt("app.py")
    captured: dict = {}
    _mock_vertex_sdk(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=None)

    assert captured["contents"] == prompt


# --- chief-wiggum#321: design_critic's screenshots reach gemini-vertex as ---
# --- real image bytes, not just filenames --------------------------------
#
# design_critic sends the SAME prompt every provider gets, but only NAMES the
# screenshot files — a CLI tool provider with real filesystem access via cwd
# can already open them itself (codex, claude-interactive); gemini-vertex's
# call path is a single non-agentic SDK request with no tool loop, so before
# this fix it never saw a single pixel. These tests assert on what is
# actually SENT to the mocked SDK — the multimodal ``contents`` list must
# carry real image Part objects with the real bytes, never a placeholder —
# and that a prompt naming no images is an honest no-op, exactly like #319's
# diff-less case.


class _FakePart:
    """Stand-in for ``google.genai.types.Part`` — records exactly what
    ``from_bytes`` was called with so a test can assert on it without a real
    SDK type."""

    def __init__(self, data: bytes, mime_type: str):
        self.data = data
        self.mime_type = mime_type

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> _FakePart:
        return cls(data, mime_type)


def _mock_vertex_sdk_with_types(monkeypatch, captured: dict):
    """Like ``_mock_vertex_sdk``, but the faked ``google.genai`` module also
    exposes a ``types`` submodule with a fake ``Part.from_bytes`` — needed
    only when a test expects the image-attachment path (``from google.genai
    import types``) to actually resolve."""
    project_secret = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: project_secret.get(name))

    class _FakeModels:
        def generate_content(self, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            resp = _FakeVertexResponse(
                usage_metadata=_FakeUsageMetadata(prompt_token_count=1, candidates_token_count=1),
                model_version=model,
            )
            resp.text = "a response"
            return resp

    class _FakeClient:
        def __init__(self, **kwargs):
            self.models = _FakeModels()

    fake_types = type("fake_types_module", (), {"Part": _FakePart})
    fake_genai = type("fake_genai_module", (), {"Client": _FakeClient, "types": fake_types})
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google", type("fake_google_module", (), {"genai": fake_genai}))


def test_image_paths_from_prompt_extracts_and_dedupes_named_screenshots():
    text = (
        "Critique these screenshots:\n"
        "- calm-home.png\n"
        "- calm-home.png\n"  # duplicate
        "- directions/bold/pricing.jpg\n"
        "Also see reference.PNG for the brand kit.\n"
    )
    assert consult_ai._image_paths_from_prompt(text) == [
        "calm-home.png", "directions/bold/pricing.jpg", "reference.PNG",
    ]


def test_image_paths_from_prompt_returns_empty_for_an_image_less_prompt():
    assert consult_ai._image_paths_from_prompt("Review this diff for correctness.") == []


def test_read_touched_images_reads_bytes_and_mime_from_cwd(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nSENTINEL_PIXELS"
    (tmp_path / "shot.png").write_bytes(png_bytes)

    images = consult_ai._read_touched_images(str(tmp_path), ["shot.png"])

    assert images == [("shot.png", png_bytes, "image/png")]


def test_read_touched_images_skips_missing_file_without_raising(tmp_path):
    images = consult_ai._read_touched_images(str(tmp_path), ["never/existed.png"])
    assert images == []


def test_read_touched_images_never_follows_a_path_that_escapes_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside_secret.png").write_bytes(b"secret pixels")

    images = consult_ai._read_touched_images(str(repo), ["../outside_secret.png"])

    assert images == []


def test_read_touched_images_drops_an_oversized_image_rather_than_truncating(tmp_path):
    big = b"\x00" * (consult_ai.MAX_RETRIEVED_IMAGE_BYTES + 1)
    (tmp_path / "huge.png").write_bytes(big)

    images = consult_ai._read_touched_images(str(tmp_path), ["huge.png"])

    assert images == []


def test_read_touched_images_caps_the_number_of_images_retrieved(tmp_path):
    paths = []
    for i in range(consult_ai.MAX_RETRIEVED_IMAGES + 10):
        name = f"shot{i}.png"
        (tmp_path / name).write_bytes(b"x")
        paths.append(name)

    images = consult_ai._read_touched_images(str(tmp_path), paths)

    assert len(images) == consult_ai.MAX_RETRIEVED_IMAGES


def test_read_touched_images_ignores_a_non_image_extension(tmp_path):
    (tmp_path / "notes.txt") .write_bytes(b"not an image")
    images = consult_ai._read_touched_images(str(tmp_path), ["notes.txt"])
    assert images == []


def test_consult_gemini_vertex_attaches_real_image_bytes_for_a_prompt_naming_screenshots(tmp_path, monkeypatch):
    # @cw-trace verifies CTR-fh-010
    png_bytes = b"\x89PNG\r\n\x1a\nSENTINEL_PIXELS_XYZ"
    (tmp_path / "calm-home.png").write_bytes(png_bytes)
    prompt = "Critique this screenshot against WCAG AA: calm-home.png"
    captured: dict = {}
    _mock_vertex_sdk_with_types(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    # contents must be a multimodal list: the text, plus a real image part
    # carrying the ACTUAL bytes read from disk — never a filename-only stub.
    contents = captured["contents"]
    assert isinstance(contents, list)
    assert contents[0] == prompt
    image_parts = [p for p in contents[1:] if isinstance(p, _FakePart)]
    assert len(image_parts) == 1
    assert image_parts[0].data == png_bytes
    assert image_parts[0].mime_type == "image/png"


def test_consult_gemini_vertex_image_attachment_is_an_honest_noop_without_named_images(tmp_path, monkeypatch):
    # A prompt naming no images (every non-design_critic role today) must
    # behave EXACTLY as before this ticket — contents stays the plain prompt
    # string, never a list, even though cwd has real image files sitting
    # right there.
    (tmp_path / "unrelated.png").write_bytes(b"must not leak in")
    prompt = "Review this diff for correctness.\n\ndiff --git a/x.py b/x.py\n+y"
    captured: dict = {}
    _mock_vertex_sdk(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    assert not isinstance(captured["contents"], list)


def test_consult_gemini_vertex_image_attachment_noop_when_named_file_does_not_exist(tmp_path, monkeypatch):
    # The prompt names a screenshot, but it isn't actually sitting under cwd
    # (e.g. a stale reference) — best-effort retrieval finds nothing to
    # attach, so contents stays the prompt alone rather than guessing.
    prompt = "Critique this screenshot: missing.png"
    captured: dict = {}
    _mock_vertex_sdk(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    assert captured["contents"] == prompt


def test_consult_gemini_vertex_combines_diff_text_retrieval_and_image_retrieval(tmp_path, monkeypatch):
    # The two retrieval paths (#319's diff-file text, #321's images) are
    # independent and additive — a prompt that happens to carry both a diff
    # header and an image filename gets both kinds of grounding.
    (tmp_path / "app.py").write_text("SENTINEL_REPO_CONTENT_ABC123")
    png_bytes = b"\x89PNG\r\nSENTINEL_PIXELS"
    (tmp_path / "shot.png").write_bytes(png_bytes)
    prompt = _diff_prompt("app.py") + "\nAlso see shot.png for the rendered result."
    captured: dict = {}
    _mock_vertex_sdk_with_types(monkeypatch, captured)

    consult_ai.consult_gemini_vertex(prompt, cwd=str(tmp_path))

    contents = captured["contents"]
    assert isinstance(contents, list)
    assert "SENTINEL_REPO_CONTENT_ABC123" in contents[0]
    image_parts = [p for p in contents[1:] if isinstance(p, _FakePart)]
    assert len(image_parts) == 1
    assert image_parts[0].data == png_bytes


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

    def fake_tool(prompt, model=None, cwd=None, timeout=None):
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

    def fake_consult_provider(provider, prompt_text, model, cwd, ticket=None, timeout_override=None):
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


# ---- openrouter provider (frontier non-Western models for quorum entropy) -------


def _openrouter_payload(content="answer", model="deepseek/deepseek-v4-pro", **usage):
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, **usage},
    }


def test_openrouter_parses_text_and_usage():
    text, usage = consult_ai._parse_openrouter_payload(_openrouter_payload(), None)
    assert text == "answer"
    assert (usage.tokens_in, usage.tokens_out) == (100, 200)
    assert usage.usage_status == "provider-json"
    assert usage.resolved_model == "deepseek/deepseek-v4-pro"


def test_openrouter_prefers_payload_model_over_requested():
    """OpenRouter may route elsewhere; the BILLED id is what telemetry must price."""
    payload = _openrouter_payload(model="deepseek/deepseek-v4-flash")
    _, usage = consult_ai._parse_openrouter_payload(payload, "deepseek/deepseek-v4-pro")
    assert usage.resolved_model == "deepseek/deepseek-v4-flash"


def test_openrouter_partial_usage_nulls_both_tokens():
    """INV-fh-011: never fabricate the missing half of a token pair."""
    payload = _openrouter_payload()
    del payload["usage"]["completion_tokens"]
    _, usage = consult_ai._parse_openrouter_payload(payload, None)
    assert (usage.tokens_in, usage.tokens_out) == (None, None)
    assert usage.usage_status == "partial"


def test_openrouter_missing_usage_is_unavailable_not_zero():
    payload = _openrouter_payload()
    del payload["usage"]
    _, usage = consult_ai._parse_openrouter_payload(payload, None)
    assert usage.usage_status == "unavailable"
    assert (usage.tokens_in, usage.tokens_out) == (None, None)


def test_openrouter_falls_back_to_reasoning_when_content_empty():
    """Reasoning models sometimes leave `content` empty — an answer is still an answer."""
    payload = _openrouter_payload(content="")
    payload["choices"][0]["message"]["reasoning"] = "the real answer"
    text, _ = consult_ai._parse_openrouter_payload(payload, None)
    assert text == "the real answer"


def test_openrouter_requires_explicit_model(monkeypatch):
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: "sk-test")
    with pytest.raises(ValueError, match="explicit model"):
        consult_ai.consult_openrouter("prompt", model=None)


def test_openrouter_requires_key_in_keyring(monkeypatch):
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: None)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        consult_ai.consult_openrouter("prompt", model="deepseek/deepseek-v4-pro")


def test_openrouter_raises_on_error_object_in_200_response(monkeypatch):
    """A provider-level failure arrives as HTTP 200 with an `error` body."""
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: "sk-test")

    class FakeResponse:
        def read(self):
            return json.dumps({"error": {"message": "upstream is down"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(consult_ai.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(RuntimeError, match="upstream is down"):
        consult_ai.consult_openrouter("prompt", model="deepseek/deepseek-v4-pro")


def test_openrouter_key_is_sent_as_header_not_env(monkeypatch):
    """CLAUDE.md secret policy: fetched at call time, passed to the request, never env."""
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: "sk-secret")
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps(_openrouter_payload()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(consult_ai.urllib.request, "urlopen", fake_urlopen)
    consult_ai.consult_openrouter("a prompt", model="deepseek/deepseek-v4-pro")

    assert captured["auth"] == "Bearer sk-secret"
    assert captured["body"]["model"] == "deepseek/deepseek-v4-pro"
    assert captured["body"]["messages"][0]["content"] == "a prompt"
    import os
    assert "OPENROUTER_API_KEY" not in os.environ


def test_openrouter_is_a_registered_tool_with_a_timeout():
    assert consult_ai.TOOLS["openrouter"] is consult_ai.consult_openrouter
    assert consult_ai.ADAPTER_BY_TOOL["openrouter"] == "openrouter-api"
    assert consult_ai.TOOL_TIMEOUTS["openrouter"] > 0


def test_shipped_provider_config_is_valid_including_openrouter_entries():
    """The divergence role must resolve, and its opt-in entropy providers must
    NOT leak into the default roles. ``deepseek-flash`` is the one sanctioned
    exception: it sits in the code quorum by explicit operator decision
    (replacing gemini-vertex), for cost/speed — not as distribution entropy."""
    from pathlib import Path as _Path

    import providers as providers_mod

    config = providers_mod.load_config(_Path(__file__).resolve().parents[1] / "config" / "providers.json")
    errors = providers_mod.validate_config(
        config,
        supported_tools=set(consult_ai.TOOLS),
        supported_delegates={"claude-interactive"},
    )
    assert errors == []

    roles = providers_mod.roles_from_config(config)
    assert "divergence" in roles
    openrouter_names = {
        name for name, p in providers_mod.providers_from_config(config).items()
        if p.tool == "openrouter"
    }
    assert openrouter_names, "expected openrouter-backed providers"
    entropy_only = openrouter_names - {"deepseek-flash"}
    for role_name, role in roles.items():
        if role_name == "divergence":
            continue
        assert not (set(role.required) | set(role.optional)) & entropy_only, (
            f"role {role_name} pulls in opt-in entropy providers"
        )


def test_openrouter_enforces_a_wall_clock_deadline(monkeypatch):
    """urllib's timeout bounds each socket op, not total elapsed — a slow-but-steady
    stream never trips it. Observed live at ~30 min against a 300s budget."""
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: "sk-test")
    monkeypatch.setitem(consult_ai.TOOL_TIMEOUTS, "openrouter", 1)

    def never_returns(request, timeout=None):
        _time.sleep(30)
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(consult_ai.urllib.request, "urlopen", never_returns)
    started = _time.monotonic()
    with pytest.raises(TimeoutError, match="budget"):
        consult_ai.consult_openrouter("prompt", model="deepseek/deepseek-v4-pro")
    assert _time.monotonic() - started < 10, "deadline did not actually fire"


def test_openrouter_deadline_reraises_the_original_error(monkeypatch):
    """A fast failure must surface as itself, not as a timeout."""
    monkeypatch.setattr(consult_ai, "get_secret", lambda name: "sk-test")

    def boom(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(consult_ai.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        consult_ai.consult_openrouter("prompt", model="deepseek/deepseek-v4-pro")


# ---- tool_timeout override chain (chief-wiggum#291) -------------------------
#
# TOOL_TIMEOUTS was hardcoded with no CLI/env lever: a large prompt against a
# large repo legitimately exceeds the default, and /implement's own rule
# forbids proceeding without a completed consult — retrying the identical call
# against the identical timeout just times out again. tool_timeout() resolves
# a single override chain: explicit override > CW_CONSULT_TIMEOUT_<TOOL> >
# CW_CONSULT_TIMEOUT > the TOOL_TIMEOUTS table.


def _clear_consult_timeout_env(monkeypatch):
    for name in ("CW_CONSULT_TIMEOUT", "CW_CONSULT_TIMEOUT_CODEX",
                 "CW_CONSULT_TIMEOUT_GEMINI_VERTEX", "CW_CONSULT_TIMEOUT_CLAUDE_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


def test_tool_timeout_defaults_to_the_table(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    assert consult_ai.tool_timeout("codex") == consult_ai.TOOL_TIMEOUTS["codex"]


def test_tool_timeout_unlisted_tool_falls_back_to_module_constant(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    assert consult_ai.tool_timeout("some-unlisted-tool") == consult_ai.TIMEOUT


def test_tool_timeout_specific_env_var_overrides_table(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "999")
    assert consult_ai.tool_timeout("codex") == 999


def test_tool_timeout_general_env_var_applies_when_no_specific_override(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT", "111")
    assert consult_ai.tool_timeout("codex") == 111
    assert consult_ai.tool_timeout("gemini") == 111


def test_tool_timeout_specific_env_var_wins_over_general(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT", "111")
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "222")
    assert consult_ai.tool_timeout("codex") == 222
    # a tool without its own specific var still falls through to the general one
    assert consult_ai.tool_timeout("gemini") == 111


def test_tool_timeout_explicit_override_wins_over_both_env_vars(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT", "111")
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "222")
    assert consult_ai.tool_timeout("codex", override=333) == 333


def test_tool_timeout_env_var_name_sanitizes_non_alphanumerics(monkeypatch):
    """``gemini-vertex`` -> ``CW_CONSULT_TIMEOUT_GEMINI_VERTEX`` (hyphen -> underscore)."""
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_GEMINI_VERTEX", "444")
    assert consult_ai.tool_timeout("gemini-vertex") == 444


def test_tool_timeout_non_numeric_env_value_falls_through(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "not-a-number")
    assert consult_ai.tool_timeout("codex") == consult_ai.TOOL_TIMEOUTS["codex"]


def test_tool_timeout_non_positive_env_value_falls_through(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "0")
    assert consult_ai.tool_timeout("codex") == consult_ai.TOOL_TIMEOUTS["codex"]
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "-5")
    assert consult_ai.tool_timeout("codex") == consult_ai.TOOL_TIMEOUTS["codex"]


def test_tool_timeout_specific_invalid_falls_through_to_general(monkeypatch):
    """A malformed SPECIFIC var must fall through to the GENERAL var, not
    straight to the table — the chain degrades one rung at a time."""
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "garbage")
    monkeypatch.setenv("CW_CONSULT_TIMEOUT", "77")
    assert consult_ai.tool_timeout("codex") == 77


def test_tool_timeout_invalid_override_falls_through_not_raises(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "555")
    # A non-positive explicit override (e.g. a stray --timeout 0) must be
    # ignored, not crash a consult mid-workflow.
    assert consult_ai.tool_timeout("codex", override=0) == 555
    assert consult_ai.tool_timeout("codex", override=-10) == 555


def test_tool_timeout_string_override_is_validated_like_env(monkeypatch):
    """``_timeout_arg``'s argparse-level parsing degrades a bad --timeout to
    None before it ever reaches tool_timeout; this pins the same tolerance at
    the tool_timeout() layer directly for a caller that passes one through."""
    _clear_consult_timeout_env(monkeypatch)
    assert consult_ai._timeout_arg("not-a-number") is None
    assert consult_ai._timeout_arg("0") is None
    assert consult_ai._timeout_arg("450") == 450


def test_consult_codex_resolves_timeout_through_the_chain(monkeypatch):
    """Integration: consult_codex's _run_capture call receives whatever
    tool_timeout() resolves, not a hardcoded TOOL_TIMEOUTS read."""
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "123")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return "", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    consult_ai.consult_codex("prompt")
    assert captured["timeout"] == 123


def test_consult_codex_explicit_timeout_param_wins_over_env(monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CODEX", "123")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return "", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    consult_ai.consult_codex("prompt", timeout=999)
    assert captured["timeout"] == 999


def test_claude_interactive_explicit_timeout_still_wins_over_env_override(tmp_path, monkeypatch):
    """Composition with the role's optional-provider cap (chief-wiggum#188): the
    role quorum threads its cap through consult_claude_interactive's existing
    ``timeout`` parameter, which must remain the highest-precedence override
    even when an env var is ALSO set — the env-var lever must never fight the
    per-role cap."""
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CLAUDE_INTERACTIVE", "999")
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path), timeout=42)
    assert captured["timeout"] == 72  # override (42) + 30s grace buffer


def test_claude_interactive_env_var_applies_when_no_explicit_override(tmp_path, monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT_CLAUDE_INTERACTIVE", "555")
    result_file = tmp_path / "result.md"
    result_file.write_text("delegate response")
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return f"RESULT={result_file}\n", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    consult_ai.consult_claude_interactive("prompt", cwd=str(tmp_path))
    assert captured["timeout"] == 585  # env (555) + 30s grace buffer


def test_role_quorum_optional_capping_still_behaves_as_before(tmp_path, monkeypatch):
    """AC4 regression pin: the --role optional-provider timeout cap
    (chief-wiggum#188) must be unaffected by the new override chain, composing
    with it rather than fighting it (per the issue's own note) — this is the
    SAME scenario as the pre-existing
    test_role_quorum_skips_hung_optional_delegate_fast_and_still_succeeds,
    re-asserted here with a CW_CONSULT_TIMEOUT env var ALSO set.

    A general env var legitimately applies to EVERY tool call (that's the
    documented, intentional scope of CW_CONSULT_TIMEOUT — codex's required
    call picks it up too, since it goes through the SAME tool_timeout()
    resolution as everything else). What must NOT change is the optional
    delegate's own cap: the role quorum threads its cap into
    consult_claude_interactive as an explicit ``timeout`` override, which is
    the highest-precedence rung of the chain — so it keeps winning over the
    env var for that ONE provider, exactly as before #291.
    """
    _clear_consult_timeout_env(monkeypatch)
    monkeypatch.setenv("CW_CONSULT_TIMEOUT", "50000")  # a value nothing else could produce by accident
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    config = tmp_path / "providers.json"
    output_dir = tmp_path / "out"
    write_config_with_delegate(config)

    captured_timeouts: dict[str, int] = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured_timeouts[tool] = timeout
        if tool == "codex":
            return "a substantive codex response, long enough to pass validation", ""
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(
        sys, "argv",
        ["consult_ai.py", "--role", "reviewer", str(prompt), "--config", str(config),
         "--output-dir", str(output_dir), "--min-bytes", "1"],
    )

    consult_ai.main()

    manifest = json.loads((output_dir / "reviewer-manifest.json").read_text())
    assert manifest["ok"] is True
    # codex (required, tool-type) has no override threaded in from consult_provider
    # (unchanged, #278 review) — it resolves the general env var like any direct call.
    assert captured_timeouts["codex"] == 50000
    # claude-interactive (optional, delegate-type) still gets the role's OWN cap
    # (+30s grace) — the explicit override from optional_provider_timeout beats
    # the env var, so the #188 fail-fast guarantee is untouched by #291.
    assert captured_timeouts["claude-interactive"] == consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS + 30


def test_cli_timeout_flag_overrides_the_table_for_single_tool_mode(tmp_path, monkeypatch):
    _clear_consult_timeout_env(monkeypatch)
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return "a substantive codex response", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(
        sys, "argv", ["consult_ai.py", "codex", str(prompt), "--timeout", "1234"],
    )
    consult_ai.main()
    assert captured["timeout"] == 1234


def test_cli_timeout_flag_invalid_value_falls_through_not_raises(tmp_path, monkeypatch):
    """A malformed --timeout must degrade to the next source (env/table),
    never crash the CLI invocation (AC2)."""
    _clear_consult_timeout_env(monkeypatch)
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT_TEXT)
    captured = {}

    def fake_run_capture(cmd, *, input_text, timeout, cwd, tool, check=True):
        captured["timeout"] = timeout
        return "a substantive codex response", ""

    monkeypatch.setattr(consult_ai, "_run_capture", fake_run_capture)
    monkeypatch.setattr(
        sys, "argv", ["consult_ai.py", "codex", str(prompt), "--timeout", "not-a-number"],
    )
    consult_ai.main()  # must not raise
    assert captured["timeout"] == consult_ai.TOOL_TIMEOUTS["codex"]

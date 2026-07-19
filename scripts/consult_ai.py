#!/usr/bin/env python3
"""
Consult an AI tool with a prompt and capture its output.

Secrets are fetched from the system keyring at call time and passed directly
to SDK constructors — never set as env vars, never printed.

Usage:
    python3 consult_ai.py <tool> <prompt_file> [--output <file>] [--context <file>] [--model <model_id>]
    python3 consult_ai.py --role <role> <prompt_file> --output-dir <dir>

Tools: codex, gemini, gemini-vertex, claude
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Allow importing keychain from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from keychain import get_secret
from providers import (
    DEFAULT_CONFIG,
    DEFAULT_LENSES,
    Provider,
    load_config,
    load_lenses,
    plan_role,
    prompt_for_provider,
    run_role_quorum,
    validate_config,
    validate_lenses,
)

# Per-tool timeouts (seconds). These are generous — better to wait than to
# lose a good response to a premature timeout.
TOOL_TIMEOUTS: dict[str, int] = {
    "codex": 600,       # 10 minutes — xhigh reasoning is slow on large prompts
    "gemini": 1200,     # 20 minutes — yolo mode explores the repo via tools
    "gemini-vertex": 600,
    "claude": 600,
    "claude-interactive": 1800,
}
TIMEOUT = 600  # fallback

# Interval (seconds) between liveness heartbeats emitted to stderr while a provider CLI
# runs. A silent multi-minute consult is indistinguishable from a hang to a worker's
# stream-watchdog; a periodic line proves the consult is alive and progressing.
HEARTBEAT_INTERVAL = 30

# Default model for Vertex AI path (override with --model)
DEFAULT_VERTEX_MODEL = "gemini-3.1-pro-preview"

# A prompt file smaller than this is almost never intentional — it's the
# signature of a truncated write (a template substitution that silently
# produced nothing, an interrupted heredoc, etc). Live use burned a codex
# call and an opus agent run on exactly this (chief-wiggum#163); refuse
# before any provider is called rather than spend a slow, expensive
# consultation on a prompt that was never meant to be submitted.
MIN_PROMPT_BYTES = 200


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group, so the provider CLI **and
    any subprocesses it spawned** die — not just the direct child. This is the crux of
    the hang fix: a surviving grandchild that inherited the stdout pipe keeps
    communicate() blocked forever otherwise."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue  # escalate to SIGKILL


def _run_capture(
    cmd: list[str], *, input_text: str | None, timeout: int, cwd: str | None, tool: str,
    check: bool = True,
) -> str:
    """Run a provider CLI, capturing stdout, with a HARD timeout that actually fires.

    ``subprocess.run(timeout=...)`` kills only the direct child; if the CLI spawned
    grandchildren holding the stdout pipe open, the follow-up ``communicate()`` blocks
    reading that pipe until they exit — so the "timeout" never returns and the calling
    worker hangs (the root cause of consult-driven stalls, #95). Here the CLI runs in
    its OWN session/process group (``start_new_session``) and a timeout kills the whole
    group, guaranteeing control returns within ``timeout``. A daemon thread emits a
    stderr heartbeat so a long-but-live consult is not mistaken for a hang.

    Raises ``subprocess.TimeoutExpired`` / ``subprocess.CalledProcessError`` to preserve
    the previous ``subprocess.run(check=True, timeout=...)`` contract.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, start_new_session=True,
    )
    stop = threading.Event()

    def _heartbeat() -> None:
        start = time.monotonic()
        while not stop.wait(HEARTBEAT_INTERVAL):
            elapsed = int(time.monotonic() - start)
            print(f"[consult:{tool}] still running ({elapsed}s / {timeout}s budget)",
                  file=sys.stderr, flush=True)

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    try:
        out, err = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=10)  # drain now-closed pipes; group is dead
        except Exception:
            pass
        raise
    finally:
        stop.set()
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=err)
    return out


def consult_codex(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Call codex CLI in read-only sandbox. Uses its own auth session.

    Passes prompt via stdin (``-``) to avoid shell argument length issues
    and to match how codex exec expects large prompts.

    Overrides reasoning effort to ``high`` (instead of user's default which
    may be ``xhigh``) to keep response times reasonable for consultations.
    """
    cmd = [
        "codex", "exec", "--sandbox", "read-only",
        "-c", 'model_reasoning_effort="high"',
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")  # read prompt from stdin
    return _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("codex", TIMEOUT),
        cwd=cwd, tool="codex",
    )


def consult_gemini(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Call gemini CLI. Uses its own auth session.

    Passes prompt via stdin to avoid shell argument length issues.
    Uses --yolo to auto-approve all tool use (required for non-interactive
    subprocess execution — without it gemini blocks on approval prompts).
    """
    cmd = ["gemini", "--yolo", "--output-format", "text", "-p", ""]
    if model:
        cmd.extend(["-m", model])
    return _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("gemini", TIMEOUT),
        cwd=cwd, tool="gemini",
    )


def consult_gemini_vertex(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Call Gemini via Vertex AI SDK. Fetches credentials from keyring."""
    project = get_secret("GOOGLE_CLOUD_PROJECT")
    location = get_secret("GOOGLE_CLOUD_LOCATION") or "us-central1"

    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT not found in keyring. "
              "Run: python3 scripts/keychain.py set GOOGLE_CLOUD_PROJECT",
              file=sys.stderr)
        sys.exit(1)

    # Import here so the dependency is only needed for this path
    from google.cloud import aiplatform  # type: ignore
    from vertexai.generative_models import GenerativeModel  # type: ignore

    aiplatform.init(project=project, location=location)
    model_id = model or DEFAULT_VERTEX_MODEL
    gen_model = GenerativeModel(model_id)
    response = gen_model.generate_content(prompt)
    return response.text


def consult_claude(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Call claude CLI. Uses its own auth session."""
    cmd = ["claude", "-p", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    return _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("claude", TIMEOUT),
        cwd=cwd, tool="claude",
    )


def consult_claude_interactive(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Delegate to the interactive Claude tmux provider."""
    if model:
        print("Warning: --model is ignored for claude-interactive", file=sys.stderr)
    script = Path(__file__).resolve().parents[1] / "skills" / "claude-interactive-delegate" / "scripts" / "claude_delegate.py"
    fd, prompt_name = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    prompt_file = Path(prompt_name)
    try:
        prompt_file.write_text(prompt)
        cmd = [
            sys.executable,
            str(script),
            "submit",
            "--prompt-file",
            str(prompt_file),
            "--wait",
        ]
        if cwd:
            cmd.extend(["--cwd", cwd])
        stdout = _run_capture(
            cmd, input_text=None, timeout=TOOL_TIMEOUTS["claude-interactive"],
            cwd=None, tool="claude-interactive",
        )
        for line in stdout.splitlines():
            if line.startswith("RESULT="):
                result_path = Path(line.removeprefix("RESULT="))
                if result_path.exists():
                    return result_path.read_text()
                raise RuntimeError(f"claude-interactive result path does not exist: {result_path}")
        raise RuntimeError(f"claude-interactive completed without RESULT line: {stdout}")
    finally:
        prompt_file.unlink(missing_ok=True)


TOOLS = {
    "codex": consult_codex,
    "gemini": consult_gemini,
    "gemini-vertex": consult_gemini_vertex,
    "claude": consult_claude,
    "claude-interactive": consult_claude_interactive,
}


def _emit_consult_telemetry(provider_label: str, model: str | None, cwd: str | None) -> None:
    """Best-effort factory telemetry for a consult. No-op unless telemetry is enabled
    (CW_TELEMETRY / CW_FACTORY_LOG); never breaks the consult. Records provider +
    model + repo now; per-provider token/cost capture is tracked in chief-wiggum#134.
    """
    try:
        import os
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        import factory_log
        repo = os.path.basename(os.path.abspath(cwd)) if cwd else None
        factory_log.emit_consult(provider_label, model, repo=repo)
    except Exception:
        pass


def consult_provider(provider: Provider, prompt: str, model: str | None, cwd: str | None) -> str:
    if provider.type == "tool":
        if not provider.tool or provider.tool not in TOOLS:
            raise ValueError(f"unsupported tool provider: {provider.name}")
        result = TOOLS[provider.tool](prompt, model=model, cwd=cwd)
        _emit_consult_telemetry(provider.tool, model, cwd)
        return result
    if provider.type == "delegate":
        if provider.delegate != "claude-interactive":
            raise ValueError(f"unsupported delegate provider: {provider.name}")
        result = consult_claude_interactive(prompt, model=model, cwd=cwd)
        _emit_consult_telemetry("claude-interactive", model, cwd)
        return result
    raise ValueError(f"unsupported provider type: {provider.type}")


def main():
    parser = argparse.ArgumentParser(
        description="Consult an AI tool with a prompt.",
    )
    parser.add_argument("target_or_prompt", help="AI tool name, or prompt file when --role is used")
    parser.add_argument("prompt_file", nargs="?", help="Path to the prompt file")
    parser.add_argument("-o", "--output", help="Write response to file instead of stdout")
    parser.add_argument("--output-dir", help="Write role provider responses to this directory")
    parser.add_argument("--context", help="Optional context file to append")
    parser.add_argument("--model", help="Override model ID for this call")
    parser.add_argument("--cwd", help="Working directory for the AI tool (e.g., target repo path)")
    parser.add_argument("--role", help="Provider role to consult from config/providers.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Provider config path")
    parser.add_argument(
        "--lenses-config", default=str(DEFAULT_LENSES),
        help="Review-lens charter config path (config/lenses.json)",
    )
    parser.add_argument("--enable-provider", action="append", default=[], help="Force-enable provider by name")
    parser.add_argument("--disable-provider", action="append", default=[], help="Disable provider by name")
    parser.add_argument("--max-attempts", type=int, default=2, help="Total attempts for required providers in --role mode (incl. first try)")
    parser.add_argument("--min-bytes", type=int, default=20, help="Minimum substantive output size in --role mode")
    args = parser.parse_args()

    if args.role:
        target = None
        prompt_file_arg = args.target_or_prompt
    else:
        target = args.target_or_prompt
        prompt_file_arg = args.prompt_file
        if target not in TOOLS:
            parser.error(f"unknown tool {target!r}; expected one of: {', '.join(sorted(TOOLS))}")
        if not prompt_file_arg:
            parser.error("<prompt_file> is required when consulting a tool")
    if args.role and not args.output_dir:
        parser.error("--role requires --output-dir")
    if args.role and args.output:
        parser.error("--role writes one file per provider and requires --output-dir, not -o/--output")

    prompt_path = Path(prompt_file_arg)
    if not prompt_path.exists():
        print(f"Prompt file not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)

    prompt = prompt_path.read_text()

    if args.context:
        ctx_path = Path(args.context)
        if ctx_path.exists():
            prompt += f"\n\n---\nContext:\n{ctx_path.read_text()}"

    # Guard the FINAL assembled prompt (prompt file + any --context), so a
    # legitimately small prompt file paired with substantive context is
    # accepted — but always BEFORE any provider is called.
    prompt_bytes = len(prompt.strip().encode("utf-8"))
    if prompt_bytes < MIN_PROMPT_BYTES:
        print(
            f"Error: assembled prompt from {prompt_path} is only {prompt_bytes} "
            f"bytes (minimum {MIN_PROMPT_BYTES}) — refusing to consult. This is "
            "the signature of a truncated or empty prompt; fix the prompt before "
            "spending a provider call on it.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.role:
        config = load_config(Path(args.config))
        errors = validate_config(
            config,
            supported_tools=set(TOOLS),
            supported_delegates={"claude-interactive"},
        )
        if errors:
            for error in errors:
                print(f"Config error: {error}", file=sys.stderr)
            sys.exit(1)
        lenses = load_lenses(Path(args.lenses_config))
        lens_errors = validate_lenses(config, lenses)
        if lens_errors:
            for error in lens_errors:
                print(f"Config error: {error}", file=sys.stderr)
            sys.exit(1)
        try:
            plan = plan_role(
                args.role,
                config,
                enabled=set(args.enable_provider),
                disabled=set(args.disable_provider),
            )
        except KeyError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if not plan.ok:
            print(
                f"Missing required providers for role {args.role}: {', '.join(plan.missing_required)}",
                file=sys.stderr,
            )
            sys.exit(1)
        # Run the quorum in parallel with retries + output validation, and write
        # a manifest. Required providers must produce substantive output. Every
        # provider gets the identical shared prompt; a provider mapped to a lens
        # (config/providers.json role.lenses) additionally gets its charter
        # appended (chief-wiggum#163) — the shared body itself never changes.
        def execute(provider: Provider) -> str:
            provider_prompt = prompt_for_provider(plan.role, provider.name, prompt, lenses)
            return consult_provider(provider, provider_prompt, args.model, args.cwd)

        manifest = run_role_quorum(
            plan,
            execute,
            args.output_dir,
            max_attempts=args.max_attempts,
            min_bytes=args.min_bytes,
        )
        for result in manifest.results:
            if result.status == "ok":
                print(f"OK: {result.name} response written to {result.path}")
            elif result.required:
                print(f"Error: required provider {result.name} failed: {result.error}", file=sys.stderr)
            else:
                print(f"Warning: optional provider {result.name} failed: {result.error}", file=sys.stderr)
        if not manifest.ok:
            print(
                f"Role {args.role} quorum failed: {', '.join(manifest.failed_required)}",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    assert target is not None
    fn = TOOLS[target]
    tool_timeout = TOOL_TIMEOUTS.get(target, TIMEOUT)
    out_path = Path(args.output) if args.output else None
    if out_path:
        # Create missing parent directories up front so writing the response —
        # success OR failure message — never fails with FileNotFoundError.
        out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output = fn(prompt, model=args.model, cwd=args.cwd)
        if out_path:
            out_path.write_text(output)
            print(f"OK: {target} response written to {args.output}")
        else:
            print(output)
    except subprocess.TimeoutExpired:
        msg = f"Timeout: {target} did not respond within {tool_timeout}s"
        if out_path:
            out_path.write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        msg = f"Error calling {target}: {e.stderr or e}"
        if out_path:
            out_path.write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        msg = f"Error: {e}"
        if out_path:
            out_path.write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

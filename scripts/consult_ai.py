#!/usr/bin/env python3
"""
Consult an AI tool with a prompt and capture its output.

Secrets are fetched from the system keyring at call time and passed directly
to SDK constructors — never set as env vars, never printed.

Usage:
    python3 consult_ai.py <tool> <prompt_file> [--output <file>] [--context <file>] [--model <model_id>] [--ticket <n>]
    python3 consult_ai.py --role <role> <prompt_file> --output-dir <dir> [--ticket <n>]

Tools: codex, gemini, gemini-vertex, claude, claude-interactive

Each consult_* function returns ``(text, Usage)`` — the response text plus a
best-effort per-provider token/model usage summary (chief-wiggum#134). A
successful consult always emits a ``factory_log`` 'consult' telemetry event
(no-op unless CW_TELEMETRY/CW_FACTORY_LOG is set) carrying that usage; cost is
derived exclusively inside ``factory_log.emit_consult`` from
``config/model_pricing.json`` — never computed here.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
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

# Default wall-clock budget (seconds) for the claude-interactive delegate when it is
# running in an OPTIONAL role slot, used when the role doesn't set its own
# ``optional_timeout_seconds`` (chief-wiggum#188). claude-interactive timed out at its
# full 1800s budget on two consecutive large-prompt consults while contributing
# nothing — since it is never a role's required voice, there is no reason a role's
# wall-clock (required providers finish in 10-20 minutes) should be held hostage to a
# voice that's allowed to fail. Deliberately shorter than every required TOOL_TIMEOUTS
# entry: an optional provider should fail fast, not merely "less slow".
DEFAULT_OPTIONAL_TIMEOUT_SECONDS = 300

# Default model for Vertex AI path (override with --model)
DEFAULT_VERTEX_MODEL = "gemini-3.1-pro-preview"

# A prompt file smaller than this is almost never intentional — it's the
# signature of a truncated write (a template substitution that silently
# produced nothing, an interrupted heredoc, etc). Live use burned a codex
# call and an opus agent run on exactly this (chief-wiggum#163); refuse
# before any provider is called rather than spend a slow, expensive
# consultation on a prompt that was never meant to be submitted.
MIN_PROMPT_BYTES = 200


@dataclass
class Usage:
    """Per-consult usage summary threaded from a provider parser to
    ``factory_log.emit_consult`` (chief-wiggum#134).

    ``tokens_in``/``tokens_out`` obey both-tokens-or-null (INV-fh-011): a
    parser that only recovered ONE of the two counts must return both as
    ``None`` (never fabricate/estimate the other) and use ``usage_status``
    ``'partial'``. ``resolved_model`` is the BILLED model id — precedence
    payload id > ``--model`` override > configured default — and must never
    be a bare CLI alias (``'codex'``/``'gemini'``/``'claude'``/
    ``'claude-interactive'``); a mis-resolution there is indistinguishable
    from an unpriced model and silently nulls cost (CTR-fh-013).
    ``usage_status`` is one of ``provider-json`` | ``sdk-metadata`` |
    ``partial`` | ``unavailable`` and is NEVER left implicit — every
    consult_* function below returns a ``Usage``, even on the fully
    unavailable path (INV-fh-011).
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    resolved_model: str | None = None
    usage_status: str = "unavailable"


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
) -> tuple[str, str]:
    """Run a provider CLI, capturing BOTH stdout and stderr, with a HARD timeout that
    actually fires.

    Returns ``(stdout, stderr)`` — some provider CLIs print their usage-bearing JSON
    payload to stderr rather than stdout, and a stdout-only capture silently loses it
    (CTR-fh-012, chief-wiggum#134).

    ``subprocess.run(timeout=...)`` kills only the direct child; if the CLI spawned
    grandchildren holding the stdout pipe open, the follow-up ``communicate()`` blocks
    reading that pipe until they exit — so the "timeout" never returns and the calling
    worker hangs (the root cause of consult-driven stalls, #95). Here the CLI runs in
    its OWN session/process group (``start_new_session``) and a timeout kills the whole
    group, guaranteeing control returns within ``timeout``. A daemon thread emits a
    stderr heartbeat so a long-but-live consult is not mistaken for a hang.

    Raises ``subprocess.TimeoutExpired`` / ``subprocess.CalledProcessError`` to preserve
    the previous ``subprocess.run(check=True, timeout=...)`` contract.

    @cw-trace guards CTR-fh-012
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
    return out, err


def _codex_configured_model() -> str | None:
    """Best-effort read of codex exec's configured default model from
    ``$CODEX_HOME/config.toml`` (default ``~/.codex/config.toml``).

    Verified live against the installed codex-cli 0.142.5: ``codex exec --json``'s
    JSONL event stream carries NO model field anywhere (only ``turn.completed.usage``
    token counts) — only the plain (non-JSON) banner prints ``model: <id>``, and that
    mode loses the separate input/output token counts we need. So when the caller
    didn't pass ``--model``, this config read is the only real lead on which model
    was actually billed — not a hardcoded guess. Returns ``None`` (honest unresolved,
    per ADR-fh-05) when the file is absent, unparseable, or has no top-level ``model``
    key; callers must NOT fall back to the literal string ``'codex'`` (CTR-fh-013).
    """
    home = os.environ.get("CODEX_HOME")
    config_path = (Path(home).expanduser() if home else Path.home() / ".codex") / "config.toml"
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model.strip() else None


def _as_int(value) -> int | None:
    """Coerce a token count from an untrusted provider payload to an ``int``.

    Accepts int, integral float, and numeric string; anything else (bool, None,
    junk string, list, ...) is ``None`` — never trusted, never guessed. This is
    the parser-boundary validation that keeps a drifted usage payload from
    poisoning downstream cost math: a malformed count degrades the usage
    (partial/unavailable), it never crashes or half-prices the record.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _iter_jsonl_events(*streams: str):
    """Yield each well-formed JSON *object* from JSONL streams, skipping
    non-JSON lines, malformed lines, and non-dict values — a drifted event
    shape must degrade parsing, never raise out of it."""
    for stream in streams:
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _codex_agent_text(stdout: str, stderr: str = "") -> str:
    """Reconstruct the plain response text from ``codex exec --json``'s event
    stream: the concatenation of ``agent_message`` ``item.completed`` events, in
    order — equivalent to what plain (non-JSON) ``codex exec`` printed as its final
    answer (verified against a live probe of codex-cli 0.142.5). Scans BOTH
    streams (CTR-fh-012) and is type-tolerant: a drifted event shape (item not a
    dict, text not a string) is skipped, never raised — the caller falls back to
    the raw stream when nothing usable is found (CTR-fh-011)."""
    parts: list[str] = []
    for event in _iter_jsonl_events(stdout, stderr):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _parse_codex_usage(stdout: str, stderr: str, model_override: str | None) -> Usage:
    """Parse ``codex exec --json``'s JSONL event stream (``turn.completed.usage``)
    for tokens. Scans BOTH stdout and stderr (CTR-fh-012) even though a live probe
    against codex-cli 0.142.5 showed the payload lands on stdout only — a future
    CLI version moving it to stderr must not silently lose it. Token values are
    validated at this boundary (``_as_int``): a present-but-malformed count
    degrades to 'partial' under both-tokens-or-null, never a crash."""
    resolved = model_override or _codex_configured_model()
    for event in _iter_jsonl_events(stdout, stderr):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        raw_tin, raw_tout = usage.get("input_tokens"), usage.get("output_tokens")
        if raw_tin is None and raw_tout is None:
            continue
        tin, tout = _as_int(raw_tin), _as_int(raw_tout)
        if tin is None or tout is None:
            # one-sided or malformed payload: both-tokens-or-null (INV-fh-011)
            return Usage(usage_status="partial", resolved_model=resolved)
        return Usage(tokens_in=tin, tokens_out=tout, usage_status="provider-json",
                     resolved_model=resolved)
    return Usage(usage_status="unavailable", resolved_model=resolved)


def consult_codex(prompt: str, model: str | None = None, cwd: str | None = None) -> tuple[str, Usage]:
    """Call codex CLI in read-only sandbox. Uses its own auth session.

    Passes prompt via stdin (``-``) to avoid shell argument length issues
    and to match how codex exec expects large prompts. Uses ``--json`` (the
    JSONL event stream, verified via ``codex exec --help`` and a live probe)
    so usage is available at all — codex's plain-text mode only prints a
    single combined token total, which fails both-tokens-or-null.

    Overrides reasoning effort to ``high`` (instead of user's default which
    may be ``xhigh``) to keep response times reasonable for consultations.

    @cw-trace guards CTR-fh-010
    """
    cmd = [
        "codex", "exec", "--sandbox", "read-only",
        "-c", 'model_reasoning_effort="high"',
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["--json", "-"])  # JSON event stream; read prompt from stdin
    out, err = _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("codex", TIMEOUT),
        cwd=cwd, tool="codex",
    )
    # @cw-trace guards CTR-fh-011 — BOTH text reconstruction and usage parsing
    # are best-effort: a drifted event shape must never turn a successful
    # provider call into a failed consult. If no agent_message text can be
    # recovered, fall back to the raw stream so the consult's product is
    # degraded, never lost.
    try:
        text = _codex_agent_text(out, err)
    except Exception:
        text = ""
    if not text:
        text = out
    try:
        usage = _parse_codex_usage(out, err, model)
    except Exception:
        usage = Usage(usage_status="unavailable", resolved_model=model)
    return text, usage


def _gemini_usage_from_payload(payload: dict) -> Usage:
    """Extract usage from a parsed gemini JSON envelope's ``stats.models``
    section, defensively: any drifted sub-shape (models not a dict, tokens not a
    dict, non-int counts) degrades to unavailable/partial — never raises."""
    stats = payload.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict) or not models:
        return Usage(usage_status="unavailable")

    def _candidates(entry) -> int:
        tokens = entry.get("tokens") if isinstance(entry, dict) else None
        return (_as_int(tokens.get("candidates")) or 0) if isinstance(tokens, dict) else 0

    # A session can bill more than one model (e.g. a router/tool-loop turn);
    # the one with the most output tokens produced the final answer.
    model_id, model_stats = max(models.items(), key=lambda kv: _candidates(kv[1]))
    resolved = model_id if isinstance(model_id, str) else None
    tokens = model_stats.get("tokens") if isinstance(model_stats, dict) else None
    if not isinstance(tokens, dict):
        return Usage(usage_status="unavailable", resolved_model=resolved)
    raw_tin, raw_tout = tokens.get("prompt"), tokens.get("candidates")
    if raw_tin is None and raw_tout is None:
        return Usage(usage_status="unavailable", resolved_model=resolved)
    tin, tout = _as_int(raw_tin), _as_int(raw_tout)
    if tin is None or tout is None:
        return Usage(usage_status="partial", resolved_model=resolved)
    return Usage(tokens_in=tin, tokens_out=tout, usage_status="provider-json",
                 resolved_model=resolved)


def _parse_gemini_output(stdout: str, stderr: str) -> tuple[str, Usage]:
    """Parse ``gemini --output-format json``'s single JSON object: ``{session_id,
    response, stats:{models:{<id>:{tokens:{prompt,candidates,...}}}}}`` (shape
    verified from the installed @google/gemini-cli 0.36.0 bundle's
    ``JsonFormatter``/``UiTelemetryService``). Both stdout and stderr are checked
    (CTR-fh-012).

    Text extraction and usage extraction are SPLIT (CTR-fh-011): once the
    envelope parses and carries ``response``, that response text is the
    consult's product — a drifted/malformed ``stats`` section degrades ONLY the
    usage (``unavailable``), it never causes the caller to receive the raw JSON
    envelope instead of the answer. Only a fully unparseable envelope falls
    back to the raw stdout (matching the pre-#134 text-mode contract)."""
    for stream in (stdout, stderr):
        stripped = stream.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "response" not in payload:
            continue
        response_text = payload.get("response")
        if not isinstance(response_text, str):
            response_text = ""
        try:
            usage = _gemini_usage_from_payload(payload)
        except Exception:
            usage = Usage(usage_status="unavailable")
        return response_text, usage
    # Neither stream parsed as the expected JSON payload — degrade to the raw
    # stdout as the response text (matches the pre-#134 text-mode contract).
    return stdout, Usage(usage_status="unavailable")


def consult_gemini(prompt: str, model: str | None = None, cwd: str | None = None) -> tuple[str, Usage]:
    """Call gemini CLI. Uses its own auth session.

    Passes prompt via stdin to avoid shell argument length issues.
    Uses --yolo to auto-approve all tool use (required for non-interactive
    subprocess execution — without it gemini blocks on approval prompts).
    Uses ``--output-format json`` (rather than ``text``) so usage is
    available at all.

    @cw-trace guards CTR-fh-010
    """
    cmd = ["gemini", "--yolo", "--output-format", "json", "-p", ""]
    if model:
        cmd.extend(["-m", model])
    out, err = _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("gemini", TIMEOUT),
        cwd=cwd, tool="gemini",
    )
    # @cw-trace guards CTR-fh-011 — a usage-parsing exception never fails
    # the consult; fall back to the raw stdout as the response text.
    try:
        return _parse_gemini_output(out, err)
    except Exception:
        return out, Usage(usage_status="unavailable")


def _parse_vertex_usage(response, requested_model: str) -> Usage:
    """Wire ``response.usage_metadata`` (google-genai SDK — field names verified
    against the installed package's ``GenerateContentResponseUsageMetadata``):
    ``prompt_token_count``/``candidates_token_count``. This is the #134 gap this
    adapter previously discarded entirely. ``response.model_version`` is the
    resolved billed model id when the SDK surfaces one."""
    resolved = getattr(response, "model_version", None) or requested_model
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return Usage(usage_status="unavailable", resolved_model=resolved)
    raw_tin = getattr(meta, "prompt_token_count", None)
    raw_tout = getattr(meta, "candidates_token_count", None)
    if raw_tin is None and raw_tout is None:
        return Usage(usage_status="unavailable", resolved_model=resolved)
    tin, tout = _as_int(raw_tin), _as_int(raw_tout)
    if tin is None or tout is None:
        # one-sided or malformed count: both-tokens-or-null (INV-fh-011)
        return Usage(usage_status="partial", resolved_model=resolved)
    return Usage(tokens_in=tin, tokens_out=tout, usage_status="sdk-metadata", resolved_model=resolved)


def consult_gemini_vertex(prompt: str, model: str | None = None, cwd: str | None = None) -> tuple[str, Usage]:
    """Call Gemini via Vertex AI (google-genai SDK). Fetches credentials from keyring.

    Gemini 3.x text models generate only via the `global` location on Vertex,
    and the legacy vertexai.generative_models surface 404s on them.
    """
    project = get_secret("GOOGLE_CLOUD_PROJECT")
    location = get_secret("GOOGLE_CLOUD_LOCATION") or "global"

    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT not found in keyring. "
              "Run: python3 scripts/keychain.py set GOOGLE_CLOUD_PROJECT",
              file=sys.stderr)
        sys.exit(1)

    # Import here so the dependency is only needed for this path
    from google import genai  # type: ignore

    requested_model = model or DEFAULT_VERTEX_MODEL
    client = genai.Client(vertexai=True, project=project, location=location)
    response = client.models.generate_content(model=requested_model, contents=prompt)
    text = response.text or ""
    # @cw-trace guards CTR-fh-010 CTR-fh-011 — response.usage_metadata is a
    # usage-bearing source by construction; parsing failures never fail the
    # consult (the text above was already produced independently).
    try:
        usage = _parse_vertex_usage(response, requested_model)
    except Exception:
        usage = Usage(usage_status="unavailable", resolved_model=requested_model)
    return text, usage


def _claude_usage_from_payload(payload: dict, model_override: str | None) -> Usage:
    """Extract usage from a parsed claude JSON envelope's ``usage``/``modelUsage``
    sections, defensively: any drifted sub-shape (usage not a dict, modelUsage
    entries not dicts, non-int counts) degrades to unavailable/partial — never
    raises.

    Top-level ``usage`` reflects the LAST/primary turn; ``modelUsage`` breaks
    totals out per model (a session can bill more than one, e.g. a cheap
    title-generation call) — the entry whose token counts match top-level
    ``usage`` is the one that produced ``result``, so its key is the resolved
    billed model id (never the bare CLI alias ``'claude'``, CTR-fh-013)."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return Usage(usage_status="unavailable", resolved_model=model_override)
    raw_tin, raw_tout = usage.get("input_tokens"), usage.get("output_tokens")
    tin, tout = _as_int(raw_tin), _as_int(raw_tout)
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict):
        model_usage = {}
    resolved = None
    if len(model_usage) == 1:
        only = next(iter(model_usage))
        resolved = only if isinstance(only, str) else None
    else:
        for mid, mu in model_usage.items():
            if not isinstance(mu, dict) or not isinstance(mid, str):
                continue
            if _as_int(mu.get("inputTokens")) == tin and _as_int(mu.get("outputTokens")) == tout:
                resolved = mid
                break
    resolved = resolved or model_override
    if raw_tin is None and raw_tout is None:
        return Usage(usage_status="unavailable", resolved_model=resolved)
    if tin is None or tout is None:
        # one-sided or malformed count: both-tokens-or-null (INV-fh-011)
        return Usage(usage_status="partial", resolved_model=resolved)
    return Usage(tokens_in=tin, tokens_out=tout, usage_status="provider-json",
                 resolved_model=resolved)


def _parse_claude_output(stdout: str, stderr: str, model_override: str | None) -> tuple[str, Usage]:
    """Parse ``claude -p --output-format json``'s result envelope (shape verified
    live against Claude Code 2.1.210): ``{result, usage:{input_tokens,output_tokens,
    ...}, modelUsage:{<model-id>:{inputTokens,outputTokens,...}}}``. Both stdout
    and stderr are checked (CTR-fh-012).

    Text extraction and usage extraction are SPLIT (CTR-fh-011): once the
    envelope parses and carries ``result``, that result text is the consult's
    product — drifted ``usage``/``modelUsage`` shapes degrade ONLY the usage,
    never replace the answer with the raw JSON envelope. Only a fully
    unparseable envelope falls back to the raw stdout."""
    for stream in (stdout, stderr):
        stripped = stream.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "result" not in payload:
            continue
        text = payload.get("result")
        if not isinstance(text, str):
            text = ""
        try:
            usage = _claude_usage_from_payload(payload, model_override)
        except Exception:
            usage = Usage(usage_status="unavailable", resolved_model=model_override)
        return text, usage
    return stdout, Usage(usage_status="unavailable", resolved_model=model_override)


def consult_claude(prompt: str, model: str | None = None, cwd: str | None = None) -> tuple[str, Usage]:
    """Call claude CLI. Uses its own auth session. Uses ``--output-format json``
    (rather than ``text``) so usage is available at all.

    @cw-trace guards CTR-fh-010
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    out, err = _run_capture(
        cmd, input_text=prompt, timeout=TOOL_TIMEOUTS.get("claude", TIMEOUT),
        cwd=cwd, tool="claude",
    )
    # @cw-trace guards CTR-fh-011
    try:
        return _parse_claude_output(out, err, model)
    except Exception:
        return out, Usage(usage_status="unavailable", resolved_model=model)


def consult_claude_interactive(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Delegate to the interactive Claude tmux provider.

    The RESULT file the delegate writes carries no usage data by construction
    (``skills/claude-interactive-delegate/scripts/claude_delegate.py`` never
    writes token counts) — this adapter is ALWAYS ``usage_status='unavailable'``,
    per ADR-fh-05.

    ``timeout`` overrides the default 1800s budget (``TOOL_TIMEOUTS["claude-interactive"]``)
    when given. This is how a role quorum caps this delegate to a much shorter
    wall-clock when it is running in an OPTIONAL slot (chief-wiggum#188) — the
    ``subprocess.TimeoutExpired`` this raises is caught by ``_run_one_provider``
    exactly like any other optional-provider failure, so a shortened timeout
    still degrades to a clean, non-blocking skip.

    @cw-trace guards CTR-fh-010
    """
    if model:
        print("Warning: --model is ignored for claude-interactive", file=sys.stderr)
    script = Path(__file__).resolve().parents[1] / "skills" / "claude-interactive-delegate" / "scripts" / "claude_delegate.py"
    fd, prompt_name = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    prompt_file = Path(prompt_name)
    try:
        prompt_file.write_text(prompt)
        effective_timeout = timeout if timeout is not None else TOOL_TIMEOUTS["claude-interactive"]
        cmd = [
            sys.executable,
            str(script),
            "submit",
            "--prompt-file",
            str(prompt_file),
            "--wait",
            "--timeout-seconds",
            str(effective_timeout),
        ]
        if cwd:
            cmd.extend(["--cwd", cwd])
        # The delegate script polls internally up to --timeout-seconds and exits
        # GRACEFULLY (a controlled "TIMEOUT: ..." message + returncode 3) rather than
        # being killed mid-poll — give it a small grace window to hit that path first;
        # _run_capture's own timeout is the hard backstop for a subprocess that hangs
        # instead of returning (chief-wiggum#188).
        stdout, _stderr = _run_capture(
            cmd, input_text=None, timeout=effective_timeout + 30,
            cwd=None, tool="claude-interactive",
        )
        for line in stdout.splitlines():
            if line.startswith("RESULT="):
                result_path = Path(line.removeprefix("RESULT="))
                if result_path.exists():
                    return result_path.read_text(), Usage(usage_status="unavailable")
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


# Which parser produced a consult's usage (ConsultUsageRecord.adapter, #134).
ADAPTER_BY_TOOL = {
    "codex": "codex-cli",
    "gemini": "gemini-cli",
    "gemini-vertex": "vertex-sdk",
    "claude": "claude-cli",
    "claude-interactive": "claude-interactive",
}


def _emit_consult_telemetry(
    provider_label: str, model: str | None, cwd: str | None, usage: Usage,
    *, ticket: str | None = None,
) -> None:
    """Best-effort factory telemetry for a consult. No-op unless telemetry is enabled
    (CW_TELEMETRY / CW_FACTORY_LOG); never breaks the consult (CTR-fh-011). Carries
    real per-provider token usage + the resolved billed model id (#134) — cost is
    computed exclusively inside ``factory_log.emit_consult`` (INV-fh-002).
    """
    try:
        import os
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        import factory_log
        repo = os.path.basename(os.path.abspath(cwd)) if cwd else None
        factory_log.emit_consult(
            provider_label, usage.resolved_model, usage.tokens_in, usage.tokens_out,
            usage_status=usage.usage_status, adapter=ADAPTER_BY_TOOL.get(provider_label),
            requested_model=model, repo=repo, ticket=ticket,
        )
    except Exception:
        pass


def consult_provider(
    provider: Provider, prompt: str, model: str | None, cwd: str | None,
    *, ticket: str | None = None, timeout_override: int | None = None,
) -> str:
    """Run one provider's consult.

    ``timeout_override`` (chief-wiggum#188) only affects the claude-interactive
    delegate today — the role quorum sets it when this provider is running in an
    OPTIONAL slot, so a hung/slow interactive session fails fast instead of
    holding the whole role's wall-clock to its full 1800s budget. Tool providers
    ignore it; their own ``TOOL_TIMEOUTS`` entries are already well under that.
    """
    if provider.type == "tool":
        if not provider.tool or provider.tool not in TOOLS:
            raise ValueError(f"unsupported tool provider: {provider.name}")
        text, usage = TOOLS[provider.tool](prompt, model=model, cwd=cwd)
        _emit_consult_telemetry(provider.tool, model, cwd, usage, ticket=ticket)
        return text
    if provider.type == "delegate":
        if provider.delegate != "claude-interactive":
            raise ValueError(f"unsupported delegate provider: {provider.name}")
        text, usage = consult_claude_interactive(prompt, model=model, cwd=cwd, timeout=timeout_override)
        _emit_consult_telemetry("claude-interactive", model, cwd, usage, ticket=ticket)
        return text
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
    parser.add_argument("--ticket", help="Issue/ticket number this consult is for (cost-by-ticket telemetry, #134)")
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
        required_names = {p.name for p in plan.required}

        def execute(provider: Provider) -> str:
            provider_prompt = prompt_for_provider(plan.role, provider.name, prompt, lenses)
            # An optional provider's delegate call is capped to a much shorter
            # wall-clock than a required one (chief-wiggum#188): it's allowed to
            # fail, so it should fail FAST rather than holding the whole role's
            # quorum to claude-interactive's full 1800s budget.
            timeout_override = None
            if provider.name not in required_names:
                timeout_override = (
                    plan.role.optional_timeout_seconds
                    if plan.role.optional_timeout_seconds is not None
                    else DEFAULT_OPTIONAL_TIMEOUT_SECONDS
                )
            return consult_provider(
                provider, provider_prompt, args.model, args.cwd,
                ticket=args.ticket, timeout_override=timeout_override,
            )

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
        output, usage = fn(prompt, model=args.model, cwd=args.cwd)
        _emit_consult_telemetry(target, args.model, args.cwd, usage, ticket=args.ticket)
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
        # In --json mode a provider CLI can report its error via stdout (e.g.
        # codex exec --json emits an {"type":"error",...} event there, not on
        # stderr) — fall back to stdout so the message is never blank.
        msg = f"Error calling {target}: {e.stderr or e.output or e}"
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

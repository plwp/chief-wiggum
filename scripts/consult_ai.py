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
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Allow importing keychain from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from keychain import get_secret
from providers import (
    DEFAULT_CONFIG,
    DEFAULT_LENSES,
    DEFAULT_OPTIONAL_TIMEOUT_SECONDS,
    MIN_PROMPT_BYTES,
    Provider,
    load_config,
    load_lenses,
    optional_provider_timeout,
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
    "openrouter": 300,  # plain HTTP completion, no tool loop — 5 min is generous
}
TIMEOUT = 600  # fallback

# Interval (seconds) between liveness heartbeats emitted to stderr while a provider CLI
# runs. A silent multi-minute consult is indistinguishable from a hang to a worker's
# stream-watchdog; a periodic line proves the consult is alive and progressing.
HEARTBEAT_INTERVAL = 30


def _positive_int(value) -> int | None:
    """Coerce ``value`` to a positive ``int``, else ``None``.

    Used to validate every rung of the timeout override chain (chief-wiggum#291):
    a non-numeric or non-positive candidate (a typo'd env var, a stray ``--timeout
    0``) must be IGNORED and fall through to the next source, never raise and
    abort an in-progress consult.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _env_timeout(name: str) -> int | None:
    """Read a positive-integer timeout from env var ``name``, else ``None``
    (unset, non-numeric, or non-positive — chief-wiggum#291)."""
    return _positive_int(os.environ.get(name))


def tool_timeout(tool: str, *, override: int | None = None) -> int:
    """Resolve ``tool``'s wall-clock timeout (seconds) through a single override
    chain (chief-wiggum#291), highest precedence first:

    1. ``override`` — an explicit per-call value (the ``--timeout`` CLI flag in
       single-tool mode, or a role's optional-provider cap threaded through
       ``consult_claude_interactive``'s existing ``timeout`` parameter).
    2. ``CW_CONSULT_TIMEOUT_<TOOL>`` — tool name upper-cased, every
       non-alphanumeric character replaced with ``_`` (e.g. ``gemini-vertex`` ->
       ``CW_CONSULT_TIMEOUT_GEMINI_VERTEX``).
    3. ``CW_CONSULT_TIMEOUT`` — applies to every tool.
    4. The ``TOOL_TIMEOUTS`` table default (``TIMEOUT`` if the tool is unlisted).

    Every candidate is validated by ``_positive_int``: a non-numeric or
    non-positive value at ANY source (including ``override``) is ignored and
    falls through to the next source rather than raising — a bad knob must
    degrade the timeout, never crash a consult mid-workflow.

    This is the ONE place the precedence lives — every call site below routes
    through it, replacing four independent ``TOOL_TIMEOUTS.get(...)`` reads that
    would otherwise drift.
    """
    valid_override = _positive_int(override)
    if valid_override is not None:
        return valid_override
    tool_env_name = "CW_CONSULT_TIMEOUT_" + re.sub(r"[^A-Za-z0-9]", "_", tool.upper())
    specific = _env_timeout(tool_env_name)
    if specific is not None:
        return specific
    general = _env_timeout("CW_CONSULT_TIMEOUT")
    if general is not None:
        return general
    return TOOL_TIMEOUTS.get(tool, TIMEOUT)

# A retry after a TIMEOUT-classified failure gets a REDUCED budget, not a
# repeat of the full one (chief-wiggum#330) — "a codex timeout at 600s is
# retried for another full 600s with the identical ~60k-token prompt" was
# the pre-#330 behavior. Half the previously-resolved budget is still
# generous headroom for a transient slow response, floored so the retry
# stays usable.
RETRY_TIMEOUT_REDUCTION_FACTOR = 0.5
MIN_RETRY_TIMEOUT_SECONDS = 60


def reduced_retry_timeout(tool: str, previous_override: int | None) -> int:
    """Half of ``tool``'s FULLY-RESOLVED first-attempt budget (chief-wiggum#330
    AC3), floored at ``MIN_RETRY_TIMEOUT_SECONDS``. ``previous_override`` is
    whatever the first attempt was actually given (``None`` for a required
    provider — its full budget; an explicit cap for an optional one) —
    resolving through ``tool_timeout`` here (rather than halving the raw
    override) means the reduction is always computed against the tool's REAL
    first-attempt budget, so the second attempt can never end up larger than
    the first."""
    base = tool_timeout(tool, override=previous_override)
    return max(MIN_RETRY_TIMEOUT_SECONDS, int(base * RETRY_TIMEOUT_REDUCTION_FACTOR))


# ``DEFAULT_OPTIONAL_TIMEOUT_SECONDS`` and ``optional_provider_timeout`` are the SINGLE
# source of the required/optional delegate-timeout decision — they live in providers.py
# (chief-wiggum#188) so both this module's ``--role`` quorum and the /implement review
# pipeline (chief_wiggum/review.run_review) cap an optional claude-interactive the same
# way. Re-exported here (imported above) for callers/tests that reference
# ``consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS``.

# Default model for Vertex AI path (override with --model)
DEFAULT_VERTEX_MODEL = "gemini-3.1-pro-preview"

# MIN_PROMPT_BYTES: a prompt file smaller than this is almost never
# intentional — it's the signature of a truncated write (a template
# substitution that silently produced nothing, an interrupted heredoc,
# etc). Live use burned a codex call and an opus agent run on exactly this
# (chief-wiggum#163). Defined in providers.py (chief-wiggum#330) — the
# SINGLE source of truth, re-exported here (imported above) so this refuses
# before any provider is called on THIS entry path (single-tool AND --role
# modes), and providers.run_role_quorum enforces the identical floor for
# every OTHER caller (e.g. scripts/run_review.py), which never went through
# this CLI's own pre-check at all before #330.


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


def consult_codex(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Call codex CLI in read-only sandbox. Uses its own auth session.

    Passes prompt via stdin (``-``) to avoid shell argument length issues
    and to match how codex exec expects large prompts. Uses ``--json`` (the
    JSONL event stream, verified via ``codex exec --help`` and a live probe)
    so usage is available at all — codex's plain-text mode only prints a
    single combined token total, which fails both-tokens-or-null.

    Overrides reasoning effort to ``high`` (instead of user's default which
    may be ``xhigh``) to keep response times reasonable for consultations.

    ``timeout`` overrides the resolved budget (chief-wiggum#291 — see
    ``tool_timeout``); ``None`` resolves through the env-var/table chain.

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
        cmd, input_text=prompt, timeout=tool_timeout("codex", override=timeout),
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


def consult_gemini(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Call gemini CLI. Uses its own auth session.

    Passes prompt via stdin to avoid shell argument length issues.
    Uses --yolo to auto-approve all tool use (required for non-interactive
    subprocess execution — without it gemini blocks on approval prompts).
    Uses ``--output-format json`` (rather than ``text``) so usage is
    available at all.

    ``timeout`` overrides the resolved budget (chief-wiggum#291 — see
    ``tool_timeout``); ``None`` resolves through the env-var/table chain.

    @cw-trace guards CTR-fh-010
    """
    cmd = ["gemini", "--yolo", "--output-format", "json", "-p", ""]
    if model:
        cmd.extend(["-m", model])
    out, err = _run_capture(
        cmd, input_text=prompt, timeout=tool_timeout("gemini", override=timeout),
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


# chief-wiggum#319: consult_gemini_vertex is a single synchronous SDK call with
# no tool loop, so it cannot browse the repo the way codex/claude's own exec
# sandboxes can. What it CAN do reliably is retrieve exactly the files a
# diff-review prompt is ABOUT: git always emits a `diff --git a/<path>
# b/<path>` header per touched file, and every review-shaped prompt this
# module's callers build (chief_wiggum.review.assemble_review_prompt's
# {{DIFF}} substitution) embeds the literal diff. Extracting those paths and
# reading each file's CURRENT full content from `cwd` gives gemini-vertex real
# grounding for exactly the class of mistake chief-wiggum#319 documents (and
# this repo's own #281 review corroborated: a false "pre-existing vs
# introduced" attribution) — without pretending to a general repo-browsing
# capability it doesn't have. A prompt with no diff header (an open-ended
# exploration prompt, no bounded file set to retrieve) yields nothing and
# falls back to prompt-only — never a guessed, unreliable file selection.
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# Bounds so a huge diff can't blow the request past a usable size: at most
# this many files, and at most this many bytes of any one file's content.
MAX_RETRIEVED_FILES = 40
MAX_RETRIEVED_FILE_BYTES = 60_000


def _touched_files_from_diff(text: str) -> list[str]:
    """Extract touched-file paths from a unified diff embedded in ``text``.

    Deterministic and bounded to files the diff itself names — NOT a general
    heuristic file-selector over an arbitrary prompt (that would be unreliable
    for an open-ended prompt with no diff, exactly the trap chief-wiggum#319
    warns against shipping). The post-image (``b/``) path is used — correct
    for a modification or rename; for a deletion the file may no longer exist,
    which ``_read_touched_files`` skips, never raises on.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for match in _DIFF_GIT_HEADER_RE.finditer(text):
        path = match.group(2)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _read_touched_files(cwd: str | None, paths: list[str]) -> list[str]:
    """Read each touched file's CURRENT full content from ``cwd``, best-effort.

    Best-effort per file (CTR-fh-011 pattern applied to retrieval): a path
    that no longer exists (deleted in the diff), isn't readable, resolves
    outside ``cwd``, or decodes as binary is skipped, never raised — a
    retrieval gap degrades the amount of context gemini-vertex gets, it never
    fails the whole consult.
    """
    if not cwd:
        return []
    base = Path(cwd).resolve()
    blocks: list[str] = []
    for rel in paths[:MAX_RETRIEVED_FILES]:
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            continue  # path escapes cwd — never follow it
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_RETRIEVED_FILE_BYTES:
            content = (
                encoded[:MAX_RETRIEVED_FILE_BYTES].decode("utf-8", errors="ignore")
                + f"\n... [truncated at {MAX_RETRIEVED_FILE_BYTES} bytes]"
            )
        blocks.append(f"--- {rel} (current full content, chief-wiggum#319) ---\n{content}")
    return blocks


# chief-wiggum#321: design_critic sends rendered SCREENSHOTS, not diff text —
# a different blindness shape than #319's diff-scoped text retrieval above,
# which never touches image bytes at all. A CLI tool provider with real
# filesystem access via ``cwd`` (codex, claude-interactive) can already open
# a named screenshot itself; gemini-vertex's call path is a single
# non-agentic SDK request with no tool loop, so it needs the bytes handed to
# it directly. The google-genai SDK's multimodal ``contents`` accepts a list
# mixing plain text with ``types.Part.from_bytes(data=..., mime_type=...)``
# image parts (verified against the installed google-genai package) — this
# reads exactly the image files named in the prompt from ``cwd``, bounded the
# same way #319 bounds diff-file retrieval: a capped count, a capped
# per-image byte size, and the same path-traversal guard. A prompt that names
# no images, or whose named images don't exist under ``cwd``, retrieves
# nothing and ``contents`` stays exactly what it would have been without
# this — an honest no-op, not a pretended fix.
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_IMAGE_PATH_RE = re.compile(
    r"(?<![\w/])((?:[\w.\-]+/)*[\w.\-]+\.(?:png|jpe?g|webp|gif))\b",
    re.IGNORECASE,
)

# Bounds mirroring MAX_RETRIEVED_FILES/MAX_RETRIEVED_FILE_BYTES above, sized
# for images rather than source text: a handful of full-page screenshots per
# design direction, capped well under Vertex's inline-request ceiling.
MAX_RETRIEVED_IMAGES = 20
MAX_RETRIEVED_IMAGE_BYTES = 8_000_000


def _image_paths_from_prompt(text: str) -> list[str]:
    """Extract image-file paths named in ``text`` (a design-critique prompt
    names the screenshot files it wants critiqued — see ``design.md`` Step
    4). Deterministic pattern match on a known set of image extensions,
    deduped in first-seen order — NOT a general file-mention heuristic. A
    path matched here that doesn't actually exist under ``cwd`` is simply
    never retrieved (``_read_touched_images`` is best-effort), so a
    false-positive match (e.g. a filename mentioned inside a URL) is
    harmless.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for match in _IMAGE_PATH_RE.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _read_touched_images(cwd: str | None, paths: list[str]) -> list[tuple[str, bytes, str]]:
    """Read each named image's bytes + MIME type from ``cwd``, best-effort.

    Mirrors ``_read_touched_files``'s discipline: a path that doesn't exist,
    isn't readable, resolves outside ``cwd``, has an unrecognized extension,
    or is over the per-image byte cap is skipped, never raised — a
    retrieval gap degrades to fewer attached images, never fails the whole
    consult. An oversized image is DROPPED rather than truncated (unlike the
    text-file path): truncating binary image bytes would produce corrupt,
    undecodable image data, not merely a shorter one.
    """
    if not cwd:
        return []
    base = Path(cwd).resolve()
    images: list[tuple[str, bytes, str]] = []
    for rel in paths[:MAX_RETRIEVED_IMAGES]:
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            continue  # path escapes cwd — never follow it
        mime = _IMAGE_MIME_TYPES.get(candidate.suffix.lower())
        if mime is None:
            continue
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_RETRIEVED_IMAGE_BYTES:
            continue
        images.append((rel, data, mime))
    return images


def _call_with_deadline(fn, timeout: int):
    """Run a blocking, non-cancellable call under a HARD wall-clock deadline
    (chief-wiggum#330), mirroring ``_http_json_with_deadline``'s pattern above:
    the call runs on a daemon thread and this function returns/raises once
    either the call finishes or ``timeout`` elapses, whichever comes first.

    Python cannot forcibly kill a thread — an abandoned daemon thread is not
    joined and cannot outlive the process, but it is NOT stopped either; if
    the underlying call never returns, that thread leaks until it does (or
    the process exits). This is the same limitation ``_kill_group`` exists to
    route around for subprocess-based providers (kill the OS process, not a
    Python thread) — the Vertex SDK call has no subprocess to kill, so a
    daemon-thread deadline is the best bound available: it unblocks the
    CALLER (and therefore the quorum) even though the leaked call itself may
    still be running somewhere.
    """
    result: dict = {}

    def _run() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # re-raised on the calling thread below
            result["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"call exceeded its {timeout}s budget")
    if "error" in result:
        raise result["error"]
    return result["value"]


def consult_gemini_vertex(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Call Gemini via Vertex AI (google-genai SDK). Fetches credentials from keyring.

    Gemini 3.x text models generate only via the `global` location on Vertex,
    and the legacy vertexai.generative_models surface 404s on them.

    chief-wiggum#330: this call used to have NO wall-clock deadline at all —
    ``timeout`` was accepted "for CLI signature parity" and never enforced,
    because this is a synchronous SDK request with no subprocess to bound
    (unlike every other tool adapter above, which routes through
    ``_run_capture``'s process-group kill). ``gemini-vertex`` is REQUIRED in
    the ``reviewer`` role, so a hung call used to block every review forever
    — the one remaining unbounded path in a codebase that has fixed hangs
    three times (#95, #188, the OpenRouter ``_http_json_with_deadline``). The
    SDK call now runs under ``_call_with_deadline``, resolved through the
    SAME override chain (chief-wiggum#291's ``tool_timeout``) every other
    tool provider already uses.

    chief-wiggum#319: this adapter used to call ``generate_content`` with
    ``contents=prompt`` alone — ``cwd`` was accepted and never touched, so
    every consult answered from the prompt text with zero repo access. It now
    retrieves the CURRENT content of every file the prompt's embedded diff
    touches (see ``_touched_files_from_diff``) and appends it to ``contents``.
    This is bounded and diff-scoped, not general repo browsing: a prompt
    carrying no diff (e.g. an open-ended exploration prompt) retrieves
    nothing and this behaves exactly as before — an honest no-op, not a
    pretended fix.

    chief-wiggum#321: the same call had NO image-attachment path at all —
    ``design_critic`` sends this adapter rendered screenshots and it never
    saw a single pixel, only the filenames a text prompt happened to name.
    It now also reads every image file the prompt names (see
    ``_image_paths_from_prompt``) from ``cwd`` and attaches each as an inline
    ``types.Part.from_bytes`` image part alongside the text. A prompt naming
    no images (every non-``design_critic`` role today) retrieves nothing and
    ``contents`` is unaffected — this is additive to, and independent of,
    the diff-file retrieval above.
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

    touched = _touched_files_from_diff(prompt)
    file_blocks = _read_touched_files(cwd, touched)
    if file_blocks:
        contents = (
            prompt
            + "\n\n---\nRepo context (chief-wiggum#319): the CURRENT full content of "
              "every file touched by the diff above, read from the repo. Use it to "
              "judge whether code shown in the diff is pre-existing or newly "
              "introduced, and to see context beyond the diff hunk.\n\n"
            + "\n\n".join(file_blocks)
        )
    else:
        contents = prompt

    image_paths = _image_paths_from_prompt(prompt)
    images = _read_touched_images(cwd, image_paths)
    if images:
        # Deferred import, mirroring the ``genai`` import above: only needed
        # on this path, so a caller that never sends images never needs the
        # submodule to resolve — the honest-no-op path picks up no new
        # import requirement.
        from google.genai import types  # type: ignore
        contents = [contents] + [
            types.Part.from_bytes(data=data, mime_type=mime) for _rel, data, mime in images
        ]

    effective_timeout = tool_timeout("gemini-vertex", override=timeout)
    response = _call_with_deadline(
        lambda: client.models.generate_content(model=requested_model, contents=contents),
        effective_timeout,
    )
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


def consult_claude(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Call claude CLI. Uses its own auth session. Uses ``--output-format json``
    (rather than ``text``) so usage is available at all.

    ``timeout`` overrides the resolved budget (chief-wiggum#291 — see
    ``tool_timeout``); ``None`` resolves through the env-var/table chain.

    @cw-trace guards CTR-fh-010
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    out, err = _run_capture(
        cmd, input_text=prompt, timeout=tool_timeout("claude", override=timeout),
        cwd=cwd, tool="claude",
    )
    # @cw-trace guards CTR-fh-011
    try:
        return _parse_claude_output(out, err, model)
    except Exception:
        return out, Usage(usage_status="unavailable", resolved_model=model)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _http_json_with_deadline(request: urllib.request.Request, timeout: int) -> dict:
    """POST and decode JSON under a HARD wall-clock deadline.

    ``urllib``'s own ``timeout`` bounds each individual socket operation, NOT total
    elapsed time — a model that emits slowly but steadily never trips it. Observed
    live: a 300s budget still running at ~30 minutes on a long reasoning response.
    A budget that cannot expire is worse than no budget, because a role quorum then
    blocks forever on one slow provider.

    The blocking call runs on a daemon thread so the deadline is real; an abandoned
    thread cannot outlive the process. The socket timeout is kept as well, so the
    common case (a genuinely stalled connection) still fails fast at the socket layer.
    """
    result: dict = {}

    def _call() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result["payload"] = json.loads(response.read().decode())
        except BaseException as exc:  # re-raised on the calling thread below
            result["error"] = exc

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"openrouter exceeded its {timeout}s budget")
    if "error" in result:
        raise result["error"]
    return result["payload"]


def _parse_openrouter_payload(payload: dict, model_override: str | None) -> tuple[str, Usage]:
    """Extract text + usage from an OpenRouter chat-completion response.

    Reasoning models sometimes put the answer in ``message.reasoning`` and leave
    ``content`` empty; falling through to reasoning keeps a real answer from being
    reported as an empty consult. Usage obeys both-tokens-or-null (INV-fh-011).
    """
    choices = payload.get("choices") or []
    message = (choices[0] or {}).get("message", {}) if choices else {}
    text = (message.get("content") or "").strip() or (message.get("reasoning") or "").strip()

    usage_raw = payload.get("usage") or {}
    tokens_in = _as_int(usage_raw.get("prompt_tokens"))
    tokens_out = _as_int(usage_raw.get("completion_tokens"))
    # OpenRouter reports the model that actually served the request, which may
    # differ from the requested id (`:floor`/`:nitro` routing, auto-fallback).
    # The BILLED id is what telemetry must price, so the payload wins (CTR-fh-013).
    resolved = payload.get("model") or model_override
    if tokens_in is not None and tokens_out is not None:
        return text, Usage(tokens_in, tokens_out, resolved, "provider-json")
    if tokens_in is not None or tokens_out is not None:
        return text, Usage(None, None, resolved, "partial")
    return text, Usage(resolved_model=resolved, usage_status="unavailable")


def consult_openrouter(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
) -> tuple[str, Usage]:
    """Call a model over the OpenRouter HTTP API.

    This provider exists to widen the quorum's *distribution*, not just its
    prompting: the frontier non-Western models (DeepSeek, Kimi, GLM, Qwen, MiniMax)
    are pretrained on materially different corpora, so their priors diverge from
    the Anthropic/OpenAI/Google cluster by construction rather than by roleplay
    (which the lens doctrine forbids anyway — config/lenses.json).

    Unlike every other provider here this is an API call, NOT a CLI with tool
    access: there is no repo, no filesystem, no web. ``cwd`` is accepted for
    signature parity and deliberately ignored. Prompts must be self-contained.

    The key is fetched from the keyring at call time and passed straight into the
    request header — never an env var, never logged (CLAUDE.md secret policy).

    ``timeout`` overrides the resolved budget (chief-wiggum#291 — see
    ``tool_timeout``); ``None`` resolves through the env-var/table chain.
    """
    api_key = get_secret("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not in keyring — set it with "
            "`python3 scripts/keychain.py set OPENROUTER_API_KEY`"
        )
    if not model:
        raise ValueError(
            "openrouter requires an explicit model id (e.g. deepseek/deepseek-v4-pro); "
            "pass --model or pin one on the provider entry in config/providers.json"
        )

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(
        OPENROUTER_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attributes traffic by these; harmless if unrecognised.
            "HTTP-Referer": "https://github.com/plwp/chief-wiggum",
            "X-Title": "chief-wiggum",
        },
    )
    effective_timeout = tool_timeout("openrouter", override=timeout)
    try:
        payload = _http_json_with_deadline(request, effective_timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"openrouter HTTP {exc.code} for {model}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"openrouter unreachable for {model}: {exc.reason}") from exc

    # A provider-level failure arrives as HTTP 200 with an `error` object.
    if payload.get("error") and not payload.get("choices"):
        message = (payload["error"] or {}).get("message", payload["error"])
        raise RuntimeError(f"openrouter error for {model}: {message}")
    return _parse_openrouter_payload(payload, model)


def _delegate_session_name(ticket: str | None = None) -> str:
    """Generate a unique, task-scoped tmux session name (chief-wiggum#331).

    NEVER returns the shared ``cw-claude`` constant a fixed default would
    reuse: every delegated consult gets its OWN session, so (a) it always
    starts from EMPTY context — there is nothing to ``/clear``, the session
    never existed before this call — and (b) two concurrent consults (e.g.
    two tickets in a parallel `/implement-wave`) get two independent tmux
    sessions that cannot queue behind each other on one REPL. A ``ticket``
    is folded into the name (sanitized to tmux/shell-safe characters) purely
    for human-legibility when debugging a stray session with ``tmux ls`` —
    uniqueness itself comes from the uuid suffix, not the ticket.
    """
    suffix = uuid.uuid4().hex[:8]
    if ticket:
        safe_ticket = re.sub(r"[^A-Za-z0-9_-]+", "-", str(ticket)).strip("-")
        if safe_ticket:
            return f"cw-claude-{safe_ticket}-{suffix}"
    return f"cw-claude-{suffix}"


def _stop_delegate_session(session: str) -> None:
    """Best-effort teardown of a delegate's task-scoped tmux session
    (chief-wiggum#331).

    Every delegated consult now owns a UNIQUE session (see
    ``_delegate_session_name``) — nothing else will ever attach to or reuse
    it, so it must be torn down here rather than left to accumulate. Called
    from a ``finally`` block in ``consult_claude_interactive`` so this runs
    whether the consult succeeded, failed, or timed out — "no cw-claude*
    session survives a completed workflow run" has to hold on every exit
    path, not just the happy one.

    Deliberately bypasses ``_run_capture`` (the process-group-aware runner
    used for the actual delegate call) and calls ``subprocess.run`` directly:
    a stop is a fire-and-forget cleanup, not a consult whose output/timeout
    semantics need that machinery, and going through a separate seam keeps
    this from being accidentally captured by tests that mock ``_run_capture``
    to inspect the delegate CALL itself. Any failure (tmux not installed, the
    session already gone, a transient error) is swallowed: a failure to stop
    degrades to a stray tmux session — annoying, cleaned up by hand or a
    later `/setup` — never a crashed, otherwise-successful consult.
    """
    script = Path(__file__).resolve().parents[1] / "skills" / "claude-interactive-delegate" / "scripts" / "claude_delegate.py"
    try:
        subprocess.run(
            [sys.executable, str(script), "--session", session, "stop"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception:
        pass


def consult_claude_interactive(
    prompt: str, model: str | None = None, cwd: str | None = None, timeout: int | None = None,
    *, ticket: str | None = None, session: str | None = None,
) -> tuple[str, Usage]:
    """Delegate to the interactive Claude tmux provider.

    The RESULT file the delegate writes carries no usage data by construction
    (``skills/claude-interactive-delegate/scripts/claude_delegate.py`` never
    writes token counts) — this adapter is ALWAYS ``usage_status='unavailable'``,
    per ADR-fh-05.

    ``timeout`` overrides the resolved budget (default 1800s, ``TOOL_TIMEOUTS
    ["claude-interactive"]``) when given — resolved through ``tool_timeout``
    (chief-wiggum#291), so an explicit ``timeout`` (highest precedence) still
    wins, but a ``None`` now also falls through the ``CW_CONSULT_TIMEOUT*`` env
    vars before the table default. This is how a role quorum caps this delegate
    to a much shorter wall-clock when it is running in an OPTIONAL slot
    (chief-wiggum#188) — the ``subprocess.TimeoutExpired`` this raises is caught
    by ``_run_one_provider`` exactly like any other optional-provider failure,
    so a shortened timeout still degrades to a clean, non-blocking skip.

    chief-wiggum#331: every call now runs against a TASK-SCOPED tmux session
    (``session``, or a fresh name derived from ``ticket``/a uuid when omitted)
    rather than the one shared ``cw-claude`` session every previous consult
    also used — that old default meant consult N was billed the ENTIRE
    accumulated transcript of consults 1..N-1 as input tokens, and two
    concurrent consults (a parallel `/implement-wave`) queued on the single
    REPL, burning the whole optional budget waiting rather than answering.
    A never-before-used session name starts with empty context by
    construction (nothing to ``/clear``), and is torn down in the
    ``finally`` below (``_stop_delegate_session``) so nothing lingers past
    this one call — success, failure, or timeout alike.

    @cw-trace guards CTR-fh-010
    """
    if model:
        print("Warning: --model is ignored for claude-interactive", file=sys.stderr)
    script = Path(__file__).resolve().parents[1] / "skills" / "claude-interactive-delegate" / "scripts" / "claude_delegate.py"
    session_name = session or _delegate_session_name(ticket)
    fd, prompt_name = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    prompt_file = Path(prompt_name)
    try:
        prompt_file.write_text(prompt)
        effective_timeout = tool_timeout("claude-interactive", override=timeout)
        cmd = [
            sys.executable,
            str(script),
            "--session",
            session_name,
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
        _stop_delegate_session(session_name)


TOOLS = {
    "codex": consult_codex,
    "gemini": consult_gemini,
    "gemini-vertex": consult_gemini_vertex,
    "claude": consult_claude,
    "claude-interactive": consult_claude_interactive,
    "openrouter": consult_openrouter,
}


# Which parser produced a consult's usage (ConsultUsageRecord.adapter, #134).
ADAPTER_BY_TOOL = {
    "codex": "codex-cli",
    "gemini": "gemini-cli",
    "gemini-vertex": "vertex-sdk",
    "claude": "claude-cli",
    "claude-interactive": "claude-interactive",
    "openrouter": "openrouter-api",
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
) -> tuple[str, Usage]:
    """Run one provider's consult.

    ``timeout_override`` (chief-wiggum#188) is set by the role quorum when
    this provider is running in an OPTIONAL slot, so a hung/slow call fails
    fast instead of holding the whole role's wall-clock to its full budget.
    Threaded to BOTH branches (chief-wiggum#330): the claude-interactive
    delegate's 1800s default AND every tool provider's own ``TOOL_TIMEOUTS``
    entry (codex 600s, gemini 1200s, gemini-vertex/claude 600s) sit well
    above the 300s optional cap, so dropping it on the tool branch (the
    pre-#330 behavior) left an optional tool provider able to hold a role's
    wall-clock exactly like the delegate used to before #188. Every
    ``consult_*`` tool function already accepts a ``timeout`` kwarg and
    resolves it through ``tool_timeout`` (chief-wiggum#291) — a required
    provider's ``None`` override still falls through that chain unchanged.

    Returns ``(text, usage)`` (chief-wiggum#319) — previously this discarded
    ``usage`` after telemetry, so a role quorum (``providers.run_role_quorum``)
    had no way to see per-provider ``tokens_in`` at all. Both callers
    (``consult_ai.py --role`` and ``scripts/run_review.py``) now propagate the
    pair straight into the quorum's ``ProviderResult``.
    """
    if provider.type == "tool":
        if not provider.tool or provider.tool not in TOOLS:
            raise ValueError(f"unsupported tool provider: {provider.name}")
        # Explicit --model wins; else the provider entry's own default model
        # (chief-wiggum#237 — e.g. the `opus` provider pins the claude tool
        # to the opus model); else the tool's configured default.
        effective_model = model or provider.model
        text, usage = TOOLS[provider.tool](prompt, model=effective_model, cwd=cwd, timeout=timeout_override)
        _emit_consult_telemetry(provider.tool, effective_model, cwd, usage, ticket=ticket)
        return text, usage
    if provider.type == "delegate":
        if provider.delegate != "claude-interactive":
            raise ValueError(f"unsupported delegate provider: {provider.name}")
        # ticket (chief-wiggum#331) folds into the delegate's task-scoped tmux
        # session name for legibility — uniqueness itself is guaranteed by
        # _delegate_session_name's uuid suffix regardless.
        text, usage = consult_claude_interactive(
            prompt, model=model, cwd=cwd, timeout=timeout_override, ticket=ticket,
        )
        _emit_consult_telemetry("claude-interactive", model, cwd, usage, ticket=ticket)
        return text, usage
    raise ValueError(f"unsupported provider type: {provider.type}")


def _timeout_arg(value: str) -> int | None:
    """``type=`` callable for ``--timeout``: a bad value (non-numeric,
    non-positive) resolves to ``None`` rather than aborting argparse
    (chief-wiggum#291 AC2 — an invalid override falls through to the next
    source, it never crashes the CLI invocation)."""
    return _positive_int(value)


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
    parser.add_argument(
        "--timeout", type=_timeout_arg, default=None, metavar="SECONDS",
        help="Override this call's provider timeout in seconds (chief-wiggum#291; highest "
             "precedence, above CW_CONSULT_TIMEOUT[_<TOOL>] and the TOOL_TIMEOUTS default). "
             "Single-tool mode only, not --role. A non-numeric/non-positive value is ignored, "
             "falling through to the env-var/table chain rather than erroring.",
    )
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
        def execute(
            provider: Provider, attempt: int = 1, previous_failure_kind: str | None = None,
        ) -> tuple[str, Usage]:
            provider_prompt = prompt_for_provider(plan.role, provider.name, prompt, lenses)
            # An optional provider's delegate call is capped to a much shorter
            # wall-clock than a required one (chief-wiggum#188): it's allowed to
            # fail, so it should fail FAST rather than holding the whole role's
            # quorum to claude-interactive's full 1800s budget. The required/optional
            # decision is centralized in providers.optional_provider_timeout so the
            # review pipeline caps the same way.
            timeout_override = optional_provider_timeout(
                plan.role, provider.name, DEFAULT_OPTIONAL_TIMEOUT_SECONDS
            )
            # chief-wiggum#330 AC3: a retry that follows a TIMEOUT-classified
            # failure gets a reduced budget, not another full one — the
            # `attempt`/`previous_failure_kind` context this function's own
            # signature declares (see providers.execute_accepts_retry_context)
            # is how providers._run_one_provider's retry loop tells us this.
            if attempt > 1 and previous_failure_kind == "timeout":
                tool_name = provider.tool if provider.type == "tool" else "claude-interactive"
                timeout_override = reduced_retry_timeout(tool_name, timeout_override)
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
            # chief-wiggum#319: the SHARED prompt (pre-lens) + lens map, so the
            # quorum can also run the blindness check and attach it to the
            # manifest. Passing the identical inputs prompt_for_provider was
            # already called with above keeps the per-provider token estimate
            # honest.
            prompt=prompt,
            lenses=lenses,
        )
        for result in manifest.results:
            if result.status == "ok":
                print(f"OK: {result.name} response written to {result.path}")
            elif result.required:
                print(f"Error: required provider {result.name} failed: {result.error}", file=sys.stderr)
            else:
                print(f"Warning: optional provider {result.name} failed: {result.error}", file=sys.stderr)
        # chief-wiggum#319: a quorum can report every required provider "ok"
        # while one of them never read anything beyond its own prompt — report
        # that loudly, on the same stderr stream as every other quorum warning,
        # regardless of whether the overall quorum passes.
        if manifest.blindness is not None:
            for finding in manifest.blindness.findings:
                print(f"Warning: {finding.message}", file=sys.stderr)
        # chief-wiggum#321: the image-shaped sibling of the above — a role
        # that sends images composed with a provider that can't receive
        # them. Structural, so this is populated even when ``manifest.blindness``
        # is None (no ``prompt`` was passed for the token-floor check).
        if manifest.image_blindness is not None:
            for finding in manifest.image_blindness.findings:
                print(f"Warning: {finding.message}", file=sys.stderr)
        if not manifest.ok:
            print(
                f"Role {args.role} quorum failed: {', '.join(manifest.failed_required)}",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    assert target is not None
    fn = TOOLS[target]
    # --timeout (chief-wiggum#291) is the top of the override chain for a
    # single-tool call; resolved here only for the timeout-expiry message
    # below — the real resolution (including the env-var rungs) happens
    # inside the consult_* function itself via tool_timeout().
    effective_timeout = tool_timeout(target, override=args.timeout)
    out_path = Path(args.output) if args.output else None
    if out_path:
        # Create missing parent directories up front so writing the response —
        # success OR failure message — never fails with FileNotFoundError.
        out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output, usage = fn(prompt, model=args.model, cwd=args.cwd, timeout=args.timeout)
        _emit_consult_telemetry(target, args.model, args.cwd, usage, ticket=args.ticket)
        if out_path:
            out_path.write_text(output)
            print(f"OK: {target} response written to {args.output}")
        else:
            print(output)
    except subprocess.TimeoutExpired:
        msg = f"Timeout: {target} did not respond within {effective_timeout}s"
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

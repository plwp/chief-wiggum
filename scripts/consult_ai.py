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

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow importing keychain from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from keychain import get_secret
from providers import (
    DEFAULT_CONFIG,
    Provider,
    load_config,
    plan_role,
    run_role_quorum,
    validate_config,
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

# Default model for Vertex AI path (override with --model)
DEFAULT_VERTEX_MODEL = "gemini-3.1-pro-preview"


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
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, check=True,
        timeout=TOOL_TIMEOUTS.get("codex", TIMEOUT),
        cwd=cwd,
    )
    return result.stdout


def consult_gemini(prompt: str, model: str | None = None, cwd: str | None = None) -> str:
    """Call gemini CLI. Uses its own auth session.

    Passes prompt via stdin to avoid shell argument length issues.
    Uses --yolo to auto-approve all tool use (required for non-interactive
    subprocess execution — without it gemini blocks on approval prompts).
    """
    cmd = ["gemini", "--yolo", "--output-format", "text", "-p", ""]
    if model:
        cmd.extend(["-m", model])
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, check=True,
        timeout=TOOL_TIMEOUTS.get("gemini", TIMEOUT),
        cwd=cwd,
    )
    return result.stdout


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
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        check=True, timeout=TOOL_TIMEOUTS.get("claude", TIMEOUT),
        cwd=cwd,
    )
    return result.stdout


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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=TOOL_TIMEOUTS["claude-interactive"],
        )
        for line in result.stdout.splitlines():
            if line.startswith("RESULT="):
                result_path = Path(line.removeprefix("RESULT="))
                if result_path.exists():
                    return result_path.read_text()
                raise RuntimeError(f"claude-interactive result path does not exist: {result_path}")
        raise RuntimeError(f"claude-interactive completed without RESULT line: {result.stdout}")
    finally:
        prompt_file.unlink(missing_ok=True)


TOOLS = {
    "codex": consult_codex,
    "gemini": consult_gemini,
    "gemini-vertex": consult_gemini_vertex,
    "claude": consult_claude,
    "claude-interactive": consult_claude_interactive,
}


def consult_provider(provider: Provider, prompt: str, model: str | None, cwd: str | None) -> str:
    if provider.type == "tool":
        if not provider.tool or provider.tool not in TOOLS:
            raise ValueError(f"unsupported tool provider: {provider.name}")
        return TOOLS[provider.tool](prompt, model=model, cwd=cwd)
    if provider.type == "delegate":
        if provider.delegate != "claude-interactive":
            raise ValueError(f"unsupported delegate provider: {provider.name}")
        return consult_claude_interactive(prompt, model=model, cwd=cwd)
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
    parser.add_argument("--enable-provider", action="append", default=[], help="Force-enable provider by name")
    parser.add_argument("--disable-provider", action="append", default=[], help="Disable provider by name")
    parser.add_argument("--max-attempts", type=int, default=2, help="Retries for required providers in --role mode")
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
        # a manifest. Required providers must produce substantive output.
        manifest = run_role_quorum(
            plan,
            lambda provider: consult_provider(provider, prompt, args.model, args.cwd),
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
    try:
        output = fn(prompt, model=args.model, cwd=args.cwd)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output)
            print(f"OK: {target} response written to {args.output}")
        else:
            print(output)
    except subprocess.TimeoutExpired:
        msg = f"Timeout: {target} did not respond within {tool_timeout}s"
        if args.output:
            Path(args.output).write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        msg = f"Error calling {target}: {e.stderr or e}"
        if args.output:
            Path(args.output).write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        msg = f"Error: {e}"
        if args.output:
            Path(args.output).write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

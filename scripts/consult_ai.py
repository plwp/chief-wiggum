#!/usr/bin/env python3
"""
Consult an AI tool with a prompt and capture its output.

Secrets are fetched from the system keyring at call time and passed directly
to SDK constructors — never set as env vars, never printed.

Usage:
    python3 consult_ai.py <tool> <prompt_file> [--output <file>] [--context <file>] [--model <model_id>]

Tools: codex, gemini, gemini-vertex, claude
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Allow importing keychain from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from keychain import get_secret

# Per-tool timeouts (seconds). These are generous — better to wait than to
# lose a good response to a premature timeout.
TOOL_TIMEOUTS: dict[str, int] = {
    "codex": 600,       # 10 minutes — xhigh reasoning is slow on large prompts
    "gemini": 600,      # 10 minutes
    "gemini-vertex": 600,
    "claude": 600,
}
TIMEOUT = 600  # fallback

# Default model for Vertex AI path (override with --model)
DEFAULT_VERTEX_MODEL = "gemini-3-pro"


def consult_codex(prompt: str, model: str | None = None) -> str:
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
    )
    return result.stdout


def consult_gemini(prompt: str, model: str | None = None) -> str:
    """Call gemini CLI. Uses its own auth session."""
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd.extend(["-m", model])
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True,
        timeout=TOOL_TIMEOUTS.get("gemini", TIMEOUT),
    )
    return result.stdout


def consult_gemini_vertex(prompt: str, model: str | None = None) -> str:
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


def consult_claude(prompt: str, model: str | None = None) -> str:
    """Call claude CLI. Uses its own auth session."""
    cmd = ["claude", "-p", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        check=True, timeout=TOOL_TIMEOUTS.get("claude", TIMEOUT),
    )
    return result.stdout


TOOLS = {
    "codex": consult_codex,
    "gemini": consult_gemini,
    "gemini-vertex": consult_gemini_vertex,
    "claude": consult_claude,
}


def main():
    parser = argparse.ArgumentParser(
        description="Consult an AI tool with a prompt.",
    )
    parser.add_argument("tool", choices=TOOLS.keys(), help="AI tool to consult")
    parser.add_argument("prompt_file", help="Path to the prompt file")
    parser.add_argument("-o", "--output", help="Write response to file instead of stdout")
    parser.add_argument("--context", help="Optional context file to append")
    parser.add_argument("--model", help="Override model ID for this call")
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Prompt file not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)

    prompt = prompt_path.read_text()

    if args.context:
        ctx_path = Path(args.context)
        if ctx_path.exists():
            prompt += f"\n\n---\nContext:\n{ctx_path.read_text()}"

    fn = TOOLS[args.tool]
    tool_timeout = TOOL_TIMEOUTS.get(args.tool, TIMEOUT)
    try:
        output = fn(prompt, model=args.model)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output)
            print(f"OK: {args.tool} response written to {args.output}")
        else:
            print(output)
    except subprocess.TimeoutExpired:
        msg = f"Timeout: {args.tool} did not respond within {tool_timeout}s"
        if args.output:
            Path(args.output).write_text(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        msg = f"Error calling {args.tool}: {e.stderr or e}"
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

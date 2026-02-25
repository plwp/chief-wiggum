#!/usr/bin/env python3
"""
Consult an AI tool with a prompt and capture its output.

Secrets are fetched from macOS Keychain at call time and passed directly
to SDK constructors — never set as env vars, never printed.

Usage:
    python3 consult_ai.py <tool> <prompt_file> [--context <file>]

Tools: codex, gemini, gemini-vertex, claude
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Allow importing keychain from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from keychain import get_secret


def consult_codex(prompt: str) -> str:
    """Call codex CLI. Uses its own auth session."""
    result = subprocess.run(
        ["codex", "-q", "--full-auto"],
        input=prompt, capture_output=True, text=True, check=True,
    )
    return result.stdout


def consult_gemini(prompt: str) -> str:
    """Call gemini CLI. Uses its own auth session."""
    result = subprocess.run(
        ["gemini"],
        input=prompt, capture_output=True, text=True, check=True,
    )
    return result.stdout


def consult_gemini_vertex(prompt: str) -> str:
    """Call Gemini via Vertex AI SDK. Fetches credentials from keychain."""
    project = get_secret("GOOGLE_CLOUD_PROJECT")
    location = get_secret("GOOGLE_CLOUD_LOCATION") or "us-central1"

    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT not found in keychain. "
              "Run: python3 scripts/keychain.py set GOOGLE_CLOUD_PROJECT",
              file=sys.stderr)
        sys.exit(1)

    # Import here so the dependency is only needed for this path
    from google.cloud import aiplatform  # type: ignore
    from vertexai.generative_models import GenerativeModel  # type: ignore

    aiplatform.init(project=project, location=location)
    model = GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(prompt)
    return response.text


def consult_claude(prompt: str) -> str:
    """Call claude CLI. Uses its own auth session."""
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, check=True,
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
    parser.add_argument("--context", help="Optional context file to append")
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
    try:
        output = fn(prompt)
        print(output)
    except subprocess.CalledProcessError as e:
        print(f"Error calling {args.tool}: {e.stderr or e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

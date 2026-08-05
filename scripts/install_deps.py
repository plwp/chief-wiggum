#!/usr/bin/env python3
"""
Install missing chief-wiggum dependencies.

Usage:
    python3 install_deps.py [--all | --cli | --python | --vertex | --tool NAME]
"""

import argparse
import importlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_log  # noqa: E402

CLI_TOOLS = {
    "claude": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "codex": ["npm", "install", "-g", "@openai/codex"],
    "gemini": ["npm", "install", "-g", "@google/gemini-cli"],
    "gh": ["brew", "install", "gh"],
    "tmux": ["brew", "install", "tmux"],
    "ffmpeg": ["brew", "install", "ffmpeg"],
}

PYTHON_PKGS = {
    "keyring": ("keyring", [[sys.executable, "-m", "pip", "install", "keyring"]]),
    "whisper": ("whisper", [[sys.executable, "-m", "pip", "install", "openai-whisper"]]),
    "browser-use": ("browser_use", [[sys.executable, "-m", "pip", "install", "browser-use"]]),
    "playwright": ("playwright", [
        [sys.executable, "-m", "pip", "install", "playwright"],
        [sys.executable, "-m", "playwright", "install", "chromium"],
    ]),
    "langchain-anthropic": (
        "langchain_anthropic",
        [[sys.executable, "-m", "pip", "install", "langchain-anthropic"]],
    ),
}

VERTEX_PKGS = {
    "langchain-google-vertexai": (
        "langchain_google_vertexai",
        [[sys.executable, "-m", "pip", "install", "langchain-google-vertexai"]],
    ),
    "google-cloud-aiplatform": (
        "google.cloud.aiplatform",
        [[sys.executable, "-m", "pip", "install", "google-cloud-aiplatform"]],
    ),
}


def _install_telemetry_capture():
    """Provision Claude-layer cost capture (chief-wiggum#345 AC1).

    Two things, both idempotent: a ``~/.chief-wiggum/otel/`` directory for
    operators who DO want the OTEL console-exporter route, and a bounded
    catch-up ingest over the transcript route — zero-config, retroactive, and
    the DEFAULT route (see docs/factory-telemetry.md; the OTEL route fights
    an interactive TUI session and suits headless runs only).

    Prints the OTEL opt-in snippet — NEVER writes ``~/.claude/settings.json``
    or a shell rc file. Those stay the operator's own files; this is the
    first time a chief-wiggum script would have touched a user dotfile, and
    the transcript route makes it unnecessary (see the #345 implementation
    plan's Open Question 1).
    """
    otel_dir = Path.home() / ".chief-wiggum" / "otel"
    otel_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Provisioned {otel_dir} (for operators who use the OTEL console-exporter route)")

    n = factory_log.ingest_claude_transcripts(since=time.time() - 7 * 86400)
    print(f"  Catch-up ingest: {n} new Claude Code turn(s) from the last 7 days")

    print(
        "\n  Optional, for headless/OTEL-pipeline runs; the transcript route above "
        "needs none of this. Print-only — apply it yourself if you want it:\n"
        "    export CLAUDE_CODE_ENABLE_TELEMETRY=1\n"
        "    export OTEL_METRICS_EXPORTER=console\n"
        "    export OTEL_LOGS_EXPORTER=console\n"
        "    export OTEL_METRIC_EXPORT_INTERVAL=10000\n"
        "    claude() { command claude \"$@\" "
        "2> >(tee -a ~/.chief-wiggum/otel/$(date +%s).jsonl >&2); }\n"
    )


# Provisioning actions that are neither a CLI tool install nor a pip install —
# a `name -> callable` shape kept separate from CLI_TOOLS/PYTHON_PKGS rather
# than forced into their `name -> argv` / `name -> (import_name, [argv])`
# shapes.
SETUP_ACTIONS = {
    "telemetry-capture": _install_telemetry_capture,
}


def run(cmd: list[str]) -> bool:
    print(f"  Running: {shlex.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"Error: command failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)
    return True


def install_cli_tools():
    for name, cmd in CLI_TOOLS.items():
        if not shutil.which(name):
            print(f"\nInstalling {name}...")
            run(cmd)
        else:
            print(f"  {name}: already installed")


def install_python_pkgs(pkgs: dict):
    for name, (import_name, cmds) in pkgs.items():
        try:
            importlib.import_module(import_name)
            print(f"  {name}: already installed")
        except ImportError:
            print(f"\nInstalling {name}...")
            for cmd in cmds:
                run(cmd)


def install_single(name: str):
    if name in SETUP_ACTIONS:
        SETUP_ACTIONS[name]()
    elif name in CLI_TOOLS:
        run(CLI_TOOLS[name])
    elif name in PYTHON_PKGS:
        for cmd in PYTHON_PKGS[name][1]:
            run(cmd)
    elif name in VERTEX_PKGS:
        for cmd in VERTEX_PKGS[name][1]:
            run(cmd)
    else:
        print(f"Unknown tool: {name}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Install chief-wiggum dependencies.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Install everything missing")
    group.add_argument("--cli", action="store_true", help="Install CLI tools only")
    group.add_argument("--python", action="store_true", help="Install Python packages only")
    group.add_argument("--vertex", action="store_true", help="Install Vertex AI packages")
    group.add_argument("--tool", type=str, help="Install a specific tool by name")
    args = parser.parse_args()

    if args.tool:
        install_single(args.tool)
    elif args.cli:
        install_cli_tools()
    elif args.python:
        install_python_pkgs(PYTHON_PKGS)
    elif args.vertex:
        install_python_pkgs(VERTEX_PKGS)
    else:  # --all or no args
        print("=== Installing all missing dependencies ===")
        install_cli_tools()
        install_python_pkgs(PYTHON_PKGS)

    print("\nDone. Run 'python3 scripts/check_deps.py' to verify.")


if __name__ == "__main__":
    main()

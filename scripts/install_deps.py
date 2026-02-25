#!/usr/bin/env python3
"""
Install missing chief-wiggum dependencies.

Usage:
    python3 install_deps.py [--all | --cli | --python | --vertex | --tool NAME]
"""

import argparse
import importlib
import shutil
import subprocess
import sys

CLI_TOOLS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
    "gemini": "npm install -g @anthropic-ai/gemini || npm install -g @google/gemini-cli",
    "gh": "brew install gh",
    "ffmpeg": "brew install ffmpeg",
}

PYTHON_PKGS = {
    "whisper": ("whisper", "pip3 install openai-whisper"),
    "browser-use": ("browser_use", "pip3 install browser-use"),
    "playwright": ("playwright", "pip3 install playwright && python3 -m playwright install chromium"),
    "langchain-anthropic": ("langchain_anthropic", "pip3 install langchain-anthropic"),
}

VERTEX_PKGS = {
    "langchain-google-vertexai": ("langchain_google_vertexai", "pip3 install langchain-google-vertexai"),
    "google-cloud-aiplatform": ("google.cloud.aiplatform", "pip3 install google-cloud-aiplatform"),
}


def run(cmd: str) -> bool:
    print(f"  Running: {cmd}")
    result = subprocess.run(cmd, shell=True)
    return result.returncode == 0


def install_cli_tools():
    for name, cmd in CLI_TOOLS.items():
        if not shutil.which(name):
            print(f"\nInstalling {name}...")
            run(cmd)
        else:
            print(f"  {name}: already installed")


def install_python_pkgs(pkgs: dict):
    for name, (import_name, cmd) in pkgs.items():
        try:
            importlib.import_module(import_name)
            print(f"  {name}: already installed")
        except ImportError:
            print(f"\nInstalling {name}...")
            run(cmd)


def install_single(name: str):
    if name in CLI_TOOLS:
        run(CLI_TOOLS[name])
    elif name in PYTHON_PKGS:
        run(PYTHON_PKGS[name][1])
    elif name in VERTEX_PKGS:
        run(VERTEX_PKGS[name][1])
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

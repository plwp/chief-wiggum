#!/usr/bin/env python3
"""
Check that all required dependencies are installed and report their versions.
Checks system keyring for secrets (never prints values).

Requires Python >= 3.11.
"""

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from keychain import has_secret

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"

pass_count = 0
fail_count = 0
warn_count = 0

WORKFLOW_REQUIREMENTS = {
    "base": {
        "cmds": {"claude", "codex", "gemini", "gh", "ffmpeg", "git"},
        "pkgs": {"keyring", "whisper"},
        "secrets": set(),
    },
    "implement": {
        "cmds": {"claude", "codex", "gemini", "gh", "git"},
        "pkgs": {"keyring", "browser-use", "playwright", "langchain-anthropic"},
        "secrets": {"ANTHROPIC_API_KEY"},
    },
    "transcribe": {
        "cmds": {"ffmpeg"},
        "pkgs": {"whisper"},
        "secrets": set(),
    },
    "vertex": {
        "cmds": set(),
        "pkgs": {"langchain-google-vertexai", "google-cloud-aiplatform"},
        "secrets": {"GOOGLE_CLOUD_PROJECT"},
    },
}


def check_cmd(name: str, cmd: str, version_flag: str = "--version", required: bool = True):
    global pass_count, fail_count, warn_count
    path = shutil.which(cmd)
    if path:
        try:
            result = subprocess.run(
                [cmd, version_flag], capture_output=True, text=True, timeout=10,
            )
            ver = (result.stdout or result.stderr).strip().split("\n")[0]
        except Exception:
            ver = "(installed)"
        print(f"{GREEN}[OK]{NC}  {name:<14s} {ver}")
        pass_count += 1
    else:
        if required:
            print(f"{RED}[MISSING]{NC}  {name:<14s} not found")
            fail_count += 1
        else:
            print(f"{YELLOW}[OPTIONAL]{NC}  {name:<14s} not found")
            warn_count += 1


def check_python_pkg(name: str, import_name: str, required: bool = True):
    global pass_count, fail_count, warn_count
    try:
        mod = importlib.import_module(import_name)
        ver = getattr(mod, "__version__", "installed")
        print(f"{GREEN}[OK]{NC}  {name:<14s} {ver}")
        pass_count += 1
    except ImportError:
        if required:
            print(f"{RED}[MISSING]{NC}  {name:<14s} python package not found")
            fail_count += 1
        else:
            print(f"{YELLOW}[OPTIONAL]{NC}  {name:<14s} not installed")
            warn_count += 1


def check_secret(name: str, required: bool = False):
    global pass_count, fail_count, warn_count
    if has_secret(name):
        print(f"{GREEN}[OK]{NC}  {name:<24s} keychain")
        pass_count += 1
    else:
        if required:
            print(f"{RED}[MISSING]{NC}  {name:<24s} not set")
            fail_count += 1
        else:
            print(f"{YELLOW}[NOT SET]{NC}  {name:<24s}")
            warn_count += 1


def is_required(kind: str, name: str, workflows: list[str]) -> bool:
    return any(name in WORKFLOW_REQUIREMENTS[workflow][kind] for workflow in workflows)


def main():
    parser = argparse.ArgumentParser(description="Check chief-wiggum dependencies.")
    parser.add_argument(
        "--for",
        dest="workflows",
        action="append",
        choices=sorted(WORKFLOW_REQUIREMENTS),
        default=[],
        help="Workflow to enforce. May be passed multiple times.",
    )
    args = parser.parse_args()
    workflows = args.workflows or ["base"]

    print("=== Chief Wiggum Dependency Check ===")
    print(f"Profile: {', '.join(workflows)}")

    print("\n--- CLI Tools ---")
    check_cmd("claude", "claude", "--version", is_required("cmds", "claude", workflows))
    check_cmd("codex", "codex", "--version", is_required("cmds", "codex", workflows))
    check_cmd("gemini", "gemini", "--version", is_required("cmds", "gemini", workflows))
    check_cmd("gh", "gh", "--version", is_required("cmds", "gh", workflows))
    check_cmd("ffmpeg", "ffmpeg", "-version", is_required("cmds", "ffmpeg", workflows))
    check_cmd("git", "git", "--version", is_required("cmds", "git", workflows))

    print("\n--- Python Packages ---")
    check_python_pkg("keyring", "keyring", is_required("pkgs", "keyring", workflows))
    check_python_pkg("whisper", "whisper", is_required("pkgs", "whisper", workflows))

    print("\n--- Python Packages (browser-use — optional, for /implement validation) ---")
    check_python_pkg("browser-use", "browser_use", is_required("pkgs", "browser-use", workflows))
    check_python_pkg("playwright", "playwright", is_required("pkgs", "playwright", workflows))
    check_python_pkg(
        "langchain-anthropic",
        "langchain_anthropic",
        is_required("pkgs", "langchain-anthropic", workflows),
    )

    print("\n--- Python Packages (Vertex AI — optional) ---")
    check_python_pkg(
        "langchain-google-vertexai",
        "langchain_google_vertexai",
        is_required("pkgs", "langchain-google-vertexai", workflows),
    )
    check_python_pkg(
        "google-cloud-aiplatform",
        "google.cloud.aiplatform",
        is_required("pkgs", "google-cloud-aiplatform", workflows),
    )

    print("\n--- Secrets (system keyring) ---")
    print("  (manage with: python3 scripts/keychain.py set|get|delete|list)")
    print()
    check_secret("ANTHROPIC_API_KEY", is_required("secrets", "ANTHROPIC_API_KEY", workflows))
    check_secret("OPENAI_API_KEY", is_required("secrets", "OPENAI_API_KEY", workflows))
    check_secret("GEMINI_API_KEY", is_required("secrets", "GEMINI_API_KEY", workflows))
    check_secret("GOOGLE_CLOUD_PROJECT", is_required("secrets", "GOOGLE_CLOUD_PROJECT", workflows))
    check_secret("GOOGLE_CLOUD_LOCATION", is_required("secrets", "GOOGLE_CLOUD_LOCATION", workflows))

    print(f"\n=== Results: {pass_count} ok, {fail_count} missing, {warn_count} warnings ===")

    if fail_count > 0:
        print("\nRun /setup to install missing dependencies.")
        sys.exit(1)


if __name__ == "__main__":
    main()

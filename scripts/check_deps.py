#!/usr/bin/env python3
"""
Check that all required dependencies are installed and report their versions.
Checks macOS Keychain for secrets (never prints values).
"""

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


def check_cmd(name: str, cmd: str, version_flag: str = "--version"):
    global pass_count, fail_count
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
        print(f"{RED}[MISSING]{NC}  {name:<14s} not found")
        fail_count += 1


def check_python_pkg(name: str, import_name: str):
    global pass_count, fail_count
    try:
        mod = importlib.import_module(import_name)
        ver = getattr(mod, "__version__", "installed")
        print(f"{GREEN}[OK]{NC}  {name:<14s} {ver}")
        pass_count += 1
    except ImportError:
        print(f"{RED}[MISSING]{NC}  {name:<14s} python package not found")
        fail_count += 1


def check_secret(name: str):
    global pass_count, warn_count
    if has_secret(name):
        print(f"{GREEN}[OK]{NC}  {name:<24s} keychain")
        pass_count += 1
    else:
        print(f"{YELLOW}[NOT SET]{NC}  {name:<24s}")
        warn_count += 1


def main():
    print("=== Chief Wiggum Dependency Check ===")

    print("\n--- CLI Tools ---")
    check_cmd("claude", "claude", "--version")
    check_cmd("codex", "codex", "--version")
    check_cmd("gemini", "gemini", "--version")
    check_cmd("gh", "gh", "--version")
    check_cmd("ffmpeg", "ffmpeg", "-version")
    check_cmd("git", "git", "--version")

    print("\n--- Python Packages ---")
    check_python_pkg("whisper", "whisper")
    check_python_pkg("browser-use", "browser_use")
    check_python_pkg("playwright", "playwright")
    check_python_pkg("langchain-anthropic", "langchain_anthropic")

    print("\n--- Python Packages (Vertex AI — optional) ---")
    check_python_pkg("langchain-google-vertexai", "langchain_google_vertexai")
    check_python_pkg("google-cloud-aiplatform", "google.cloud.aiplatform")

    print("\n--- Secrets (macOS Keychain) ---")
    print("  (manage with: python3 scripts/keychain.py set|get|delete|list)")
    print()
    check_secret("ANTHROPIC_API_KEY")
    check_secret("OPENAI_API_KEY")
    check_secret("GEMINI_API_KEY")
    check_secret("GOOGLE_CLOUD_PROJECT")
    check_secret("GOOGLE_CLOUD_LOCATION")

    print(f"\n=== Results: {pass_count} ok, {fail_count} missing, {warn_count} warnings ===")

    if fail_count > 0:
        print("\nRun /setup to install missing dependencies.")
        sys.exit(1)


if __name__ == "__main__":
    main()

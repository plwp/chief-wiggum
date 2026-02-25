#!/usr/bin/env bash
# Check that all required dependencies are installed and report their versions.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check_cmd() {
  local name="$1"
  local cmd="$2"
  local version_flag="${3:---version}"

  if command -v "$cmd" &>/dev/null; then
    local ver
    ver=$("$cmd" "$version_flag" 2>&1 | head -1) || ver="(installed)"
    printf "${GREEN}[OK]${NC}  %-14s %s\n" "$name" "$ver"
    PASS=$((PASS + 1))
  else
    printf "${RED}[MISSING]${NC}  %-14s not found\n" "$name"
    FAIL=$((FAIL + 1))
  fi
}

check_env() {
  local name="$1"
  if [ -n "${!name:-}" ]; then
    local masked="${!name:0:4}****"
    printf "${GREEN}[OK]${NC}  %-14s %s\n" "$name" "$masked"
    PASS=$((PASS + 1))
  else
    printf "${YELLOW}[NOT SET]${NC}  %-14s\n" "$name"
    WARN=$((WARN + 1))
  fi
}

check_python_pkg() {
  local name="$1"
  local import_name="${2:-$1}"
  if python3 -c "import $import_name" 2>/dev/null; then
    local ver
    ver=$(python3 -c "import $import_name; print(getattr($import_name, '__version__', 'installed'))" 2>/dev/null) || ver="installed"
    printf "${GREEN}[OK]${NC}  %-14s %s\n" "$name" "$ver"
    PASS=$((PASS + 1))
  else
    printf "${RED}[MISSING]${NC}  %-14s python package not found\n" "$name"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Chief Wiggum Dependency Check ==="
echo ""
echo "--- CLI Tools ---"
check_cmd "claude"     "claude"     "--version"
check_cmd "codex"      "codex"      "--version"
check_cmd "gemini"     "gemini"     "--version"
check_cmd "gh"         "gh"         "--version"
check_cmd "ffmpeg"     "ffmpeg"     "-version"
check_cmd "git"        "git"        "--version"

echo ""
echo "--- Python Packages ---"
check_python_pkg "whisper"      "whisper"
check_python_pkg "browser-use"  "browser_use"
check_python_pkg "playwright"   "playwright"
check_python_pkg "langchain-anthropic" "langchain_anthropic"

echo ""
echo "--- API Keys ---"
check_env "ANTHROPIC_API_KEY"
check_env "OPENAI_API_KEY"
check_env "GEMINI_API_KEY"

echo ""
echo "=== Results: ${PASS} ok, ${FAIL} missing, ${WARN} warnings ==="

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Run /setup to install missing dependencies."
  exit 1
fi

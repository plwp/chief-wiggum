#!/usr/bin/env bash
# Install missing chief-wiggum dependencies.
# Usage: ./install-deps.sh [--all | --cli | --python | --tool TOOL_NAME]
set -euo pipefail

install_cli_tool() {
  local name="$1"
  case "$name" in
    codex)
      echo "Installing codex via npm..."
      npm install -g @openai/codex
      ;;
    gemini)
      echo "Installing gemini via npm..."
      npm install -g @anthropic-ai/gemini || npm install -g @google/gemini-cli
      ;;
    gh)
      echo "Installing GitHub CLI via brew..."
      brew install gh
      ;;
    ffmpeg)
      echo "Installing ffmpeg via brew..."
      brew install ffmpeg
      ;;
    claude)
      echo "Claude Code should be installed via: npm install -g @anthropic-ai/claude-code"
      npm install -g @anthropic-ai/claude-code
      ;;
    *)
      echo "Unknown CLI tool: $name"
      return 1
      ;;
  esac
}

install_python_pkg() {
  local name="$1"
  case "$name" in
    whisper)
      echo "Installing openai-whisper..."
      pip3 install --user openai-whisper
      ;;
    browser-use)
      echo "Installing browser-use..."
      pip3 install --user browser-use
      ;;
    playwright)
      echo "Installing playwright..."
      pip3 install --user playwright
      python3 -m playwright install chromium
      ;;
    langchain-anthropic)
      echo "Installing langchain-anthropic..."
      pip3 install --user langchain-anthropic
      ;;
    *)
      echo "Unknown Python package: $name"
      return 1
      ;;
  esac
}

case "${1:---all}" in
  --all)
    echo "=== Installing all missing dependencies ==="
    for tool in claude codex gemini gh ffmpeg; do
      if ! command -v "$tool" &>/dev/null; then
        install_cli_tool "$tool" || true
      fi
    done
    for pkg in whisper browser-use playwright langchain-anthropic; do
      import_name="${pkg//-/_}"
      if ! python3 -c "import $import_name" 2>/dev/null; then
        install_python_pkg "$pkg" || true
      fi
    done
    ;;
  --cli)
    for tool in claude codex gemini gh ffmpeg; do
      if ! command -v "$tool" &>/dev/null; then
        install_cli_tool "$tool" || true
      fi
    done
    ;;
  --python)
    for pkg in whisper browser-use playwright langchain-anthropic; do
      import_name="${pkg//-/_}"
      if ! python3 -c "import $import_name" 2>/dev/null; then
        install_python_pkg "$pkg" || true
      fi
    done
    ;;
  --tool)
    install_cli_tool "${2:?Usage: --tool TOOL_NAME}" || install_python_pkg "$2"
    ;;
  *)
    echo "Usage: $0 [--all | --cli | --python | --tool TOOL_NAME]"
    exit 1
    ;;
esac

echo ""
echo "Done. Run 'bash scripts/check-deps.sh' to verify."

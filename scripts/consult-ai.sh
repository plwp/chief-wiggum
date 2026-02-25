#!/usr/bin/env bash
# Consult an AI tool with a prompt and capture its output.
# Usage: ./consult-ai.sh <tool> <prompt_file> [--context <file>]
#
# Tools: codex, gemini, claude
# Output goes to stdout. Errors go to stderr.
set -euo pipefail

TOOL="${1:?Usage: consult-ai.sh <codex|gemini|claude> <prompt_file> [--context <file>]}"
PROMPT_FILE="${2:?Usage: consult-ai.sh <tool> <prompt_file>}"
CONTEXT_FILE=""

shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)
      CONTEXT_FILE="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ ! -f "$PROMPT_FILE" ]; then
  echo "Prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

PROMPT=$(cat "$PROMPT_FILE")

if [ -n "$CONTEXT_FILE" ] && [ -f "$CONTEXT_FILE" ]; then
  CONTEXT=$(cat "$CONTEXT_FILE")
  PROMPT="$PROMPT

---
Context:
$CONTEXT"
fi

case "$TOOL" in
  codex)
    echo "$PROMPT" | codex -q --full-auto 2>/dev/null
    ;;
  gemini)
    echo "$PROMPT" | gemini 2>/dev/null
    ;;
  claude)
    echo "$PROMPT" | claude -p --output-format text 2>/dev/null
    ;;
  *)
    echo "Unknown tool: $TOOL (expected codex, gemini, or claude)" >&2
    exit 1
    ;;
esac

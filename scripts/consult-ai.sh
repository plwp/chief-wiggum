#!/usr/bin/env bash
# Consult an AI tool with a prompt and capture its output.
# Usage: ./consult-ai.sh <tool> <prompt_file> [--context <file>]
#
# Tools: codex, gemini, gemini-vertex, claude
# Output goes to stdout. Errors go to stderr.
#
# For gemini-vertex, requires GOOGLE_CLOUD_PROJECT and gcloud auth.
# Secrets are loaded from macOS Keychain via keychain.sh — never leaked to stdout.
set -euo pipefail

# Load secrets from keychain (env vars take precedence)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/keychain.sh"
cw_load_all

TOOL="${1:?Usage: consult-ai.sh <codex|gemini|gemini-vertex|claude> <prompt_file> [--context <file>]}"
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
  gemini-vertex)
    # Call Gemini via Vertex AI using the Python SDK
    PROJECT="${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT must be set for gemini-vertex}"
    LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
    python3 -c "
import sys
from google.cloud import aiplatform
from vertexai.generative_models import GenerativeModel
aiplatform.init(project='$PROJECT', location='$LOCATION')
model = GenerativeModel('gemini-2.0-flash')
response = model.generate_content(sys.stdin.read())
print(response.text)
" <<< "$PROMPT" 2>/dev/null
    ;;
  claude)
    echo "$PROMPT" | claude -p --output-format text 2>/dev/null
    ;;
  *)
    echo "Unknown tool: $TOOL (expected codex, gemini, gemini-vertex, or claude)" >&2
    exit 1
    ;;
esac

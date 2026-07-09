#!/bin/bash
# Daily runner for the VIC RFT scout (invoked by launchd).
# Default path: Playwright fetch (no Claude needed). If Cloudflare blocks the
# Playwright fetch in your environment, switch to the claude-in-chrome runner
# (see runner_prompt.md) by commenting the python line and using the claude one.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .venv/bin/activate

# --- Default: self-contained Playwright fetch -------------------------------
python scout.py

# --- Reliable alternative: drive your real Chrome via claude-in-chrome -------
# Requires the Claude Code CLI + the Claude-in-Chrome extension connected.
# claude -p "$(cat runner_prompt.md)" --dangerously-skip-permissions

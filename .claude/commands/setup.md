# Setup - Verify & Install Dependencies

Check that all tools required by chief-wiggum are installed and working.

## Usage
```
/setup
```

## Workflow

### Step 0: Resolve CW_HOME

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
```

### Step 1: Run dependency check

Run the check script to see what's installed and what's missing:

```bash
python3 $CW_HOME/scripts/check_deps.py --for core
```

Dependency checks are profile-based. Use only the profiles the current harness and workflow need. Rather than guessing the profiles, ask the checker to recommend them for the workflows and provider roles in play (`check_deps.py` is the source of truth for the mapping):

```bash
# Recommend the flags for a workflow + the provider roles it uses, then run the check:
RECO=$(python3 $CW_HOME/scripts/check_deps.py --recommend --workflow implement --role reviewer)
python3 $CW_HOME/scripts/check_deps.py $RECO
python3 $CW_HOME/scripts/check_deps.py --list-profiles   # see every available profile
```

The profiles map to flags like:

```bash
python3 $CW_HOME/scripts/check_deps.py --for core
python3 $CW_HOME/scripts/check_deps.py --for core --provider claude-code
python3 $CW_HOME/scripts/check_deps.py --for core --provider codex --provider gemini
python3 $CW_HOME/scripts/check_deps.py --for core --provider claude-interactive
python3 $CW_HOME/scripts/check_deps.py --for transcription
python3 $CW_HOME/scripts/check_deps.py --for browser-validation
python3 $CW_HOME/scripts/check_deps.py --for vertex
```

### Step 2: Report results

Present the results to the user in a clear summary table showing:
- Tool name, installed version, and status (OK / MISSING / NOT SET)
- Group by: CLI tools, Python packages, Keychain secrets

### Step 3: Offer to install missing dependencies

If anything is missing, ask the user if they want to install it. For each missing tool, explain what it's for:

- **codex**: OpenAI Codex CLI for multi-AI code consultation
- **gemini**: Google Gemini CLI for multi-AI code consultation
- **claude**: Claude Code CLI for Claude Code harness usage or interactive delegation
- **tmux**: Persistent terminal session manager for the Claude interactive delegate
- **whisper**: Local speech-to-text for `/transcribe`
- **browser-use**: AI-driven browser automation for E2E validation
- **playwright**: Browser automation framework (used by browser-use)
- **langchain-anthropic**: LLM integration for browser-use
- **gh**: GitHub CLI for issue/PR management
- **ffmpeg**: Media processing for extracting audio/screenshots

If the user agrees, run the installer for specific missing tools:

```bash
python3 $CW_HOME/scripts/install_deps.py --tool <tool_name>
```

For Vertex AI packages:
```bash
python3 $CW_HOME/scripts/install_deps.py --vertex
```

### Step 4: Set up secrets in Keychain

For any missing secrets, help the user store them securely in macOS Keychain. Secrets are NEVER stored as environment variables — they are fetched from Keychain at runtime by Python wrappers.

Show current keychain status:
```bash
python3 $CW_HOME/scripts/keychain.py list
```

To store each key (prompts securely, no echo):
```bash
python3 $CW_HOME/scripts/keychain.py set ANTHROPIC_API_KEY
python3 $CW_HOME/scripts/keychain.py set OPENAI_API_KEY
python3 $CW_HOME/scripts/keychain.py set GEMINI_API_KEY
```

Explain where to get each key:
- `ANTHROPIC_API_KEY`: https://console.anthropic.com/
- `OPENAI_API_KEY`: https://platform.openai.com/api-keys
- `GEMINI_API_KEY`: https://aistudio.google.com/apikey

For Vertex AI (alternative to GEMINI_API_KEY):
```bash
python3 $CW_HOME/scripts/keychain.py set GOOGLE_CLOUD_PROJECT
python3 $CW_HOME/scripts/keychain.py set GOOGLE_CLOUD_LOCATION
```

### Step 5: Final verification

Re-run the check script to confirm everything is green:

```bash
python3 $CW_HOME/scripts/check_deps.py --for core
```

For workflow-specific preflight checks:
```bash
python3 $CW_HOME/scripts/check_deps.py --for implement
python3 $CW_HOME/scripts/check_deps.py --for transcription
python3 $CW_HOME/scripts/check_deps.py --for core --provider claude-interactive
python3 $CW_HOME/scripts/check_deps.py --for vertex
```

Provider roles are configured in `$CW_HOME/config/providers.json`. Validate role dependencies by running the matching provider profiles before a workflow starts.

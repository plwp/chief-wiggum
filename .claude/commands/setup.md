# Setup - Verify & Install Dependencies

Check that all tools required by chief-wiggum are installed and working.

## Usage
```
/setup
```

## Workflow

### Step 1: Run dependency check

Run the check script to see what's installed and what's missing:

```bash
python3 ~/repos/chief-wiggum/scripts/check_deps.py
```

### Step 2: Report results

Present the results to the user in a clear summary table showing:
- Tool name, installed version, and status (OK / MISSING / NOT SET)
- Group by: CLI tools, Python packages, Keychain secrets

### Step 3: Offer to install missing dependencies

If anything is missing, ask the user if they want to install it. For each missing tool, explain what it's for:

- **codex**: OpenAI Codex CLI for multi-AI code consultation
- **gemini**: Google Gemini CLI for multi-AI code consultation
- **whisper**: Local speech-to-text for `/transcribe`
- **browser-use**: AI-driven browser automation for E2E validation
- **playwright**: Browser automation framework (used by browser-use)
- **langchain-anthropic**: LLM integration for browser-use
- **gh**: GitHub CLI for issue/PR management
- **ffmpeg**: Media processing for extracting audio/screenshots

If the user agrees, run the installer for specific missing tools:

```bash
python3 ~/repos/chief-wiggum/scripts/install_deps.py --tool <tool_name>
```

For Vertex AI packages:
```bash
python3 ~/repos/chief-wiggum/scripts/install_deps.py --vertex
```

### Step 4: Set up secrets in Keychain

For any missing secrets, help the user store them securely in macOS Keychain. Secrets are NEVER stored as environment variables — they are fetched from Keychain at runtime by Python wrappers.

Show current keychain status:
```bash
python3 ~/repos/chief-wiggum/scripts/keychain.py list
```

To store each key (prompts securely, no echo):
```bash
python3 ~/repos/chief-wiggum/scripts/keychain.py set ANTHROPIC_API_KEY
python3 ~/repos/chief-wiggum/scripts/keychain.py set OPENAI_API_KEY
python3 ~/repos/chief-wiggum/scripts/keychain.py set GEMINI_API_KEY
```

Explain where to get each key:
- `ANTHROPIC_API_KEY`: https://console.anthropic.com/
- `OPENAI_API_KEY`: https://platform.openai.com/api-keys
- `GEMINI_API_KEY`: https://aistudio.google.com/apikey

For Vertex AI (alternative to GEMINI_API_KEY):
```bash
python3 ~/repos/chief-wiggum/scripts/keychain.py set GOOGLE_CLOUD_PROJECT
python3 ~/repos/chief-wiggum/scripts/keychain.py set GOOGLE_CLOUD_LOCATION
```

### Step 5: Final verification

Re-run the check script to confirm everything is green:

```bash
python3 ~/repos/chief-wiggum/scripts/check_deps.py
```

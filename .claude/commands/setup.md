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
bash ~/repos/chief-wiggum/scripts/check-deps.sh
```

### Step 2: Report results

Present the results to the user in a clear summary table showing:
- Tool name, installed version, and status (OK / MISSING / NOT SET)
- Group by: CLI tools, Python packages, API keys

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
bash ~/repos/chief-wiggum/scripts/install-deps.sh --tool <tool_name>
```

### Step 4: Verify API keys

For any missing API keys, explain where to set them:
- `ANTHROPIC_API_KEY`: https://console.anthropic.com/
- `OPENAI_API_KEY`: https://platform.openai.com/api-keys
- `GEMINI_API_KEY`: https://aistudio.google.com/apikey

Suggest adding them to `~/.zshrc` or `~/.bashrc`.

### Step 5: Final verification

Re-run the check script to confirm everything is green:

```bash
bash ~/repos/chief-wiggum/scripts/check-deps.sh
```

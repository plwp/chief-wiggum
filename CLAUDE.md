# Chief Wiggum - Agentic SDLC Orchestration

Project-agnostic orchestration layer for AI-powered software development lifecycle.

## What This Repo Is

A collection of Claude Code skills (`/setup`, `/transcribe`, `/triage`, `/plan-sprint`, `/create-issue`, `/implement`, `/ship`) that orchestrate a full development pipeline: transcribe client conversations, triage requirements, plan sprints, create issues, implement with multi-AI consultation, test, review, validate, and ship PRs.

## Key Principles

- **Project-agnostic**: Skills reference "the target repo" — never hardcode project names
- **Human-in-the-loop**: User confirms at every checkpoint (requirements, approach, final review)
- **Skills are markdown prompts**: They instruct Claude Code what to do, not executable code
- **Scripts are thin wrappers**: `consult-ai.sh` calls codex/gemini with a prompt and captures output
- **Same prompt for all AIs**: codex, gemini, and opus get identical context. Value is in natural divergence, not roleplay
- **Browser-use stays in target repos**: `/implement` looks for and uses the target repo's browser-use setup
- **Worktree for implementation**: Sub-agents always work in isolated git worktrees

## Required Tools

- `claude` - Claude Code CLI
- `codex` - OpenAI Codex CLI
- `gemini` - Google Gemini CLI
- `gh` - GitHub CLI
- `ffmpeg` - Media processing
- `whisper` - OpenAI Whisper (local transcription)
- `playwright` - Browser automation (via target repo)
- `browser-use` - AI browser agent (via target repo)

## Authentication

CLI tools (`claude`, `codex`, `gemini`, `gh`) use their own login sessions — no API keys needed.

API keys are only required for SDK-level calls (browser-use, direct API scripts):

- `ANTHROPIC_API_KEY` - For browser-use (langchain-anthropic SDK)
- `OPENAI_API_KEY` - Optional, if calling OpenAI APIs directly
- `GEMINI_API_KEY` - Optional, if calling Gemini APIs directly

### Vertex AI (alternative to API keys for Google)

For Gemini and browser-use via GCP instead of API keys:

- `GOOGLE_CLOUD_PROJECT` - Your GCP project ID
- `GOOGLE_CLOUD_LOCATION` - Region (default: `us-central1`)
- Authenticate via `gcloud auth application-default login`
- Python packages: `langchain-google-vertexai`, `google-cloud-aiplatform`

Use `gemini-vertex` as the tool name in `consult-ai.sh` to route through Vertex AI.

## Repo Layout

```
.claude/commands/    # Claude Code skills (the core of this repo)
scripts/             # Shell/Python helpers called by skills
templates/           # Issue, PR, and review prompt templates
```

## Usage

Skills are invoked from any target repo that has chief-wiggum configured as a skill source:

```bash
# In your target repo's .claude/settings.local.json, add:
# { "commandDirs": ["~/repos/chief-wiggum/.claude/commands"] }

/setup                          # Verify dependencies
/transcribe path/to/audio.mp4   # Transcribe client conversation
/triage owner/repo              # Prioritise open issues
/plan-sprint owner/repo         # Interactive sprint planning
/create-issue owner/repo        # Create a GitHub issue
/implement owner/repo#42        # Full implementation loop for ticket #42
/ship                           # Create PR with mermaid diagrams
```

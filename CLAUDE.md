# Chief Wiggum - Agentic SDLC Orchestration

Project-agnostic orchestration layer for AI-powered software development lifecycle.

## What This Repo Is

A collection of Claude Code skills (`/setup`, `/transcribe`, `/triage`, `/plan-sprint`, `/create-issue`, `/implement`, `/ship`, `/update`) that orchestrate a full development pipeline: transcribe client conversations, triage requirements, plan sprints, create issues, implement with multi-AI consultation, test, review, validate, and ship PRs.

## Key Principles

- **Project-agnostic**: Skills reference "the target repo" — never hardcode project names or local paths
- **Auto-cloning**: Target repos are resolved and cloned via `gh` on demand, cached in `~/.chief-wiggum/repos/`
- **Human-in-the-loop**: User confirms at every checkpoint (requirements, approach, final review)
- **Skills are markdown prompts**: They instruct Claude Code what to do, not executable code
- **Scripts are Python**: All helpers are Python — no bash scripts
- **Secrets never touch env vars**: API keys are fetched from macOS Keychain at call time by Python wrappers and passed directly to SDK constructors. They are never set as environment variables, never printed, never logged. This prevents secrets from leaking into conversation history.
- **Same prompt for all AIs**: codex, gemini, and opus get identical context. Value is in natural divergence, not roleplay
- **Browser-use stays in target repos**: `/implement` looks for and uses the target repo's browser-use setup
- **Worktree for implementation**: Sub-agents always work in isolated git worktrees

## Requirements

- **Python >= 3.11** (for type hints and browser-use)

## Required Tools

- `claude` - Claude Code CLI
- `codex` - OpenAI Codex CLI
- `gemini` - Google Gemini CLI
- `gh` - GitHub CLI
- `ffmpeg` - Media processing
- `whisper` - OpenAI Whisper (local transcription)
- `playwright` - Browser automation (via target repo)
- `browser-use` - AI browser agent (via target repo)

## Secret Management

Secrets are stored in the **system keyring** (macOS Keychain, Linux SecretService, etc.) via the `keyring` Python library under the `chief-wiggum` service. They are NEVER stored as environment variables.

```bash
python3 scripts/keychain.py list                       # show status (not values)
python3 scripts/keychain.py set ANTHROPIC_API_KEY      # store (prompts securely)
python3 scripts/keychain.py delete ANTHROPIC_API_KEY   # remove
```

In Python scripts, secrets are loaded on demand:
```python
from keychain import get_secret
api_key = get_secret("ANTHROPIC_API_KEY")  # fetched from Keychain, never env
client = Anthropic(api_key=api_key)        # passed directly to constructor
```

### Required secrets (for SDK calls)

- `ANTHROPIC_API_KEY` - For browser-use (langchain-anthropic SDK)
- `OPENAI_API_KEY` - Optional, if calling OpenAI APIs directly
- `GEMINI_API_KEY` - Optional, if calling Gemini APIs directly

### Vertex AI (alternative to API keys for Google)

- `GOOGLE_CLOUD_PROJECT` - Your GCP project ID
- `GOOGLE_CLOUD_LOCATION` - Region (default: `us-central1`)
- Authenticate via `gcloud auth application-default login`

Use `gemini-vertex` as the tool name in `consult_ai.py` to route through Vertex AI.

## AI Models Reference

See `models.md` for current model IDs, library versions, and default choices. Refresh with `/update`.

## User Data Directory

Chief-wiggum stores all user-space data under `~/.chief-wiggum/`:

```
~/.chief-wiggum/
├── repos/           # Cached target repo clones
└── tmp/             # Temporary files (prompts, reviews, diffs)
```

Temp files go in `~/.chief-wiggum/tmp/`, **not** `/tmp/`. This keeps them isolated from other users/agents and makes cleanup easy.

## Path Resolution

**Chief-wiggum install path**: Never hardcode `~/repos/chief-wiggum`. Skills should resolve the install directory dynamically at the start of each session:

```bash
CW_HOME=$(python3 -c "from pathlib import Path; print(Path(__file__).resolve().parent.parent)" --fake 2>/dev/null || python3 scripts/repo.py home)
```

Or from any location, since `repo.py` computes it from its own `__file__`:

```bash
# From a skill that knows its own path:
CW_HOME=$(cd "$(dirname "$0")/../.." && pwd)
```

In practice, skills reference scripts as `python3 "$CW_HOME/scripts/..."` after resolving `CW_HOME` once.

## Target Repo Resolution

When a skill receives `owner/repo`, it resolves to a local path using `scripts/repo.py`:

1. If cwd is already inside the repo, use `git rev-parse --show-toplevel` for the root
2. If cached in `~/.chief-wiggum/repos/owner/repo`, pull latest and use that
3. Otherwise clone via `gh repo clone` into the cache

```bash
python3 "$CW_HOME/scripts/repo.py" resolve plwp/dgrd  # prints local path
python3 "$CW_HOME/scripts/repo.py" home                # prints chief-wiggum install dir
python3 "$CW_HOME/scripts/repo.py" list                # show cached repos
python3 "$CW_HOME/scripts/repo.py" clean plwp/dgrd     # remove cache
```

## Repo Layout

```
.claude/commands/    # Claude Code skills (the core of this repo)
scripts/             # Python helpers called by skills
templates/           # Issue, PR, and review prompt templates
models.md            # AI model IDs and library versions (refresh with /update)
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
/update                         # Refresh model IDs and library versions
```

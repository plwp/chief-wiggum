# Chief Wiggum

Agentic SDLC orchestration for Claude Code. A reusable set of skills that power an AI-driven software development lifecycle.

## Quick Start

```bash
# 1. Clone and verify
cd ~/repos/chief-wiggum
claude /setup

# 2. Add as skill source to your project
# In your-project/.claude/settings.local.json:
{
  "commandDirs": ["~/repos/chief-wiggum/.claude/commands"]
}

# 3. Use from your project
claude /transcribe ~/recordings/client-call.mp4
claude /triage owner/repo
claude /implement owner/repo#42
```

## Skills

| Skill | Purpose |
|-------|---------|
| `/setup` | Verify and install all dependencies |
| `/transcribe` | Whisper transcription → structured requirements |
| `/triage` | Read and prioritise GitHub issues |
| `/plan-sprint` | Interactive sprint planning session |
| `/create-issue` | Create well-structured GitHub issues |
| `/implement` | Full implementation loop with multi-AI consultation |
| `/ship` | PR creation with mermaid architecture diagrams |

## Pipeline

```
Audio/Video → /transcribe → /triage → /plan-sprint → /create-issue
                                                          ↓
                              /ship ← review ← /implement
```

## Requirements

- Claude Code CLI (`claude`)
- OpenAI Codex CLI (`codex`)
- Google Gemini CLI (`gemini`)
- GitHub CLI (`gh`)
- ffmpeg, openai-whisper (for transcription)
- API keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`

# Chief Wiggum

Agentic SDLC orchestration for Claude Code. A reusable set of skills that power an AI-driven software development lifecycle.

## Quick Start

```bash
# 1. Clone and verify
cd ~/repos/chief-wiggum
claude /setup

# 2. Add as skill source to your target project
# In your-project/.claude/settings.local.json:
{
  "commandDirs": ["~/repos/chief-wiggum/.claude/commands"]
}

# 3. Use from your target project directory (not chief-wiggum)
claude /transcribe ~/recordings/client-call.mp4
claude /triage owner/repo
claude /implement owner/repo#42
```

**Important**: Run skills from your target project directory, not from chief-wiggum itself.

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
| `/update` | Refresh AI model IDs and library versions |

## Pipeline

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
graph LR
    A["/transcribe"]:::entry --> B["/triage"]
    B --> C["/plan-sprint"]
    C --> D["/create-issue"]
    D --> E["/implement"]
    E --> F["/ship"]

    classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
    classDef default fill:#003f5c,stroke:#2f4b7c,color:#fff
```

## `/implement` — Orchestration Detail

```mermaid
%%{init: {'theme': 'default'}}%%
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant AI as Codex / Gemini / Opus
    participant S as Sonnet (worktree)

    U->>O: /implement owner/repo#42
    O->>O: Resolve paths & read ticket

    rect rgba(102, 81, 145, 0.25)
        note right of O: Step 4 — Multi-AI consultation
        par Approach gathering
            O->>AI: Codex consultation
            O->>AI: Gemini consultation
            O->>AI: Opus exploration
        end
        AI-->>O: Three approaches
        O->>AI: Opus reconciliation
        AI-->>O: Implementation plan
    end

    O-->>U: Approach summary (checkpoint)

    rect rgba(212, 80, 135, 0.25)
        note right of O: Step 5 — Implementation
        O->>S: Plan + feature branch
        S->>S: Code, test, lint, fix
        S-->>O: Done
    end

    rect rgba(102, 81, 145, 0.25)
        note right of O: Step 6 — Code review
        par Review
            O->>AI: Codex review
            O->>AI: Gemini review
        end
        AI-->>O: Review findings
    end

    O->>O: Apply fixes & verify independently
    O->>O: Browser-use / E2E validation
    O->>O: Create PR with mermaid diagrams
    O-->>U: PR link
```

## Architecture

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
graph TD
    subgraph "Chief Wiggum"
        Skills[".claude/commands/"]:::entry
        Scripts["scripts/"]:::modified
        Templates["templates/"]:::modified
    end

    subgraph "AI Backends"
        Codex["OpenAI Codex"]:::existing
        Gemini["Google Gemini"]:::existing
        Opus["Claude Opus"]:::existing
    end

    subgraph "Target Repo (worktree)"
        Code["Source code"]:::new
        Tests["Test suite"]:::new
        BU["Browser-use / E2E"]:::new
    end

    subgraph "Infrastructure"
        GH["GitHub CLI"]:::existing
        Keyring["System Keyring"]:::existing
        Whisper["Whisper"]:::existing
    end

    Skills --> Scripts
    Skills --> Templates
    Scripts -->|consult_ai.py| Codex
    Scripts -->|consult_ai.py| Gemini
    Scripts -->|sub-agent| Opus
    Scripts -->|repo.py| GH
    Scripts -->|keychain.py| Keyring
    Scripts -->|transcribe| Whisper
    Scripts -->|implement in| Code
    Code --> Tests
    Code --> BU

    classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
    classDef existing fill:#003f5c,stroke:#2f4b7c,color:#fff
    classDef modified fill:#665191,stroke:#a05195,color:#fff
    classDef new fill:#d45087,stroke:#f95d6a,color:#fff
```

## Requirements

- **Python >= 3.11**
- Claude Code CLI (`claude`)
- OpenAI Codex CLI (`codex`)
- Google Gemini CLI (`gemini`)
- GitHub CLI (`gh`)
- ffmpeg, openai-whisper (for transcription)
- Secrets stored in system keyring (managed via `python3 scripts/keychain.py`)

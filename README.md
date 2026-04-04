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

### Epic Level
| Skill | Purpose |
|-------|---------|
| `/plan-epic` | Group related issues into an epic with dependency graph and integration risks |
| `/architect` | Define contracts, invariants, state machines, ADRs, and integration tests for an epic |
| `/close-epic` | Epic-level quality gate: integration tests, mutation testing, stitch-audit, retrospective |

### Ticket Level
| Skill | Purpose |
|-------|---------|
| `/implement` | TDD implementation loop: test-first → multi-AI consultation → structured review → verify |

### Supporting
| Skill | Purpose |
|-------|---------|
| `/setup` | Verify and install all dependencies |
| `/transcribe` | Whisper transcription → structured requirements |
| `/triage` | Read and prioritise GitHub issues |
| `/seed` | Architecture brainstorm and issue seeding for new projects |
| `/create-issue` | Create well-structured GitHub issues |
| `/ship` | PR creation with mermaid architecture diagrams |
| `/stitch-audit` | Cross-layer data flow analysis |
| `/update` | Refresh AI model IDs and library versions |

## Pipeline

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
graph TD
    subgraph "Input"
        A["/transcribe"]:::entry
        B["/seed"]:::entry
        C["/triage"]:::default
        D["/create-issue"]:::default
    end

    subgraph "Epic Flow"
        E["/plan-epic"]:::modified
        F["/architect"]:::new
        G["/implement<br/>(per ticket)"]:::modified
        H["/close-epic"]:::new
    end

    A --> D
    B --> C
    C --> E
    D --> E
    E --> F
    F --> G
    G --> G
    G --> H

    classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
    classDef default fill:#003f5c,stroke:#2f4b7c,color:#fff
    classDef modified fill:#665191,stroke:#a05195,color:#fff
    classDef new fill:#d45087,stroke:#f95d6a,color:#fff
```

## `/implement` — Orchestration Detail

```mermaid
%%{init: {'theme': 'dark'}}%%
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant AI as Codex / Gemini / Opus
    participant S as Sonnet (worktree)

    U->>O: /implement owner/repo#42
    O->>O: Resolve paths, load epic context

    rect rgba(102, 81, 145, 0.25)
        note right of O: Step 4 — Multi-AI consultation
        par Approach gathering
            O->>AI: Codex consultation
            O->>AI: Gemini consultation
            O->>AI: Opus exploration
        end
        AI-->>O: Three approaches
        O->>AI: Opus reconciliation (+ epic contracts)
        AI-->>O: Implementation plan
    end

    O-->>U: Approach summary (checkpoint)

    rect rgba(212, 80, 135, 0.25)
        note right of O: Step 5 — Test-first specification
        O->>S: Write failing tests (TDD red phase)
        S-->>O: Tests written, all failing
    end

    rect rgba(212, 80, 135, 0.25)
        note right of O: Step 6 — Implementation
        O->>S: Make tests pass + enforce contracts
        S->>S: Code, lint, fix
        S-->>O: All tests green
    end

    rect rgba(102, 81, 145, 0.25)
        note right of O: Step 7 — Structured review
        par Review (with checklist)
            O->>AI: Codex review
            O->>AI: Gemini review
        end
        AI-->>O: Checklist scorecard + findings
    end

    O->>O: Static analysis gate
    O->>O: Apply fixes & verify independently
    O->>O: Verify contract enforcement
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

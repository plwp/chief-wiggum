# Chief Wiggum - Agentic SDLC Orchestration

Project-agnostic orchestration layer for AI-powered software development lifecycle.

This file is the Claude Code adapter guide. Harness-neutral instructions live in `AGENTS.md`, and cross-harness install guidance lives in `docs/harnesses.md`.

## What This Repo Is

A collection of portable workflow contracts, scripts, and Claude Code slash-command adapters that orchestrate a full development pipeline at two levels:

- **Product level**: `/design` — runs once per product between `/seed` and epic planning. Divergent rendered HTML mockups → human picks a direction → tokens mechanically extracted into `docs/design/` (design.json, approved mockups, reference screenshots), which `/architect` folds into epic ui-specs and the design-fidelity gate compares built screens against.
- **Epic level**: `/plan-epic` → `/architect` → (implement tickets) → `/close-epic` — defines contracts, invariants, and integration tests before implementation, validates cross-cutting quality after.
- **Ticket level**: `/implement` — TDD, multi-AI consultation, structured review, static analysis, and independent verification per ticket.
- **Wave level**: `/implement-wave` — parallel implementation of an entire epic in dependency-ordered waves. Each wave runs multiple `/implement` loops concurrently in isolated worktrees, merges to main, then starts the next wave.
- **Supporting**: `/setup`, `/transcribe`, `/seed`, `/create-issue`, `/ship`, `/update`, `/stitch-audit`, `/code-metrics`, `/tutorial-video`.

## Key Principles

- **Own the solution, not just the code**: The validation loop is not negotiable. Before shipping, ask: "Am I proud of this? Is it clean and elegant?" If not, fix it.
- **Orchestrator verifies independently**: Never trust a sub-agent's self-reported results. The orchestrator must run tests, start services, and hit endpoints itself. Sub-agents optimise for speed and will take shortcuts.
- **Never punt to the user**: If Docker isn't running, start it. If a dependency is missing, install it. "Want to skip?" is never the right question.
- **Project-agnostic**: Skills reference "the target repo" — never hardcode project names or local paths
- **Auto-cloning**: Target repos are resolved and cloned via `gh` on demand, cached in `~/.chief-wiggum/repos/`
- **Two-tier quality**: Epic-level contracts and invariants prevent cross-ticket bugs; ticket-level TDD and structured review prevent per-ticket bugs
- **Test-first**: Write failing tests before implementation code. The objective is "make these tests pass", not "implement this feature"
- **Contracts are executable**: Every REQUIRES/ENSURES from `/architect` becomes a runtime guard in the code. The review checklist verifies this
- **Traceability is mechanical, not trusted**: contracts and invariants get stable IDs (`CTR-`/`INV-`/`BR-`); code and tests link to them with `@cw-trace guards/ensures/verifies` annotations. `scripts/check_traceability.py` builds the business-rule → contract → code → test graph and reports orphans, uncovered/untested contracts, and dangling links — gated in `/architect` (soundness) and `/close-epic` (coverage). See `docs/traceability.md`
- **Single write paths are inventoried, not trusted**: an invariant that declares a "single write path"/"single source of truth" for a field or state names its `controls_field` + `sanctioned_writers` (structured on the `state-machines.json` invariant, or via a `@cw-writes` tag in `invariants.md`). `scripts/check_single_writer.py` scans the target repo for EVERY writer of the controlled field (Go/Mongo-aware — assignments, struct-literal sets, bson `$set`, SQL UPDATE) and flags any writer outside the sanctioned set — gated in `/architect` (soundness: metadata well-formed) and `/close-epic` (coverage: hard-fail on unsanctioned writers). This catches the class of bug traceability and the ratchet cannot see: a legacy mutator (e.g. an admin `ChangePlan` dropdown) silently becoming a second writer of a single-write-path field. See `docs/single-writer.md`
- **Quality ratchets, never slides**: the test pass-set that has ever passed on main is a high-water mark that may not shrink, and a contract can't "pass" by weakening its definition (stable-ID blocks are hashed). `scripts/ratchet.py` gates `/implement`, `/implement-wave`, and `/close-epic`; the journal is an append-only hash chain, so lowering the bar is tamper-evident and fails closed. Workers can't touch the goalposts (contracts, specs, ratchet state) — such diffs are parked for the human. See `docs/ratchet.md`
- **Unknowns gate work**: Facts that can't be confirmed against a real source are marked `TBD:`/`UNRESOLVED:` in artifacts. `scripts/check_unresolved.py` detects them; `/implement-wave` refuses to build dependent tickets on a guess
- **Gates prove precision before they block**: a hard-fail gate that is noisy on real code is worse than no gate — the operator learns to `--force` past it, eroding trust in every gate. Every gate script is report-only by default (prints findings, exits 0) and only blocks when a workflow passes `--gate`. A NEW gate ships report-only and is validated on a real, already-shipped repo before it is wired as a blocker in `/architect` or `/close-epic`. See `docs/gate-rollout.md`
- **Ground truth before contracts**: For products on existing data sources, `/seed` ingests the semantic layer, physical schema, and transformation-repo history into `docs/domain-context.md` before `/architect` writes data contracts
- **The loop must look at the UI**: "Build + tests green" never closes a frontend ticket. `/architect` writes a visual design contract (ui-spec `design` section: tokens, component-library binding, reference screenshots); `/implement` Step 9 renders the app, screenshots it, and reviews against that contract
- **Designs are chosen, not converged**: `/design` generates 3–4 deliberately distinct rendered directions and a human picks — one generated design converges to the model's default taste. Tokens are extracted mechanically from the approved mock's CSS (`scripts/extract_design.py`), so the contract can't drift from what was approved
- **Human-in-the-loop**: User confirms at every checkpoint (requirements, approach, final review)
- **Workflow instructions are markdown prompts**: In Claude Code they are slash commands; in other harnesses they should be packaged as portable skills or native adapter metadata
- **Scripts are Python**: All helpers are Python — no bash scripts
- **Secrets never touch env vars**: API keys are fetched from macOS Keychain at call time by Python wrappers and passed directly to SDK constructors. They are never set as environment variables, never printed, never logged. This prevents secrets from leaking into conversation history.
- **Same prompt for all AIs**: codex, gemini, and opus get identical context. Value is in natural divergence, not roleplay
- **Browser-use stays in target repos**: `/implement` looks for and uses the target repo's browser-use setup
- **Worktree for implementation**: Sub-agents always work in isolated git worktrees
- **Validate before acting**: Never assume a root cause — always test the hypothesis first. When debugging, reproduce the failure, verify the fix, then move on. Do not make speculative changes based on untested assumptions.

## Requirements

- **Python >= 3.11** (for type hints and browser-use)

## Required Tools

Chief Wiggum dependency checks are profile-based:

- `core` - `gh`, `git`, and Python keyring support
- `claude-code` - Claude Code CLI for Claude Code harness usage
- `codex` - OpenAI Codex CLI provider
- `gemini` - Google Gemini CLI provider
- `claude-interactive` - Claude Code CLI plus `tmux` for interactive delegation
- `transcription` - ffmpeg and OpenAI Whisper
- `browser-validation` - browser-use, Playwright, and Anthropic browser-use integration
- `vertex` - Vertex AI packages and project configuration
- `go-lsp` - `gopls` + Go toolchain, for semantic code intelligence in `/implement` (optional; `scripts/lsp_query.py`)
- `python-lsp` - `pyright-langserver`, for Python semantic code intelligence (optional; same helper)

Example:

```bash
python3 scripts/check_deps.py --for core --provider claude-interactive
```

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

## Provider Roles

Provider roles live in `config/providers.json`. Use `scripts/consult_ai.py` directly for one provider, or `--role <role> --output-dir <dir>` for a configured quorum:

```bash
python3 scripts/consult_ai.py codex prompt.md -o response.md
python3 scripts/consult_ai.py --role reviewer prompt.md --output-dir "$CW_TMP/reviews"
```

Roles define required and optional providers. Required providers must succeed; optional providers may be disabled or fail without blocking the role quorum. This keeps Claude, Codex, Gemini, and interactive delegates configurable rather than hard-coded into workflow logic.

## User Data Directory

Chief-wiggum stores all user-space data under `~/.chief-wiggum/`:

```
~/.chief-wiggum/
├── repos/           # Cached target repo clones
└── tmp/             # Temporary files (prompts, reviews, diffs)
    └── <session-id>/ # Per-session subdirectory to avoid collisions
```

Temp files go in `~/.chief-wiggum/tmp/`, **not** `/tmp/`. Each session must create a **unique subdirectory** to avoid collisions when multiple sessions run concurrently:

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
```

All temp file references (`approach-prompt.md`, `approach-codex.md`, etc.) go inside `$CW_TMP`. Per-ticket files go in `$CW_TMP/<ticket-number>/` to avoid collisions when implementing multiple tickets in one session (see `/implement` Step 1).

## Path Resolution

**Chief-wiggum install path**: Skills should resolve the install directory at the start of each session. `CHIEF_WIGGUM_HOME` can override the common checkout path:

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
```

In practice, skills reference scripts as `python3 "$CW_HOME/scripts/..."` after resolving `CW_HOME` once. Use `python3 "$CW_HOME/scripts/env.py" tmp` for session temp directories and `python3 "$CW_HOME/scripts/env.py" slug "$epic_name"` for `docs/epics/<slug>` paths.

## Target Repo Resolution

When a skill receives `owner/repo`, it resolves to a local path using `scripts/repo.py`:

1. If cwd is already inside the repo, use `git rev-parse --show-toplevel` for the root
2. If cached in `~/.chief-wiggum/repos/owner/repo`, pull latest and use that
3. Otherwise clone via `gh repo clone` into the cache

```bash
python3 "$CW_HOME/scripts/repo.py" resolve acme/app  # prints local path
python3 "$CW_HOME/scripts/repo.py" home               # prints chief-wiggum install dir
python3 "$CW_HOME/scripts/repo.py" list               # show cached repos
python3 "$CW_HOME/scripts/repo.py" clean acme/app     # remove cache
```

## Repo Layout

```
.claude/commands/    # Claude Code slash-command adapter
skills/              # Harness-portable skills and bundled resources
scripts/             # Python helpers called by skills
templates/           # Issue, PR, review, and checklist templates
patterns/            # Registry of reusable product patterns CW stamps into built apps (see docs/patterns-registry.md)
models.md            # AI model IDs and library versions (refresh with /update)
```

### Epic artifacts (in target repos)

`/architect` commits artifacts to `docs/epics/[slug]/` in the target repo:
```
docs/epics/order-lifecycle/
├── contracts.md          # REQUIRES/ENSURES for APIs and entities
├── state-machines.md     # Valid states and transitions
├── invariants.md         # Cross-cutting rules
├── adr.md                # Architectural Decision Record
├── integration-tests.md  # Cross-ticket test specifications
├── traceability.md       # AC → test mapping
└── retrospective.md      # Written by /close-epic
```

### Product design artifacts (in target repos)

`/design` commits artifacts to `docs/design/` in the target repo:
```
docs/design/
├── design.json        # Binding tokens + component-library + assets + voice (ui-spec design format)
├── mockups/           # Approved HTML mockups — living reference implementations
├── reference/         # Screenshots of approved mockups — the design-fidelity gate's baseline
└── styleguide.html    # Rendered token sheet
```

## Usage

Skills are invoked from any target repo that has chief-wiggum configured as a skill source:

```bash
# In your target repo's .claude/settings.local.json, add:
# { "commandDirs": ["~/repos/chief-wiggum/.claude/commands"] }

/setup                          # Verify dependencies
/transcribe path/to/audio.mp4   # Transcribe client conversation
/create-issue owner/repo        # Create a GitHub issue
/seed owner/repo                # Architecture brainstorm & issue seeding
/design owner/repo              # Product design: mockups → human choice → docs/design/
/apply-pattern owner/repo --pattern <id>  # Install a registry pattern's invariant-cluster contract pack

# Epic flow (the core loop)
/plan-epic owner/repo           # Group issues into epic with dependency graph
/architect owner/repo --epic "Epic: Name"  # Define contracts, invariants, tests
/implement owner/repo#42        # TDD implementation loop for a single ticket
/implement-wave owner/repo --epic "Epic: Name"  # Parallel implementation in waves
/close-epic owner/repo --epic "Epic: Name" # Epic-level quality gate

/ship                           # Create PR with mermaid diagrams (standalone)
/stitch-audit owner/repo --trace keyword   # Cross-layer data flow audit
/code-metrics owner/repo                    # Literature-grounded code-quality metrics (churn/complexity/survival/duplication)
/tutorial-video owner/repo --feature "..."  # Narrated click-through tutorial video
/saas-gate owner/repo --base-url <url>     # SaaS non-functional-requirements gate (security/isolation/perf/observability)
/update                         # Refresh model IDs and library versions
```

Harness-portable skills live under `skills/`. Install them into Codex with a symlink, for example:

```bash
ln -sfn ~/repos/chief-wiggum/skills/claude-interactive-delegate ~/.codex/skills/claude-interactive-delegate
```

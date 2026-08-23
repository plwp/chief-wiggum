# Chief Wiggum - Agentic SDLC Orchestration

Project-agnostic orchestration layer for AI-powered software development lifecycle.

This file is the Claude Code adapter guide. Harness-neutral instructions live in `AGENTS.md`, and cross-harness install guidance lives in `docs/harnesses.md`.

## What This Repo Is

A collection of portable workflow contracts, scripts, and Claude Code slash-command adapters that orchestrate a full development pipeline at two levels:

- **Product level**: `/design` — runs once per product between `/seed` and epic planning. Divergent rendered HTML mockups → human picks a direction → tokens mechanically extracted into `docs/design/` (design.json, approved mockups, reference screenshots), which `/architect` folds into epic ui-specs and the design-fidelity gate compares built screens against.
- **Epic level**: `/plan-epic` → `/architect` → (implement tickets) → `/close-epic` — defines contracts, invariants, and integration tests before implementation, validates cross-cutting quality after.
- **Ticket level**: `/implement` — TDD, multi-AI consultation, structured review, static analysis, and independent verification per ticket.
- **Wave level**: `/implement-wave` — parallel implementation of an entire epic in dependency-ordered waves. Each wave runs multiple `/implement` loops concurrently in isolated worktrees, merges to main, then starts the next wave.
- **Supporting**: `/setup`, `/transcribe`, `/seed`, `/create-issue`, `/adopt`, `/ship`, `/update`, `/stitch-audit`, `/code-metrics`, `/status`, `/ux-review`, `/tutorial-video`, `/business-consultant`.

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
- **Architecture knowledge is queried live, never cached or paraphrased**: `scripts/code_query.py` answers "what governs this file/field/contract" from the epic artifacts (`contracts.json`/`state-machines.json`/`transition-map.json`/`ui-spec.json`) joined with code annotations — a **locator**, not a content store, returning stable-ID handles (`file:line`), never re-serialized contract bodies. `orient` binds by ARTIFACT (operation path, ui-spec route, transition-map `code_location`) as well as by `@cw-trace` annotation, so an un-annotated handler still gets a real answer; a path that can't be scanned reports `unscanned`, never the same empty `facts: []` a genuinely-clean file gets. `/implement` and `/implement-wave` call it instead of re-deriving architecture from scratch each session. See `docs/code-query.md`
- **Meta location is resolved, never assumed**: every skill and gate asks `scripts/artifacts.py` where a target's CW meta lives — `embedded` (the default: `<target>/docs/`) or `sidecar` (an identical tree under `~/.chief-wiggum/meta/<owner>/<repo>/`, elected once per target and recorded outside the target). In sidecar mode goalpost edits cannot ride in a worker's reviewed diff (no path in the target tree — the boundary is the diff, not the disk: workers are not filesystem-sandboxed, see `docs/sidecar.md` "Trust boundary"), and the domain `scope.json` splits gate findings into in-domain (blocking-eligible) vs boundary (reported, never blocking). `/status` shows the resolved state live. See `docs/sidecar.md`
- **Quality ratchets, never slides**: the test pass-set that has ever passed on main is a high-water mark that may not shrink, and a contract can't "pass" by weakening its definition (stable-ID blocks are hashed). `scripts/ratchet.py` gates `/implement`, `/implement-wave`, and `/close-epic`; the journal is an append-only hash chain, so lowering the bar is tamper-evident and fails closed. Workers can't touch the goalposts (contracts, specs, ratchet state) — such diffs are parked for the human. See `docs/ratchet.md`
- **Debt is remediated on a budget, with a mechanical acceptance test**: `/plan-epic --from-debt` clusters `DEBT-` items (clone class → module → change-coupling) into refactor tickets and REFUSES to plan without an explicit budget — leftover inventory is the normal end state. `refactor` tickets invert TDD (characterization tests pin current behavior BEFORE code changes); on adopted repos every ticket kind declares a touch pathset and files mid-ticket discoveries as `DEBT-` candidates instead of fixing them in-diff (found ≠ fixed); `/close-epic` re-runs the inventory and hard-checks every ticketed id is gone. Boundary findings are referred to the owning team, never auto-fixed. See `docs/remediation.md`
- **Unknowns gate work, and an external interface is an unknown until someone looks at it**: Facts that can't be confirmed against a real source are marked `TBD:`/`UNRESOLVED:` in artifacts (plus `UNVERIFIED` where it introduces a claim — bare `UNVERIFIED` is domain vocabulary, not a marker). `scripts/check_unresolved.py` detects them; `/implement-wave` refuses to build dependent tickets on a guess. That discipline used to fire for DATA unknowns and not for SHAPE: an epic shipped nine guessed tool schemas and their routes behind a prose caveat ("best-effort approximation… not a verified contract"), carried 45 `TBD:` markers all on data, and every live call 404'd. So a contracts operation marked `"external": true` must cite provenance somebody actually saw — `observed_fact` or `api_doc`; a `ticket` cite records who ASKED for the interface, never that anyone looked at it. `scripts/check_interface_provenance.py` gates it report-only in `/architect`, and because `external` is optional by design it prints a named **declaration gap** rather than a clean zero when an epic declares operations and marks none external. The sibling proposal — a hedge-prose lint on "best-effort"/"assumed"/"approximation" — was measured against 314 shipped artifacts and rejected: `best-effort` alone fired 84 times, every hit a real design property. Presence of a cited source is decidable; intent in prose is not. See `docs/interface-provenance.md`
- **Gates prove precision before they block**: a hard-fail gate that is noisy on real code is worse than no gate — the operator learns to `--force` past it, eroding trust in every gate. Every gate script is report-only by default (prints findings, exits 0) and only blocks when a workflow passes `--gate`. A NEW gate ships report-only and is validated on a real, already-shipped repo before it is wired as a blocker in `/architect` or `/close-epic`. See `docs/gate-rollout.md`
- **Gate validation is a protocol, not a convention**: "validated on a real, already-shipped repo" is mechanized as a per-gate `validation/<gate>.json` record — seeded-defect trials (including mandatory evasion classes: omission, config indirection, sampling gaps, concurrency where applicable, plus instrumentation-deleted for telemetry-dependent gates), clean-corpus runs with coverage evidence, and an authority-boundary statement. `scripts/check_gate_validation.py` enforces it; `/close-epic` refuses `--gate` for a checker lacking a passing record. Results are journaled via the existing ratchet hash chain (no signing/DSSE). A production escape whose `--seed-class` matches a class the gate's record certified it catches triggers a DEMOTION (`factory_log.py bug`): revert the gate to report-only and file a ticket to re-derive that seed class. `scripts/gate_validation_designer.py` (report-only always, #218) audits the records against the mandatory seed-class matrix, extracts independently-versioned seeds files (`docs/quality/validation/seeds/`), runs the independent escape intake, and proposes new seeds via mutation testing. See `docs/gate-validation.md`
- **Ground truth before contracts**: For products on existing data sources, `/seed` ingests the semantic layer, physical schema, and transformation-repo history into `docs/domain-context.md` before `/architect` writes data contracts
- **CW discloses its own AI authorship, and classifies what it builds, under the EU AI Act**: two distinct subjects. CW's own posture — Art. 2(12) FOSS exemption, turning on whether Art. 50 applies to it — is recorded in `docs/ai-act-posture.md` (chief-wiggum#317); `chief_wiggum.ai_disclosure` mechanizes the mitigation (a trailer on generated commits, a line in generated PR/issue bodies) so the most arguable limb never has to be won. Separately, every product CW builds gets its AI functionality classified in `docs/compliance/ai-act.json` (chief-wiggum#316) — decoupled from the regulated-*data* trigger in `docs/compliance-requirements.md`, since the Act keys on AI functionality and market reach, not data sensitivity. `scripts/check_ai_act.py` gates the in-force layer (Art. 5 prohibitions, Art. 50 transparency) report-only; the Chapter III high-risk conformity pack is parked until harmonised standards exist. Art. 6(4) makes the classification itself load-bearing: its absence must read as "never assessed", never as a silent "not high risk"
- **A test double must be recorded, not invented**: when one worker authors the code and its fake from a single assumption, the test validates the assumption rather than reality — TDD-with-fakes makes the fake the spec. A fixture routed by `TrimPrefix(path, "/api/gx-agent/")` and looked up by tool name, the exact same wrong assumption as the client it stood in for; both agreed, every test was green, and the route bug was invisible until a real call. So a double for a declared external system carries `@cw-fixture <system> capture=<path>` naming the real interaction it came from, and the capture carries `captured_at`/`source` so an invented file cannot satisfy the check as well as a real one. `scripts/record_capture.py` takes the capture; `scripts/check_fixture_provenance.py` gates it report-only in `/close-epic`. See `docs/fixture-provenance.md`
- **Config that declares tools is not an agent that uses them**: an agent config was built with its tools declared and the engine tests asserted the tools were PASSED to the model — all green — but with no system instruction the model never called one and answered "I can't retrieve that". Nothing tested the BEHAVIOUR. `scripts/check_behavioral_eval.py` (report-only in `/close-epic`) reads the target's `docs/quality/behavioral-evals.json` golden set and its RESULTS, and asserts the expected tool was actually called. A case that passed without recording `called_tools` is `unverified`, never `verified` — exit status is not behaviour. See `docs/behavioral-eval.md`
- **A build that compiles is not a service that serves**: `go build ./...` and green package tests never compose the binary and ask it for a route. A duplicate `mux.Handle("/")` panicked at startup and was found on deploy as a crash-loop, with `cmd/server` carrying no test files at all, and two declared routes that were never wired. `scripts/check_boot_and_hit.py` probes every non-`external` declared operation against a RUNNING instance (report-only in `/close-epic` and `/implement-wave`) — reaching the base URL is itself the startup check. It refuses two things on purpose: it never fires a mutating request without `--probe-mutating` (a gate that damages the system it inspects is worse than no gate) and it never invents a path parameter (a 404 from a guessed id means "no such record" as readily as "not registered"). No `--base-url` is `inapplicable`, never a pass. See `docs/boot-and-hit.md`
- **"Fails closed without the secret" and "works with the secret" are independent properties**: a gate that only tests the first will pass an endpoint nobody can ever call. A prod PSK was provisioned with `echo`, so its value carried a trailing `0x0a`; the service compared the header untrimmed, and an HTTP header value can never end in a newline — the endpoint was structurally unreachable by every caller from the day it shipped, and nobody knew for a month, because every check read "401 without the secret = pass". Staging's secret happened to lack the newline. So `saas_gate.py` probes each documented internal PSK endpoint for CALLABILITY with the provisioned secret (an endpoint that 401s both with and without it is the signature of this defect), secret-shaped getters `TrimSpace` on read, and every provisioning instruction CW ships uses `printf %s` — never `echo`, which `tests/test_secret_provisioning.py` enforces. The same one byte breaks webhook HMAC verification and any outbound `Authorization: Bearer` header (chief-wiggum#370)
- **An integration nobody exercised is not a working integration**: unit tests go green without ever touching the real system, so every external system an epic declares (`"external": true`, see above) needs ONE real round-trip, marked `@cw-smoke <system> [case=<test>]`. Four production bugs in a single epic — a turn-flow bug, a missing system prompt, a trailing-newline token that made `Bearer <token>\n` an invalid HTTP header, and a guessed route — each needed exactly one real end-to-end interaction to surface, and none was required to pass. `scripts/check_external_smoke.py` gates it report-only in `/close-epic`, and the states it keeps apart are the whole point: a SKIPPED smoke (credentials absent, build tag off) is **`unverified`**, a loud visible gap, never the green tick a passing suite would imply. It parses junit itself rather than reusing the ratchet's parser, because that one collapses `skipped` in with `failed` — the exact conflation the gate exists to prevent. See `docs/external-smoke.md`
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
"${CW_PY:-python3}" scripts/check_deps.py --for core --provider claude-interactive
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

**Optional providers fail fast, not slow**: the `claude-interactive` delegate's own budget is a generous 1800s (`TOOL_TIMEOUTS` in `scripts/consult_ai.py`) — appropriate when it's the one voice a step is waiting on, wasteful when it's merely an optional third opinion a role can run without. A role's `optional_timeout_seconds` (`config/providers.json`) caps how long an OPTIONAL provider's delegate call may run before `consult_ai.py`'s role quorum abandons it; every shipped role sets it to `300`. Unset, it falls back to `consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS`. This only affects the claude-interactive delegate (tool providers already run well under that budget) and only when a provider is in the role's `optional` list — a required provider always gets the delegate's full 1800s. See chief-wiggum#188.

**Distribution entropy is opt-in, not the default quorum**: the `divergence` role (`deepseek`, `kimi` required; `glm`, `qwen`, `minimax` optional) reaches frontier non-Western models over the OpenRouter HTTP API (`openrouter` tool in `scripts/consult_ai.py`, key from the keyring as `OPENROUTER_API_KEY`). Its purpose is to widen the quorum's *pretraining distribution*, not its prompting — the usual providers cluster on Anthropic/OpenAI/Google priors, so on generative questions (strategy, naming, positioning) they converge for reasons that have nothing to do with the question being settled. The divergence role itself stays opt-in: it is a second opinion you ask for on purpose, not a tax on every consult. Separately from that role, `deepseek-flash` (`deepseek/deepseek-v4-flash` over the same OpenRouter path) fills the code-quorum seat Gemini vacated — required in `reviewer`/`risky_diff_review`, optional in `explorer`/`architecture_critic` — chosen for cost and speed, not distribution entropy. It honestly declares `reads_repo=false`/`needs_inline_diff=true`, so those roles' prompts must stay self-contained; `gemini-vertex` remains only where images are sent (`design_critic`), because every OpenRouter provider is `accepts_images=false`. Two caveats — it is a plain API call with **no repo, filesystem, or web access** (prompts must be self-contained, so it cannot review a diff), and the role sets no lenses, because same-prompt-different-distribution is where the value is (see "Same prompt for all AIs" above). Asking the same question in Mandarin is a second, independent entropy axis on the same models.

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
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
CW_TMP=$("$CW_PY" "$CW_HOME/scripts/env.py" tmp)
```

All temp file references (`approach-prompt.md`, `approach-codex.md`, etc.) go inside `$CW_TMP`. Per-ticket files go in `$CW_TMP/<ticket-number>/` to avoid collisions when implementing multiple tickets in one session (see `/implement` Step 1).

## Path Resolution

**Chief-wiggum install path**: Skills should resolve the install directory at the start of each session. `CHIEF_WIGGUM_HOME` can override the common checkout path:

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
```

**Runtime interpreter**: a bare `python3` is whatever the shell happens to resolve — an unpinned, per-machine accident. Homebrew bumping `python3` from 3.11 to 3.13 silently strands every dependency installed for the old one, and CW discovers it as `ModuleNotFoundError: keyring` inside a backgrounded consult, where the exit is instant and the output file never appears (chief-wiggum#374). `env.py python` resolves and caches a **validated** interpreter — one that actually has the profile's imports — preferring a `CW_PYTHON` override.

The rule, which `tests/test_skill_interpreter_pinning.py` enforces: **every python invocation in a skill runs under `$CW_PY`**, with exactly two exceptions — the two bootstrap calls above (chicken-and-egg; `env.py` imports nothing outside the stdlib), and anything running the TARGET repo's own tooling (`cd "$TARGET_REPO" && python3 tests/browser-use/run.py`, `python3 "$TUT_STATUS_SCRIPT"`), which belongs to the target's interpreter and not CW's.

Call sites use `"${CW_PY:-python3}"` rather than `"$CW_PY"`, so a bash block run in a fresh shell that never saw the bootstrap degrades to exactly today's behaviour instead of failing on an empty command.

In practice, skills reference scripts as `"${CW_PY:-python3}" "$CW_HOME/scripts/..."` after resolving `CW_HOME` and `CW_PY` once. Use `"${CW_PY:-python3}" "$CW_HOME/scripts/env.py" tmp` for session temp directories and `"${CW_PY:-python3}" "$CW_HOME/scripts/env.py" slug "$epic_name"` for `docs/epics/<slug>` paths.

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

**Template placeholder convention** (chief-wiggum#347). The two brace styles in
`templates/` mean different things, and the split is deliberate rather than
accidental:

- `{{DOUBLE_BRACE}}` — **machine-substituted**. A script fills it before the
  text is used (e.g. `{{KEYWORD}}`, `{{DIFF_REPORT}}` in the prompt templates).
  Leaving one unsubstituted is a bug: it ships the literal token to a model.
- `{SINGLE_BRACE}` — **human-filled**. A person completes it when adopting the
  template (e.g. `{PRODUCT}`, `{GOVERNING_STANDARD}` in
  `compliance-requirements.md`). These are expected to survive copying and are
  filled in the target repo.

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
/adopt owner/repo               # Brownfield entry: survey → elect → real-test baseline → grandfather → adoption record

# Epic flow (the core loop)
/plan-epic owner/repo           # Group issues into epic with dependency graph
/architect owner/repo --epic "Epic: Name"  # Define contracts, invariants, tests
/implement owner/repo#42        # TDD implementation loop for a single ticket
/implement-wave owner/repo --epic "Epic: Name"  # Parallel implementation in waves
/close-epic owner/repo --epic "Epic: Name" # Epic-level quality gate

/ship                           # Create PR with mermaid diagrams (standalone)
/stitch-audit owner/repo --trace keyword   # Cross-layer data flow audit
/code-metrics owner/repo                    # Literature-grounded code-quality metrics (churn/complexity/survival/duplication)
/status owner/repo                          # Live one-screen target state: footprint mode, scope, gate ledger, ratchet, patterns, debt
/ux-review owner/repo [--base-url <url>]    # Behavioural product walk-through across personas → severity-ranked findings
/reflect owner/repo                         # Factory self-assessment: mine a built repo → CW-improvement issues
/tutorial-video owner/repo --feature "..."  # Narrated click-through tutorial video
/saas-gate owner/repo --base-url <url>     # SaaS non-functional-requirements gate (security/isolation/perf/observability)
/business-consultant owner/repo             # Unit economics + pricing-model fit from adopted patterns + stack cost tiers
/update                         # Refresh model IDs and library versions
```

Harness-portable skills live under `skills/`. Install them into Codex with a symlink, for example:

```bash
ln -sfn ~/repos/chief-wiggum/skills/claude-interactive-delegate ~/.codex/skills/claude-interactive-delegate
```

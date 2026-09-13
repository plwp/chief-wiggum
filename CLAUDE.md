# Chief Wiggum - Agentic SDLC Orchestration

Project-agnostic orchestration layer for AI-powered software development lifecycle.

This file is the Claude Code adapter guide. Harness-neutral instructions live in `AGENTS.md`, and cross-harness install guidance lives in `docs/harnesses.md`. The reasoning behind each principle below lives in the linked doc, not here.

## What This Repo Is

Portable workflow contracts, scripts, and Claude Code slash-command adapters that orchestrate a full development pipeline:

- **Product level**: `/design` — divergent rendered mockups → human picks → tokens extracted mechanically into `docs/design/`.
- **Epic level**: `/plan-epic` → `/architect` → (implement tickets) → `/close-epic` — contracts, invariants, and integration tests before implementation; cross-cutting gates after.
- **Ticket level**: `/implement` — TDD, multi-AI consultation, structured review, static analysis, independent verification.
- **Wave level**: `/implement-wave` — an epic's tickets in dependency-ordered waves, each wave running `/implement` loops in parallel worktrees.
- **Supporting**: `/setup`, `/transcribe`, `/seed`, `/create-issue`, `/adopt`, `/ship`, `/update`, `/stitch-audit`, `/code-metrics`, `/status`, `/ux-review`, `/tutorial-video`, `/business-consultant`.

## Key Principles

- **Own the solution, not just the code.** The validation loop is not negotiable; before shipping ask "is this clean, and have I verified it works end-to-end?"
- **The orchestrator verifies independently.** A sub-agent's self-reported result is a claim: run the tests, start the services, hit the endpoints yourself.
- **Never punt to the user.** Docker down → start it; dependency missing → install it.
- **Project-agnostic.** Skills reference "the target repo"; never hardcode project names or local paths. Target repos are cloned via `gh` on demand into `~/.chief-wiggum/repos/`.
- **Test-first.** Failing tests before implementation; the objective is "make these tests pass".
- **Contracts are executable.** Every REQUIRES/ENSURES from `/architect` becomes a runtime guard; the review checklist verifies it.
- **Traceability is mechanical.** `CTR-`/`INV-`/`BR-` stable IDs, `@cw-trace` annotations, `scripts/check_traceability.py` builds the graph and reports orphans — `docs/traceability.md`.
- **Single write paths are inventoried.** A single-writer invariant names `controls_field` + `sanctioned_writers`; `scripts/check_single_writer.py` finds every other writer — `docs/single-writer.md`.
- **Architecture knowledge is queried live, never cached or paraphrased.** `scripts/code_query.py` is a locator returning `file:line` handles, not a content store — `docs/code-query.md`.
- **Meta location is resolved, never assumed.** `scripts/artifacts.py` says whether a target's CW meta is `embedded` (`<target>/docs/`) or `sidecar` (`~/.chief-wiggum/meta/<owner>/<repo>/`) — `docs/sidecar.md`.
- **Quality ratchets, never slides.** The pass-set that has passed on main may not shrink; contract definitions are hashed; the journal is an append-only hash chain; workers can't touch goalposts — `docs/ratchet.md`.
- **Debt is remediated on a budget.** `/plan-epic --from-debt` refuses to plan without one; `refactor` tickets pin behavior with characterization tests first; found ≠ fixed on adopted repos — `docs/remediation.md`.
- **Unknowns gate work.** `TBD:`/`UNRESOLVED:` markers block dependent tickets (`scripts/check_unresolved.py`); an `"external": true` operation must cite `observed_fact` or `api_doc` provenance (`scripts/check_interface_provenance.py`) — `docs/interface-provenance.md`.
- **Gates prove precision before they block.** Every gate is report-only by default and blocks only under `--gate`; a new gate is validated on a real repo before it is wired as a blocker — `docs/gate-rollout.md`. Validation is a per-gate `validation/<gate>.json` record with seeded-defect trials; an escape in a certified class demotes the gate — `docs/gate-validation.md`.
- **Ground truth before contracts.** `/seed` ingests the semantic layer and physical schema into `docs/domain-context.md` before `/architect` writes data contracts.
- **AI-Act posture.** CW discloses its own AI authorship (`chief_wiggum.ai_disclosure`, `docs/ai-act-posture.md`) and classifies each product's AI functionality in `docs/compliance/ai-act.json` (`scripts/check_ai_act.py`); an absent classification reads as "never assessed", never "not high risk".
- **A test double is recorded, not invented.** A fake for a declared external system carries `@cw-fixture <system> capture=<path>` pointing at a real capture — `docs/fixture-provenance.md`.
- **Behaviour, not configuration.** `scripts/check_behavioral_eval.py` asserts the expected tool was actually called; a case without `called_tools` is `unverified` — `docs/behavioral-eval.md`.
- **A build that compiles is not a service that serves.** `scripts/check_boot_and_hit.py` probes declared operations against a running instance; it never fires mutating requests without `--probe-mutating` and never invents path parameters — `docs/boot-and-hit.md`.
- **Callability is checked with the secret, not just without it.** `saas_gate.py` probes PSK endpoints with the provisioned secret; secret getters `TrimSpace`; provisioning uses `printf %s`, never `echo` (chief-wiggum#370).
- **Every external system gets one real round-trip.** `@cw-smoke <system>`; a skipped smoke is `unverified`, never a pass — `docs/external-smoke.md`.
- **The loop must look at the UI.** `/architect` writes a visual design contract; `/implement` Step 9 screenshots and reviews against it.
- **Designs are chosen, not converged.** `/design` renders 3–4 distinct directions; tokens are extracted mechanically from the approved mock (`scripts/extract_design.py`).
- **Human-in-the-loop** at requirements, approach, and final review. Everything else runs autonomously.
- **Workflow instructions are markdown prompts** (slash commands here; portable skills elsewhere). **Scripts are Python** — no bash scripts.
- **Secrets never touch env vars.** Fetched from the system keyring at call time by Python wrappers and passed to SDK constructors; never printed or logged.
- **Same prompt for all AIs.** Value is in natural divergence, not roleplay.
- **Browser-use stays in target repos.** Sub-agents always work in isolated worktrees.
- **Validate before acting.** Reproduce the failure, verify the fix, then move on.

## Requirements

- **Python >= 3.11**

## Required Tools

Dependency checks are profile-based (`core`, `claude-code`, `codex`, `gemini`, `claude-interactive`, `transcription`, `browser-validation`, `vertex`, `go-lsp`, `python-lsp`):

```bash
"${CW_PY:-python3}" scripts/check_deps.py --for core --provider claude-interactive
```

## Secret Management

Secrets live in the system keyring under the `chief-wiggum` service, never in environment variables:

```bash
python3 scripts/keychain.py list                       # show status (not values)
python3 scripts/keychain.py set ANTHROPIC_API_KEY      # store (prompts securely)
python3 scripts/keychain.py delete ANTHROPIC_API_KEY   # remove
```

```python
from keychain import get_secret
api_key = get_secret("ANTHROPIC_API_KEY")  # fetched from the keyring, never env
client = Anthropic(api_key=api_key)        # passed directly to the constructor
```

Secrets: `ANTHROPIC_API_KEY` (browser-use); optionally `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`. Vertex AI: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (default `us-central1`), `gcloud auth application-default login`; use `gemini-vertex` as the tool name in `consult_ai.py`.

## AI Models Reference

See `models.md` for current model IDs, library versions, and default choices. Refresh with `/update`.

## Provider Roles

Roles live in `config/providers.json`; workflows name a role, never a vendor:

```bash
python3 scripts/consult_ai.py codex prompt.md -o response.md                        # one provider
python3 scripts/consult_ai.py --role reviewer prompt.md --output-dir "$CW_TMP/reviews"  # a configured quorum
```

Full detail (role semantics, timeouts, the divergence role, secrets) is in the `provider-consult` skill, `.claude/skills/provider-consult/SKILL.md`. Required providers must succeed; optional ones may fail without blocking. `optional_timeout_seconds` caps an optional delegate (chief-wiggum#188). The `divergence` role (non-Western models over OpenRouter) is opt-in and exists to widen the quorum's pretraining distribution; `deepseek-flash` fills the code-quorum seat gemini vacated. OpenRouter providers are `reads_repo=false` — prompts to them must be self-contained.

## User Data Directory

```
~/.chief-wiggum/
├── repos/           # Cached target repo clones
└── tmp/             # Temporary files, one <session-id>/ subdirectory per session
```

Temp files go in `$CW_TMP`, never `/tmp/`; per-ticket files in `$CW_TMP/<ticket-number>/`.

## Path Resolution

Every skill resolves the install dir and the interpreter once at session start:

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
CW_TMP=$("${CW_PY:-python3}" "$CW_HOME/scripts/env.py" tmp)
```

**Every python invocation in a skill runs under `"${CW_PY:-python3}"`** (`tests/test_skill_interpreter_pinning.py`), with two exceptions: the two bootstrap calls above, and the target repo's own tooling (`cd "$TARGET_REPO" && python3 ...`). `env.py python` resolves and caches a validated interpreter, preferring a `CW_PYTHON` override (chief-wiggum#374). Use `env.py tmp` for session temp dirs and `env.py slug "$epic_name"` for `docs/epics/<slug>` paths.

## Target Repo Resolution

`scripts/repo.py` resolves `owner/repo` to a local path: cwd inside the repo → `git rev-parse --show-toplevel`; cached under `~/.chief-wiggum/repos/owner/repo` → pull; otherwise `gh repo clone` into the cache.

```bash
python3 "$CW_HOME/scripts/repo.py" resolve acme/app  # prints local path
python3 "$CW_HOME/scripts/repo.py" list               # show cached repos
python3 "$CW_HOME/scripts/repo.py" clean acme/app     # remove cache
```

## Repo Layout

Slash-command adapters in `.claude/commands/`, portable skills in `skills/`, Python helpers in `scripts/`, templates in `templates/`, the pattern registry in `patterns/` (see `docs/patterns-registry.md`), model ids in `models.md`.

**Template placeholders** (chief-wiggum#347): `{{DOUBLE_BRACE}}` is machine-substituted (leaving one unsubstituted is a bug); `{SINGLE_BRACE}` is human-filled and expected to survive copying.

### Epic artifacts (in target repos)

`/architect` commits to `docs/epics/[slug]/`: `contracts.md`, `state-machines.md`, `invariants.md`, `adr.md`, `integration-tests.md`, `traceability.md`, plus `models/` (`contracts.json`, `state-machines.json`, `ui-spec.json`, …); `/close-epic` writes `retrospective.md`. The JSON models are the source of truth; the prose is rendered from them.

### Product design artifacts (in target repos)

`/design` commits to `docs/design/`: `design.json` (binding tokens, component library, assets, voice), `mockups/` (approved HTML), `reference/` (screenshots — the design-fidelity baseline), `styleguide.html`.

## Usage

Skills are invoked from any target repo with chief-wiggum configured as a skill source (`.claude/settings.local.json`: `{ "commandDirs": ["~/repos/chief-wiggum/.claude/commands"] }`):

One file per `/command` under `.claude/commands/`; the listing (name and one-line description) is resident in every session, so it is not repeated here.

Harness-portable skills live under `skills/`; install into Codex with a symlink:

```bash
ln -sfn ~/repos/chief-wiggum/skills/claude-interactive-delegate ~/.codex/skills/claude-interactive-delegate
```

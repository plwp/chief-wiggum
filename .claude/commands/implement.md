# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → **test-first specification** → implement → **static analysis** → structured review → apply fixes → **verify** → validate → ship.

## Ownership

**You own the solution, not just the code.** Before shipping ANY implementation, ask yourself:
- Am I proud of this work?
- Is it clean and elegant?
- Have I verified it actually works end-to-end?

If the answer to any of these is no — fix it. Don't ship "good enough."

**The validation loop is not negotiable.** Workers will take shortcuts. The orchestrator is the quality gate. Never trust a worker's self-reported "tests pass" — independently verify.

**Never punt to the user.** If Docker isn't running, start it. If a dependency is missing, install it. If you can't run the tests, that's YOUR problem to solve. "Want to skip this step?" is never the right question.

**Every step is mandatory.** You do NOT get to decide that a change is "too small" to warrant code review, or that consultations are "good enough" with only 2 of 3 responses. The process exists for a reason — follow it completely every time, no exceptions. Specifically:
- **Never skip the multi-AI code review** (Step 7), regardless of change size. A one-line fix gets the same review process as a 500-line feature. No developer gets to self-certify their own code.
- **Never skip AI consultations** (Step 4). Wait for ALL consultations (Codex, Gemini, Opus) to complete. If one times out, retry it. Never proceed to reconciliation with partial results.
- **Never skip browser-use/E2E validation** (Step 10) unless `--skip-browser-use` was explicitly passed by the user.
- **Never create a PR before review is complete.** The PR is the final artifact (Step 11), not an intermediate checkpoint.

## Autonomy

**Run the full pipeline autonomously.** Do NOT pause between steps to ask "ready to proceed?" or "want to skip this?". Move through every step without asking for permission unless you genuinely need user input (e.g., ambiguous requirements in Step 2, a design decision where approaches conflict and there's no clear winner, or a blocking error you can't resolve).

Checkpoints where you MUST get user input:
- **Step 3** (Clarify requirements): Only if requirements are genuinely unclear or ambiguous
- **Step 4 Phase B** (Approach reconciliation): Only if approaches fundamentally conflict with no clear winner — present the trade-off and ask
- **Step 11** (Final check): Present the summary, then proceed to ship unless the user intervenes

Everything else — just do it.

## Usage
```
/implement <owner/repo#number> [--skip-browser-use]
```

## Parameters
- `owner/repo#number`: GitHub issue to implement (e.g., `acme/app#42`)
- `--skip-browser-use`: Skip browser-use validation step (useful if target repo has no browser-use setup)

## Workflow

### Step 1: Resolve paths and load epic context

**Prevent sleep**: Start `caffeinate` to keep the machine awake for the duration of the workflow:
```bash
caffeinate -ims &
CAFFEINATE_PID=$!
```
Kill it when the workflow completes (or fails): `kill $CAFFEINATE_PID 2>/dev/null`

Resolve the chief-wiggum install directory and the target repo path. **Never hardcode paths.**

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# One tested call resolves CW_HOME, CW_TMP, TARGET_REPO, DEFAULT_BRANCH, ISSUE_NUMBER.
# Capture first and check status so a resolver failure aborts instead of
# continuing with unset/stale variables.
CW_CTX=$(python3 "$CW_HOME/scripts/workflow_context.py" "$owner_repo#$issue_number" --shell) || {
  echo "workflow_context failed for $owner_repo#$issue_number" >&2; exit 1; }
eval "$CW_CTX"
```

**Important**: `$CW_TMP` uses a unique session ID so concurrent `/implement` runs don't clobber each other's temp files.

Create a **ticket-scoped subdirectory** for all per-ticket artifacts. When implementing multiple tickets in one session, this prevents file collisions (e.g., `approach-codex.md` for ticket #417 being overwritten by ticket #418):

```bash
TICKET_TMP="$CW_TMP/$issue_number"
mkdir -p "$TICKET_TMP"
```

All per-ticket files (`approach-prompt.md`, `approach-codex.md`, `approach-gemini.md`, `approach-opus.md`, `implementation-plan.md`, `review-prompt.md`, `reviews/reviewer-*.md`, `impl-diff.txt`) go in `$TICKET_TMP`, not `$CW_TMP`. Shared session files (e.g., epic context) remain in `$CW_TMP`.

**Load epic context** (if this ticket belongs to an epic):

```bash
# Find the ticket's milestone
MILESTONE=$(gh issue view "$issue_number" --repo "$owner_repo" --json milestone -q '.milestone.title // empty')
if [ -n "$MILESTONE" ]; then
  EPIC_SLUG=$(python3 "$CW_HOME/scripts/env.py" slug "$MILESTONE")
  EPIC_DIR="$TARGET_REPO/docs/epics/$EPIC_SLUG"
fi
```

If a milestone exists and `$EPIC_DIR/` exists in the target repo, load:
- `contracts.md` — REQUIRES/ENSURES for APIs and entities
- `state-machines.md` — valid state transitions
- `invariants.md` — cross-cutting rules
- `traceability.md` — which acceptance criteria need which tests

Also check for **formal model artifacts** in `$EPIC_DIR/models/`:
- `contracts.json` — structured contracts (machine-readable)
- `state-machines.json` — structured state machines (machine-readable)
- `ui-spec.json` — UI specification (pages, components, interactions, navigation)
- `test-paths.json` — mechanically generated test paths
- `test-plan.md` — test plan with positive/negative cases
- `test_state_machine.py` — Hypothesis RuleBasedStateMachine skeleton
- `transition-map.json` — transition ↔ ticket mapping (updated by `/implement`)

Build the artifact inventory once with the tested helper, then read its flags (it discovers prose/model/design artifacts, validates model JSON, and runs the unresolved-marker scan in one pass):
```bash
python3 "$CW_HOME/scripts/epic_inventory.py" "$TARGET_REPO" --epic-slug "${EPIC_SLUG:-}" --issue "$issue_number" > "$TICKET_TMP/inventory.json"
HAS_FORMAL_MODELS=$(jq -r '.flags.HAS_FORMAL_MODELS' "$TICKET_TMP/inventory.json")
HAS_UI_SPEC=$(jq -r '.flags.HAS_UI_SPEC' "$TICKET_TMP/inventory.json")
HAS_TRANSITION_MAP=$(jq -r '.flags.HAS_TRANSITION_MAP' "$TICKET_TMP/inventory.json")
[ "$HAS_FORMAL_MODELS" = "true" ] && MODELS_DIR="$EPIC_DIR/models"
```
The inventory's `blocked_tickets` and `warnings` (e.g. malformed model JSON) feed the unresolved-unknowns gate below.

These artifacts are **hard constraints** on the implementation. The coding worker MUST satisfy them. The review checklist MUST verify them. When formal models exist, test generation in Step 5 uses them for mechanical path coverage.

**Unresolved-unknowns gate**: scan the epic artifacts for markers this ticket would inherit:
```bash
python3 "$CW_HOME/scripts/check_unresolved.py" "$EPIC_DIR" --format json
```
If any finding's `tickets` list includes this ticket (or the finding sits on an entity/operation this ticket implements), do NOT implement on the guessed value. Resolve the unknown first — introspect the real source, read the upstream repo, or ask the user — update the artifact with a citation, then proceed. Building a query layer against `TBD:` schema names produces code that compiles, passes mocked tests, and fails on first contact with reality.

If no epic context exists, proceed without it — the skill works standalone too.

All subsequent steps should work within `$TARGET_REPO`. Use `$CW_HOME` for chief-wiggum scripts/templates. Use `$CW_TMP` for temporary files (not `/tmp/`). Use `$DEFAULT_BRANCH` instead of hardcoding `main`.

### Step 2: Pick and read the ticket

Fetch the issue details:

```bash
gh issue view "$issue_number" --repo "$owner_repo" --json title,body,labels,assignees,milestone,comments
```

Present to the user:
- Title and description
- Acceptance criteria
- Labels and current status
- Any comments with additional context
- Epic context (if loaded): relevant contracts, invariants, state machine transitions

### Step 3: Clarify requirements (only if needed)

Present a concise summary of what needs building (scope in/out). Only ask the user questions if the ticket is genuinely ambiguous — unclear acceptance criteria, conflicting requirements, or missing critical details. If the ticket is well-specified, state your understanding and move on.

If you do need to ask, keep it tight:
1. Summarise your understanding of what needs to be built
2. Ask ONLY questions where the answer isn't inferrable from the ticket or codebase
3. Confirm scope and proceed

### Step 4: Consult AIs on approach

This step has two phases, each in its own worker. This keeps the heavy codebase exploration and synthesis out of the main context window.

#### Phase A: Gather approaches (parallel)

Run **four** tasks in parallel — three AI consultations plus a codebase exploration agent:

1. **Codex + Gemini** — Launch as background bash commands:
   ```bash
   python3 "$CW_HOME/scripts/consult_ai.py" codex $TICKET_TMP/approach-prompt.md -o $TICKET_TMP/approach-codex.md --cwd "$TARGET_REPO" &
   python3 "$CW_HOME/scripts/consult_ai.py" gemini $TICKET_TMP/approach-prompt.md -o $TICKET_TMP/approach-gemini.md --cwd "$TARGET_REPO" &
   wait
   ```

2. **Opus exploration** — Launch an **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) in parallel with the above. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`. This worker should:
   - Explore the target repo codebase thoroughly (read key files, understand patterns)
   - Form its own implementation approach
   - Write its findings to `$TICKET_TMP/approach-opus.md`

3. **Codebase deep-dive** — Launch a background **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) in parallel with all of the above; it signals completion by writing its findings artifact (and a status file), not via a harness notification. *Claude Code adapter:* `subagent_type: "Explore"`, thoroughness "very thorough", `run_in_background: true`. This worker should:
   - Read key files in the areas the ticket will touch (based on ticket description and labels)
   - Document existing patterns, conventions, test infrastructure, and relevant data models
   - Write findings to `$TICKET_TMP/codebase-context.md`
   
   This agent's output is **not blocking** for Phase A completion — it feeds into Phase B reconciliation. If it finishes before Phase B starts, great. If not, Phase B should wait for it (it's fast — typically 2-3 minutes).

Before launching, prepare the approach prompt at `$TICKET_TMP/approach-prompt.md` including:
- Ticket title, description, and acceptance criteria
- **Epic context** (if loaded): relevant contracts, invariants, state machine transitions — these are constraints, not suggestions
- **Orientation context** (give them the lay of the land, NOT the answer):
  - Tech stack and key dependencies
  - Repo structure (top-level tree or directory layout)
  - Conventions and idioms (naming, patterns, test style)
  - How to run tests and linting
  - **Do NOT include**: specific files suspected to be relevant, suggested root causes, or preliminary solution directions. Let each AI discover what's relevant independently — the divergence is the value.
- Question: "Propose an implementation approach including: files to modify/create, step-by-step plan, design decisions and trade-offs, risks/gotchas, testing strategy"

**HARD RULE**: Do NOT proceed to Phase B until ALL THREE approaches (Codex, Gemini, Opus) have completed successfully. If any consultation times out or fails, retry it — do not proceed with partial results. The value of multi-AI consultation comes from diverse perspectives; 2 of 3 is not acceptable.

**Validate consultation output after `wait`**: After all background processes complete, check each output file:
```bash
# For each output file (approach-codex.md, approach-gemini.md):
# 1. File must exist
# 2. File must be > 100 bytes (not empty or just an error message)
# 3. File must NOT start with "Timeout:" or "Error:"
```
If any output is empty, missing, or contains only an error message, **retry that specific consultation** (up to 2 retries). Log which consultation failed and why. Only proceed when all three have substantive output.

#### Phase B: Reconcile into implementation plan

Once all three approaches are ready, ensure the codebase deep-dive explorer worker (Phase A, task 3) has also completed (its findings artifact exists). Then launch a **synthesis worker** (contract: `docs/worker-contracts.md#synthesis-worker`) to reconcile them. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`. This worker should:

1. Read all three approach files (`approach-codex.md`, `approach-gemini.md`, `approach-opus.md`)
2. Read the codebase context file (`$TICKET_TMP/codebase-context.md`) from the deep-dive agent
3. Read the epic context files (contracts, invariants, state machines) if they exist
4. Identify consensus, conflicts, and unique insights
5. Produce a **comprehensive implementation plan** detailed enough that a Sonnet coding agent can execute it mechanically. The plan must include:
   - **Files to create/modify** with specific paths
   - **Ordered implementation steps** — each step should specify exactly what to do, in which file, with enough detail that no further codebase exploration is needed
   - **Code patterns to follow** — reference specific existing files/functions as templates
   - **Key design decisions** — where AIs agreed vs diverged, with a clear recommendation
   - **Contract enforcement** — which REQUIRES/ENSURES blocks from the epic contracts must appear as runtime guards in the code
   - **Test plan** — specific test cases to write and how to run them
   - **Open questions** for the user (if any)
6. Write the full plan to `$TICKET_TMP/implementation-plan.md`
7. Return a concise summary for the main thread

Present a concise summary to the user. If there are open questions that genuinely need user input (e.g., conflicting approaches with no clear winner), ask. Otherwise, proceed directly to Step 5.

### Step 5: Test-first specification

**Write failing tests before writing implementation code.** This transforms the objective from "implement this feature" to "make these tests pass" — a more constrained and verifiable target.

**If formal models exist** (`$HAS_FORMAL_MODELS == true`), generate mechanical test artifacts BEFORE launching the worker. The orchestrator does this directly — it's a deterministic script call, not LLM work:

```bash
# One idempotent call generates every model-derived test artifact (test paths,
# test plan, contract assertions, Hypothesis skeleton, guard templates) and a
# manifest, for whichever models exist in $MODELS_DIR.
python3 "$CW_HOME/scripts/generate_formal_test_artifacts.py" "$MODELS_DIR" --output "$TICKET_TMP/"
```

The manifest (`$TICKET_TMP/formal-artifacts-manifest.json`) lists the generated files and per-model status; a non-zero exit means a present model failed validation — fix the model before building on it.

These mechanically generated artifacts are passed to the worker as inputs — the worker adapts them to the target repo's test framework and conventions, not invents tests from scratch.

Launch an **implementation worker** (contract: `docs/worker-contracts.md#implementation-worker`) — it must operate in its own isolated checkout and never touch the main checkout. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`. Pass it:
- The implementation plan from Step 4
- The epic contracts and traceability matrix (if they exist)
- The target repo's test framework and conventions
- **If formal models exist**: the generated test plan (`$TICKET_TMP/test-plan.md`), test paths (`$TICKET_TMP/test-paths.json`), contract assertions (`$TICKET_TMP/contract-assertions.md`), Hypothesis skeleton (`$TICKET_TMP/test_state_machine.py`), and guard templates (`$TICKET_TMP/guards.py`) — all listed in `$TICKET_TMP/formal-artifacts-manifest.json`
- **If UI spec exists** (`$HAS_UI_SPEC == true`): include the UI spec's page, component, and interaction definitions for the pages this ticket touches. Read `$MODELS_DIR/ui-spec.json` and extract the relevant pages and their component trees. The worker MUST follow the UI spec's structural decisions — if the spec says "sidebar-panel", don't build a separate page; if it says "3-dot-menu", don't use a tab bar. Interaction contracts (trigger → action → target) are binding, not suggestions. If the spec has a `design` section, also pass its tokens, component-library binding, relevant assets, and voice guidelines — bind tokens as CSS variables/theme values, never hardcode the component library's defaults. The design-fidelity gate (Step 9) will review rendered screenshots against this contract.
**HARD RULES for worker**:
- Do NOT create pull requests, do NOT merge branches, do NOT run `gh pr create` or `gh pr merge`. Your job is to write code and commit to the feature branch. The orchestrator owns PR creation (Step 11).
- You work in an **isolated checkout** (required isolation behavior). At the start, assert isolation with the tested check (it aborts non-zero if you are in the main checkout): `python3 "$CW_HOME/scripts/git_safety.py" assert-worktree --main "$TARGET_REPO"`. Work ONLY in the checkout root it prints. Do NOT `cd` to `$TARGET_REPO`. Never run destructive git operations (`reset --hard`, `clean -f`) on the main checkout.

The worker should:

1. Create a feature branch named after the ticket (e.g., `feat/42-add-dark-mode`)
2. Write test files FIRST, covering:
   - **Mechanically derived tests** (if formal models exist): Adapt the test plan and paths from the formal model to the target repo's test framework. Each path in `test-paths.json` becomes a test case. Each invalid transition becomes a negative test case. Each contract assertion becomes a precondition/postcondition check. Tag these tests with a `# DERIVED: model` comment for traceability.
   - **Acceptance criteria tests**: One or more tests per AC from the ticket. If a traceability matrix exists, follow it.
   - **Contract tests**: For each REQUIRES/ENSURES in the epic contracts that this ticket touches, write a test that verifies the precondition is checked and the postcondition holds.
   - **State machine tests** (if applicable): Test that valid transitions succeed and invalid transitions are rejected. If the Hypothesis skeleton was provided, adapt it to use the actual implementation's API rather than just tracking state in a variable.
   - **Property-based tests** (where appropriate): For pure functions and data transformations, write at least one property test (roundtrip, idempotency, no-crash-on-valid-input). Use the project's property testing library if one exists (Hypothesis, fast-check, gopter), otherwise skip.
   - **Error path tests**: For each API endpoint or operation, test at least one error case (invalid input, missing auth, service unavailable).
3. Run the tests — **all should fail** (red phase). If any pass before implementation, the test is not testing new behaviour. Investigate and fix.
4. Commit the test files with message: `test: add failing tests for #[number] — [title]`
5. Report back: which tests were written (noting which are model-derived vs LLM-written), which frameworks used, any gaps in the traceability matrix

**Important**: The worker should report the worktree path and branch name. The implementation worker in Step 6 will work in the SAME worktree.

### Step 6: Implement

Launch an **implementation worker** (contract: `docs/worker-contracts.md#implementation-worker`) in the **same isolated checkout** from Step 5. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`. Pass it the **full implementation plan** from `$TICKET_TMP/implementation-plan.md` plus any user feedback, plus the fact that failing tests already exist on the branch.

**HARD RULES for worker**:
- Do NOT create pull requests, do NOT merge branches, do NOT run `gh pr create` or `gh pr merge`. Your job is to write code, run tests, and commit. The orchestrator owns PR creation (Step 11).
- You are working in a **git worktree** (the same one from Step 5). Confirm isolation with `python3 "$CW_HOME/scripts/git_safety.py" assert-worktree --main "$TARGET_REPO"`. Do NOT `cd` to `$TARGET_REPO`. Never run destructive git operations (`reset --hard`, `clean -f`) on the main checkout.

The worker should:
1. Implement the approved approach — the primary objective is **making the failing tests from Step 5 turn green**
2. Enforce epic contracts as runtime guards:
   - Every REQUIRES block → input validation / guard clause at function entry
   - Every ENSURES block → verify postcondition before returning (or via integration test)
   - Every state machine transition → validate current state before allowing transition
3. Run the project's **full** test suite — not just the new tests, ALL existing tests too:
   - **Preferred**: If the target repo has a `/test` skill or `make ci` target, use it — these replicate CI exactly
   - Otherwise look for `Makefile`, `package.json`, or common test commands
   - Go projects: `go test ./...`
   - Node projects: `npm test`
   - Python projects: `pytest`
4. Run linting if the project has a linter configured (golangci-lint, eslint, etc.)
5. Run Playwright/E2E tests if they exist in the target repo
6. Fix **all** failures — including pre-existing ones. Every PR must leave CI green. Do not dismiss failures as "pre-existing" or "not ours".
7. If stuck after 3 attempts at the same error, report back to the user
8. **Report honestly.** If you could not run a test or validation step, say so clearly with the reason. Do NOT silently skip steps or mark them as passed when they were not executed. The orchestrator will verify independently — discrepancies will be caught.

### Step 7: Multi-AI code review with structured checklist

**THIS STEP IS NEVER OPTIONAL.** Every implementation gets a multi-AI code review, regardless of change size. A one-line typo fix, a 10-line config change, a 500-line feature — all get the same review process. You do not get to self-certify your own code. No exceptions, no shortcuts.

**IMPORTANT**: Run this entire step inside a **review worker** (contract: `docs/worker-contracts.md#review-worker`). The orchestrator should only receive the synthesized review summary with actionable items. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`.

The worker should:

1. Get the diff from the implementation:
   ```bash
   git diff "$DEFAULT_BRANCH"...HEAD > $TICKET_TMP/impl-diff.txt
   ```

2. Run the review pipeline in one call. It captures the `base...HEAD` diff, assembles the review prompt from `templates/review-prompt.md` + `review-checklist.md` (plus any epic artifacts you pass), runs the `reviewer` quorum (parallel, retries, output validation), and writes the synthesis prompt + a manifest. Pass a `ticket.json` with the title/body/acceptance criteria, and optionally epic artifacts:
   ```bash
   python3 "$CW_HOME/scripts/run_review.py" \
     --ticket-context "$TICKET_TMP/ticket.json" \
     --worktree "$(git rev-parse --show-toplevel)" --base "$DEFAULT_BRANCH" \
     --output-dir "$TICKET_TMP/reviews" \
     --epic-artifact "Contracts=$EPIC_DIR/contracts.md" \
     --epic-artifact "Invariants=$EPIC_DIR/invariants.md"
   ```
   Outputs land in `$TICKET_TMP/reviews/`: `impl-diff.txt`, `review-prompt.md`, `reviewer-<provider>.md`, `synthesis-prompt.md`, and `review-manifest.json`. It refuses to run outside a git repo or when `--base` can't be resolved; a non-zero exit means a required provider never produced valid output (note the gap, proceed with available reviews).

3. Perform its own review of the diff.

4. Synthesize using:
   ```bash
   python3 "$CW_HOME/scripts/synthesize_reviews.py" $TICKET_TMP/reviews/reviewer-codex.md $TICKET_TMP/reviews/reviewer-gemini.md
   ```

5. Return a concise summary categorising each piece of feedback:
   - **High-confidence fixes**: Concrete bugs/regressions with clear failure scenarios. Apply automatically.
   - **Medium-confidence findings**: Plausible issues that need a quick local verification before applying.
   - **Low-confidence or architectural feedback**: Speculative concerns or design trade-offs. Flag for user decision.
   - Ignore style-only comments unless they point to a real defect.

   Also return the **checklist scorecard**: pass/fail for each item in the structured checklist, with one-line justification for any failures.

### Step 8: Apply review fixes and verify

Apply clear-cut fixes from the review. Flag ambiguous items for the user. Then **the orchestrator independently verifies the final state** — this is not delegatable.

1. **Apply clear-cut fixes** directly (don't re-launch a worker for trivial changes)
2. **Flag ambiguous feedback** for user decision — only block on items that genuinely need input
3. **Run static analysis** on the changed files:
   - Go: `golangci-lint run ./...`
   - TypeScript/JavaScript: `npx eslint --no-warn-ignored` or `npx biome check`
   - Python: `ruff check` or `flake8`
   - Feed violations back to the code — fix them before proceeding. Gate on zero high-severity findings.
4. **Run the full test suite** from the repo root. The verification runner detects the project type and emits structured evidence (command, exit code, duration, log tail) for the PR body — it prefers a `make` target named for the profile when present:
   ```bash
   python3 "$CW_HOME/scripts/run_verification.py" --repo "$(git rev-parse --show-toplevel)" --profile test,lint,build --markdown
   ```
   It exits non-zero if any step fails. ALL tests must pass. Zero tolerance.
5. **Start services** and verify they work:
   - If `docker-compose.yml` exists: `docker compose up -d` and wait for healthy
   - If Docker isn't running, start it (`open -a Docker` on macOS, `sudo systemctl start docker` on Linux) and wait
   - Hit key endpoints (health checks, any endpoints the ticket specifies)
   - Verify responses match expectations
6. **Walk the acceptance criteria** from the ticket:
   - For each checkbox in the AC, verify it's actually met — not just "code exists" but "it works"
   - If the ticket says "health endpoint returns 200", curl it and confirm
   - If the ticket says "tests pass", run them yourself and confirm
7. **Verify contract enforcement** (if epic context exists):
   - Check that REQUIRES blocks appear as guard clauses in the implementation
   - Check that invalid state transitions are rejected (try one via curl or test)
   - Check that ENSURES postconditions hold after operations complete
8. **Formal model conformance check** (if `$HAS_FORMAL_MODELS == true`):
   This is the mechanical verification step that doesn't rely on worker self-reports.
   - **State machine coverage**: For each test path in `test-paths.json`, verify a corresponding test exists and passes. Count: paths covered / paths total.
   - **Invalid transition coverage**: For each invalid transition in the model, verify a negative test exists that asserts rejection. Count: invalid transitions tested / invalid transitions total.
   - **Guard clause presence**: For each REQUIRES precondition in `contracts.json`, grep the implementation for a corresponding guard clause or validation check. Flag missing guards.
   - **Invariant coverage**: For each invariant in the model, verify at least one test checks it (either directly or via the Hypothesis `RuleBasedStateMachine`).
   - Produce a conformance summary:
     ```
     Model conformance:
       Test paths:           X/Y covered
       Invalid transitions:  X/Y tested
       Guard clauses:        X/Y present
       Invariants:           X/Y checked
     ```
   - If any category is below 80% coverage, flag it as a gap in the PR body. Do NOT block shipping — this is a signal, not a gate, in Phase 1. (Phase 2 may tighten this to a hard gate.)
8b. **Transition-map verification** (if `$HAS_TRANSITION_MAP == true`):
   Run the verification script scoped to this ticket:
   ```bash
   python3 "$CW_HOME/scripts/verify_transitions.py" "$(git rev-parse --show-toplevel)" "$MODELS_DIR/state-machines.json" --ticket "#$issue_number" --format text
   ```
   This reports:
   - Which transitions this ticket was supposed to introduce (from `derived_from` provenance)
   - Which are now present in code (COVERED)
   - Which are still missing (MISSING) — implementation gap, fix before shipping
   - Any undocumented transitions in the diff (UNDOCUMENTED) — either update the model or remove the code

   After verification, update the transition-map:
   ```bash
   python3 "$CW_HOME/scripts/verify_transitions.py" "$(git rev-parse --show-toplevel)" "$MODELS_DIR/state-machines.json" --output "$MODELS_DIR/transition-map.json" --format json
   git add "$MODELS_DIR/transition-map.json"
   ```
   Include transition coverage in the PR body under "Model conformance".
9. **Quality check** — Read the key files produced:
   - Is the code idiomatic for the language?
   - Are there any obvious issues (missing error handling, security gaps, dead code)?
   - Does it follow existing patterns in the codebase?
   - Would you be proud to ship this?
10. **Clean up** — Stop any services you started (`docker compose down`)

If ANY verification fails: fix it directly, or re-launch the coding worker with specific instructions for larger issues. Do NOT proceed to ship until verification passes.

### Step 9: UX sanity + design-fidelity gate

Run the tested gate to do all the mechanical setup — frontend-impact detection (diff paths + labels), ui-spec design-binding check, reference-screenshot discovery, and screenshot-capture planning — and emit a manifest:

```bash
git diff "$DEFAULT_BRANCH"...HEAD --name-only > "$TICKET_TMP/changed.txt"
python3 "$CW_HOME/scripts/ux_gate.py" \
  --changed-files "$TICKET_TMP/changed.txt" \
  $(gh issue view "$issue_number" --repo "$owner_repo" --json labels -q '.labels[].name' | sed 's/^/--label /') \
  --ui-spec "$MODELS_DIR/ui-spec.json" --design-dir "$TARGET_REPO/docs/design" \
  --screenshot-dir "$TICKET_TMP/ux-screenshots" --markdown > "$TICKET_TMP/ux-manifest.md"
```

(Add `--have-browser-use` / `--have-playwright` based on the target repo's tooling.)

**Skip this step if** `--skip-browser-use` was passed, or the manifest's `should_run_gate` is false (no frontend impact). If the manifest is **blocked** (frontend ticket with a design contract but no screenshot tooling), resolve the tooling — do not silently skip the design-fidelity gate. Otherwise run the judgment-heavy review below against the discovered reference screenshots.

**Goal**: Verify that the implemented UI aligns with the *spirit* of the requirements — information architecture, menu coherence, field exposure, contextual clarity — AND with the **visual design contract** (`ui-spec.json` → `design` section): tokens applied, brand assets present, reference screenshots matched. Functional tests can pass while screens feel wrong or ship off-brand; this is the only step in the loop that actually *looks* at the result. "Build + tests green" is NOT sufficient to call a frontend ticket done.

(Real example: dogeared-coach shipped 131 passing tests and 40 green E2E specs with a client-facing playlist numbered "0., 1., ..." and a completely unthemed stock component library. One screenshot caught both.)

#### Phase 1: Capture screenshots

Determine what tooling is available, in priority order:

1. **browser-use** (if `tests/browser-use/run.py` or similar exists):
   ```bash
   cd "$TARGET_REPO" && python3 tests/browser-use/run.py \
     --scenario "ux-sanity-#$issue_number" \
     --screenshot-dir "$TICKET_TMP/ux-screenshots/"
   ```
   If the repo's browser-use setup does not support ad-hoc scenario strings, write a minimal scenario file to `$TICKET_TMP/ux-scenario.md` describing each step from the AC (e.g., "navigate to X", "click Y", "fill form Z", "submit"), then pass it as the scenario source.

2. **Playwright** (if Playwright is installed and `playwright.config.*` exists):
   ```bash
   cd "$TARGET_REPO" && npx playwright screenshot \
     --browser chromium \
     "$START_URL" \
     "$TICKET_TMP/ux-screenshots/00-initial.png"
   ```
   For flows with multiple states (form open → filled → submitted → success), write a **minimal throwaway Playwright script** to `$TICKET_TMP/ux-capture.spec.ts` that navigates each state transition and calls `page.screenshot()` at each step. Run it once, then delete the file — it is not a permanent test artifact.

**Screenshots must cover each state transition** identified in the AC or state machine:
- Initial page load / entry point
- Each form or dialog in its empty state
- Each form in its filled/valid state
- Post-submit / success state
- Any error states the ticket introduces

Name screenshots sequentially: `00-entry.png`, `01-form-empty.png`, `02-form-filled.png`, `03-success.png`, etc.

If services need to be running, start them as in Step 8 before capturing. After capture, leave them running for Step 10 (browser-use validation) — do not stop yet.

If screenshot capture fails entirely (services won't start, no browser tooling at all), note it as a gap and move on — do not block.

#### Phase 1b: Mechanical token check (if a design contract exists)

If `$MODELS_DIR/ui-spec.json` has a `design` section, run a cheap mechanical check before the AI review: for each color token value in `design.tokens.colors`, grep the frontend's theme/CSS files for it. If the primary brand color appears nowhere in the codebase, the frontend ignored the design contract — that's a hard finding, no screenshot review needed to call it.

```bash
python3 -c "
import json, sys
spec = json.load(open('$MODELS_DIR/ui-spec.json'))
for name, value in spec.get('design', {}).get('tokens', {}).get('colors', {}).items():
    print(f'{name}\t{value}')
" | while IFS=$'\t' read -r name value; do
  grep -ri --include='*.css' --include='*.scss' --include='*.ts' --include='*.tsx' -F "$value" "$WORKTREE/ui/src" >/dev/null 2>&1 \
    && echo "token $name: BOUND" || echo "token $name: MISSING ($value not found in styles)"
done
```

#### Phase 2: Opus UX + design-fidelity review

Launch a **review worker** (contract: `docs/worker-contracts.md#review-worker`) and give it: *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`.

1. **The screenshots** — pass paths to all captured images in `$TICKET_TMP/ux-screenshots/`
2. **Requirement prose** — the full ticket body (not just AC bullet points): title, description, user story, and any comments
3. **Domain model artifacts** (if epic context loaded): `contracts.md`, `state-machines.md`, `invariants.md` from `$EPIC_DIR/`. These represent the "spirit" of the domain — what states are meaningful, what data belongs where, what a user is actually trying to accomplish
4. **The visual design contract** (if `ui-spec.json` has a `design` section): the design tokens, component-library binding, brand assets, voice guidelines, and — critically — any `reference-screenshot` assets whose `applies_to` covers the pages this ticket touches. Pass the reference image paths so the reviewer can compare side by side. If the target repo has `docs/design/` (produced by `/design`), the approved-mock screenshots in `docs/design/reference/` are the comparison baseline — pass them even if the epic's ui-spec assets don't list one for these pages, and treat `docs/design/mockups/*.html` as the living reference implementation when layout questions arise. Include the Phase 1b token-check output.
5. **The AC bullets** — for reference, but instruct the reviewer: *"These bullets define the floor, not the ceiling. Your job is to evaluate whether the screens make sense to a user trying to accomplish the goal described in the prose, and whether they honor the visual design contract."*

The worker should evaluate:

- **Information architecture**: Is the right information on the right screen? Is anything missing that a user would expect to see? Is anything shown that shouldn't be visible at this stage?
- **Menu and navigation coherence**: If menus or navigation changed, do the new entries make sense in context? Are they in the right place, with the right label? Do they appear/disappear at the right times?
- **Field exposure**: Are there fields visible that the user shouldn't see at this point in their flow (e.g., internal IDs, status codes, fields for a later step)? Are required fields clearly indicated?
- **State legibility**: Can a user tell what state they're in? Is it clear what happened after they submitted a form or completed an action?
- **Contextual fit**: Does the screen match what the ticket is trying to accomplish? If the domain model says "an order in PENDING state should only show a Confirm button, not a Cancel button", does the screen reflect that?
- **Missing context**: Is there information the user would need to make a decision that isn't shown?
- **Design fidelity** (when a design contract exists):
  - Are the design tokens actually applied — brand colors, typography, spacing — or is this the component library's default theme?
  - Do the screens match the reference screenshots for these pages (layout density, treatment, hierarchy)?
  - Are the brand assets present where `applies_to` says they should be (logo in nav, illustration in empty states)?
  - Does visible copy match the voice guidelines (empty states with personality, not "No data")?
  - **Surface-level correctness a human would catch in 2 seconds**: 0-indexed lists shown to users, raw enum values in labels, placeholder copy, truncated text, misaligned elements, debug output visible in the UI.

The worker returns findings in the same confidence categories as the code review:

- **High-confidence**: Clear UX problems — missing confirmation message after submit, a Cancel button that should not appear in PENDING state, a form field exposing an internal DB ID — and clear design-contract violations: design tokens not applied (Phase 1b MISSING), a page that plainly doesn't match its reference screenshot, a missing brand asset, user-visible 0-indexing or debug output. These have an obvious fix and **fail the ticket until fixed**.
- **Medium-confidence**: Likely issues that need a quick look — a menu label that's technically correct but potentially confusing, an empty state with no guidance copy. These should be applied but are worth a quick sanity check.
- **Low-confidence**: Subjective observations or minor polish — layout density, label wording choices, optional affordances the ticket doesn't require. Flag for awareness, do not block.

The worker writes findings to `$TICKET_TMP/ux-review.md` and returns a concise summary.

#### Phase 3: Apply findings

Apply findings using the same pattern as Step 8:
- **High-confidence findings**: Fix directly. These are clear defects — wrong button state, missing feedback, field that shouldn't be visible.
- **Medium-confidence findings**: Investigate quickly (check the domain model or requirement prose), then apply if confirmed.
- **Low-confidence findings**: Add to the PR body under a "UX observations" section for reviewer awareness. Do not block on these.

After applying fixes, re-run the relevant Playwright specs (if they exist) to confirm nothing regressed.

**Fold findings into the PR body**: Add a `## UX & design fidelity` section to the PR body (Step 11) listing:
- What flow was walked
- High/medium findings found and fixed
- Low-confidence observations noted
- Token-check results (Phase 1b) when a design contract exists
- **Attach the screenshots**: commit them under the PR or upload via `gh` so the human reviewer sees what shipped, not just that tests passed. At minimum, embed the key before/after screenshot paths in the PR body.

If no screenshots could be captured, that is a **blocker for frontend tickets with a design contract** — fix the tooling (start the services, install Playwright) rather than skipping. Only note "UX sanity: no frontend tooling available — skipped" for repos with no design contract and no browser tooling at all.

### Step 10: Browser-use validation

**Do not skip this step** unless `--skip-browser-use` was explicitly passed.

Check if the target repo has a browser-use or E2E setup:

```bash
ls "$TARGET_REPO/tests/browser-use/run.py" 2>/dev/null
ls "$TARGET_REPO/e2e/" 2>/dev/null
ls "$TARGET_REPO/tests/e2e/" 2>/dev/null
ls "$TARGET_REPO/ui/tests/" 2>/dev/null
```

If **Playwright tests** exist (e.g. `ui/tests/*.spec.ts`, `e2e/*.spec.ts`):
1. Identify which test files are relevant to this ticket (match by feature area)
2. Run them:
   ```bash
   cd "$TARGET_REPO/ui" && npx playwright test <relevant-spec-files>
   ```
3. If all relevant specs pass, move on. If failures occur, fix them.

If **browser-use** exists (e.g. `tests/browser-use/run.py`):
1. Identify which scenarios are relevant to this ticket (match by tags or description)
2. Run the relevant scenarios:
   ```bash
   cd "$TARGET_REPO" && python3 tests/browser-use/run.py --scenario <ids>
   ```
3. Capture results and screenshots
4. Report pass/fail with details

If no browser-use or E2E setup exists at all, note it as a gap in the final summary and move on.

### Step 11: Ship PR

**Do not create a PR until Steps 7-9 are complete.** The PR is the final artifact, not an intermediate checkpoint.

1. Push the branch
2. **Generate mermaid diagrams** — this is NOT optional. Every PR must include at least one mermaid diagram. Generate them based on the diff:

   **Color palette** (mandatory):
   ```
   #003f5c (deep navy)    — existing infrastructure, databases
   #2f4b7c (slate blue)   — existing dependencies
   #665191 (muted purple) — modified components
   #a05195 (plum)         — modified internals
   #d45087 (rose)         — new components
   #f95d6a (coral)        — new internals
   #ff7c43 (tangerine)    — user-facing / entry points
   #ffa600 (amber)        — highlights
   ```

   Write the diagram body (without the `%%{init}%%` line — `draft_pr.py` injects the themed init block in step 3) to `$TICKET_TMP/architecture.mmd`, using these classDef styles:
   ```mermaid
   graph TD
       classDef existing fill:#003f5c,stroke:#2f4b7c,color:#fff
       classDef modified fill:#665191,stroke:#a05195,color:#fff
       classDef new fill:#d45087,stroke:#f95d6a,color:#fff
       classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
   ```

   Include at minimum a **Component Relationship Diagram** showing what was added/modified. Add a **Sequence Diagram** if data flow changed (pass `--mermaid-sequence` to `draft_pr.py`). Keep diagrams focused (max 15 nodes).

3. Draft the PR body with the tested helper. It folds in the verification evidence and (when present) the review/UX/model-conformance manifests, themes the Mermaid diagram with the shared palette automatically, validates the required sections, and links the issue:

```bash
python3 "$CW_HOME/scripts/draft_pr.py" \
  --issue "$issue_number" --title "$pr_title" --summary "$summary" \
  --change "Change 1" --change "Change 2" \
  --mermaid-file "$TICKET_TMP/architecture.mmd" \
  --verification "$TICKET_TMP/verification.json" \
  --review "$TICKET_TMP/reviews/review-manifest.json" \
  --model-conformance "$TICKET_TMP/model-conformance.md" \
  --base "$DEFAULT_BRANCH" --out "$TICKET_TMP/pr-body.md"
```

(Omit `--model-conformance` / `--review` when they don't apply — those sections are then omitted.) Then create the PR from the body file:

```bash
gh pr create --repo "$owner_repo" --title "$pr_title" --body-file "$TICKET_TMP/pr-body.md" --base "$DEFAULT_BRANCH"
```

4. The helper links the original issue via `Closes #N` from `--issue`.

### Step 12: Verify CI green

**Do not declare the PR done until all checks pass.** This is a hard gate — no exceptions.

1. **If CI is available**, poll CI status:
   ```bash
   gh pr checks <pr_number> --repo "$owner_repo" --watch
   ```
2. **If CI is unavailable**, run checks locally (detect project type and run appropriate commands).
3. If any check fails:
   - Fix the failures (including pre-existing ones — every PR must leave CI green)
   - Push fixes and re-check
   - Repeat until all checks pass
4. Only after all checks pass, present the final summary:
   - **Summary**: What was implemented (files changed, approach taken)
   - **Test results**: CI status (all green), TDD stats (N tests written first)
   - **Review feedback**: What was addressed, what was deferred
   - **Checklist scorecard**: Summary of structured review results
   - **Browser-use results**: Screenshots and pass/fail (if applicable)
   - **Pre-existing fixes**: Any broken tests/lint we fixed that weren't ours
   - **Traceability update**: Which AC are now covered (if epic context exists)
   - **Lingering questions**: Anything unresolved
5. Show the PR URL

### Step 13: Update traceability matrix

If epic context exists, flip this ticket's rows from `pending` to `covered` with the tested updater (it parses, updates by ticket/AC, and re-renders the table in place — no brittle manual markdown edits):

```bash
python3 "$CW_HOME/scripts/traceability.py" update "$EPIC_DIR/traceability.md" \
  --ticket "$issue_number" --status covered
# Narrow to specific rows with --ac "<criterion text>" when a ticket only
# partially addresses its ACs.
```

Commit the updated `traceability.md` (or comment on the epic milestone if other tickets are in flight).

Close the loop:
- Ask if the issue should be updated with a comment linking to the PR

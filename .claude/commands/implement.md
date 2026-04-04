# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → **test-first specification** → implement → **static analysis** → structured review → apply fixes → **verify** → validate → ship.

## Ownership

**You own the solution, not just the code.** Before shipping ANY implementation, ask yourself:
- Am I proud of this work?
- Is it clean and elegant?
- Have I verified it actually works end-to-end?

If the answer to any of these is no — fix it. Don't ship "good enough."

**The validation loop is not negotiable.** Sub-agents will take shortcuts. The orchestrator is the quality gate. Never trust a sub-agent's self-reported "tests pass" — independently verify.

**Never punt to the user.** If Docker isn't running, start it. If a dependency is missing, install it. If you can't run the tests, that's YOUR problem to solve. "Want to skip this step?" is never the right question.

**Every step is mandatory.** You do NOT get to decide that a change is "too small" to warrant code review, or that consultations are "good enough" with only 2 of 3 responses. The process exists for a reason — follow it completely every time, no exceptions. Specifically:
- **Never skip the multi-AI code review** (Step 7), regardless of change size. A one-line fix gets the same review process as a 500-line feature. No developer gets to self-certify their own code.
- **Never skip AI consultations** (Step 4). Wait for ALL consultations (Codex, Gemini, Opus) to complete. If one times out, retry it. Never proceed to reconciliation with partial results.
- **Never skip browser-use/E2E validation** (Step 9) unless `--skip-browser-use` was explicitly passed by the user.
- **Never create a PR before review is complete.** The PR is the final artifact (Step 10), not an intermediate checkpoint.

## Autonomy

**Run the full pipeline autonomously.** Do NOT pause between steps to ask "ready to proceed?" or "want to skip this?". Move through every step without asking for permission unless you genuinely need user input (e.g., ambiguous requirements in Step 2, a design decision where approaches conflict and there's no clear winner, or a blocking error you can't resolve).

Checkpoints where you MUST get user input:
- **Step 3** (Clarify requirements): Only if requirements are genuinely unclear or ambiguous
- **Step 4 Phase B** (Approach reconciliation): Only if approaches fundamentally conflict with no clear winner — present the trade-off and ask
- **Step 10** (Final check): Present the summary, then proceed to ship unless the user intervenes

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

Resolve the chief-wiggum install directory and the target repo path. **Never hardcode paths.**

```bash
CW_HOME=$(python3 "$(dirname "$0")/../../scripts/repo.py" home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
DEFAULT_BRANCH=$(gh repo view "$owner_repo" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
```

**Important**: `$CW_TMP` uses a unique session ID so concurrent `/implement` runs don't clobber each other's temp files.

Create a **ticket-scoped subdirectory** for all per-ticket artifacts. When implementing multiple tickets in one session, this prevents file collisions (e.g., `approach-codex.md` for ticket #417 being overwritten by ticket #418):

```bash
TICKET_TMP="$CW_TMP/$issue_number"
mkdir -p "$TICKET_TMP"
```

All per-ticket files (`approach-prompt.md`, `approach-codex.md`, `approach-gemini.md`, `approach-opus.md`, `implementation-plan.md`, `review-prompt.md`, `review-codex.md`, `review-gemini.md`, `impl-diff.txt`) go in `$TICKET_TMP`, not `$CW_TMP`. Shared session files (e.g., epic context) remain in `$CW_TMP`.

**Load epic context** (if this ticket belongs to an epic):

```bash
# Find the ticket's milestone
MILESTONE=$(gh issue view "$issue_number" --repo "$owner_repo" --json milestone -q '.milestone.title // empty')
```

If a milestone exists and `docs/epics/[epic-slug]/` exists in the target repo, load:
- `contracts.md` — REQUIRES/ENSURES for APIs and entities
- `state-machines.md` — valid state transitions
- `invariants.md` — cross-cutting rules
- `traceability.md` — which acceptance criteria need which tests

These artifacts are **hard constraints** on the implementation. The coding sub-agent MUST satisfy them. The review checklist MUST verify them.

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

This step has two phases, each in its own sub-agent. This keeps the heavy codebase exploration and synthesis out of the main context window.

#### Phase A: Gather approaches (parallel)

Run **four** tasks in parallel — three AI consultations plus a codebase exploration agent:

1. **Codex + Gemini** — Launch as background bash commands:
   ```bash
   python3 "$CW_HOME/scripts/consult_ai.py" codex $TICKET_TMP/approach-prompt.md -o $TICKET_TMP/approach-codex.md --cwd "$TARGET_REPO" &
   python3 "$CW_HOME/scripts/consult_ai.py" gemini $TICKET_TMP/approach-prompt.md -o $TICKET_TMP/approach-gemini.md --cwd "$TARGET_REPO" &
   wait
   ```

2. **Opus exploration** — Launch an **Opus sub-agent** (`subagent_type: "general-purpose"`, `model: "opus"`) in parallel with the above. This sub-agent should:
   - Explore the target repo codebase thoroughly (read key files, understand patterns)
   - Form its own implementation approach
   - Write its findings to `$TICKET_TMP/approach-opus.md`

3. **Codebase deep-dive** — Launch a **Sonnet Explore sub-agent** (`subagent_type: "Explore"`, thoroughness: "very thorough") in parallel with all of the above, running in the background (`run_in_background: true`). This sub-agent should:
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

Once all three approaches are ready, ensure the codebase deep-dive agent (Phase A, task 3) has also completed. Then launch a **second Opus sub-agent** (`subagent_type: "general-purpose"`, `model: "opus"`) to reconcile them. This sub-agent should:

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

Launch a **Sonnet sub-agent** in a worktree (`subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`). Pass it:
- The implementation plan from Step 4
- The epic contracts and traceability matrix (if they exist)
- The target repo's test framework and conventions
**HARD RULES for sub-agent**:
- Do NOT create pull requests, do NOT merge branches, do NOT run `gh pr create` or `gh pr merge`. Your job is to write code and commit to the feature branch. The orchestrator owns PR creation (Step 10).
- You are working in a **git worktree** (created by the `isolation: "worktree"` parameter). At the start, run `git rev-parse --show-toplevel` to discover your working directory. Work ONLY in this directory. Do NOT `cd` to `$TARGET_REPO` — that is the main checkout, not your worktree. If `git rev-parse --show-toplevel` returns the same path as the main checkout, STOP and report the error. Never run destructive git operations (`reset --hard`, `clean -f`) on the main checkout.

The sub-agent should:

1. Create a feature branch named after the ticket (e.g., `feat/42-add-dark-mode`)
2. Write test files FIRST, covering:
   - **Acceptance criteria tests**: One or more tests per AC from the ticket. If a traceability matrix exists, follow it.
   - **Contract tests**: For each REQUIRES/ENSURES in the epic contracts that this ticket touches, write a test that verifies the precondition is checked and the postcondition holds.
   - **State machine tests** (if applicable): Test that valid transitions succeed and invalid transitions are rejected.
   - **Property-based tests** (where appropriate): For pure functions and data transformations, write at least one property test (roundtrip, idempotency, no-crash-on-valid-input). Use the project's property testing library if one exists (Hypothesis, fast-check, gopter), otherwise skip.
   - **Error path tests**: For each API endpoint or operation, test at least one error case (invalid input, missing auth, service unavailable).
3. Run the tests — **all should fail** (red phase). If any pass before implementation, the test is not testing new behaviour. Investigate and fix.
4. Commit the test files with message: `test: add failing tests for #[number] — [title]`
5. Report back: which tests were written, which frameworks used, any gaps in the traceability matrix

**Important**: The sub-agent should report the worktree path and branch name. The implementation sub-agent in Step 6 will work in the SAME worktree.

### Step 6: Implement

Launch a **Sonnet sub-agent** in the **same worktree** from Step 5 (`subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`). Pass it the **full implementation plan** from `$TICKET_TMP/implementation-plan.md` plus any user feedback, plus the fact that failing tests already exist on the branch.

**HARD RULES for sub-agent**:
- Do NOT create pull requests, do NOT merge branches, do NOT run `gh pr create` or `gh pr merge`. Your job is to write code, run tests, and commit. The orchestrator owns PR creation (Step 10).
- You are working in a **git worktree** (the same one from Step 5). Run `git rev-parse --show-toplevel` to confirm your working directory. Do NOT `cd` to `$TARGET_REPO`. Never run destructive git operations (`reset --hard`, `clean -f`) on the main checkout.

The sub-agent should:
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

**IMPORTANT**: Run this entire step inside a **Sonnet sub-agent** (`subagent_type: "general-purpose"`, `model: "sonnet"`). The main thread should only receive the synthesized review summary with actionable items.

The sub-agent should:

1. Get the diff from the implementation:
   ```bash
   git diff "$DEFAULT_BRANCH"...HEAD > $TICKET_TMP/impl-diff.txt
   ```

2. Prepare a review prompt using `$CW_HOME/templates/review-prompt.md` as a base. Read the template, replace the `{{TICKET_TITLE}}`, `{{TICKET_DESCRIPTION}}`, `{{ACCEPTANCE_CRITERIA}}`, and `{{DIFF}}` placeholders with actual values. **Also include the structured checklist** from `$CW_HOME/templates/review-checklist.md` and the epic contracts/invariants if they exist. Write to `$TICKET_TMP/review-prompt.md`.

3. Run external AI reviews in parallel:
   ```bash
   python3 "$CW_HOME/scripts/consult_ai.py" codex $TICKET_TMP/review-prompt.md -o $TICKET_TMP/review-codex.md --cwd "$(git rev-parse --show-toplevel)" &
   python3 "$CW_HOME/scripts/consult_ai.py" gemini $TICKET_TMP/review-prompt.md -o $TICKET_TMP/review-gemini.md --cwd "$(git rev-parse --show-toplevel)" &
   wait
   ```

   **Validate review output**: After `wait`, check that both `review-codex.md` and `review-gemini.md` exist and contain substantive output (>100 bytes, not starting with "Timeout:" or "Error:"). Retry any failed consultation up to 2 times. If a consultation still fails after retries, proceed with available reviews but note the gap in the synthesis.

4. Perform its own review of the diff.

5. Synthesize using:
   ```bash
   python3 "$CW_HOME/scripts/synthesize_reviews.py" $TICKET_TMP/review-codex.md $TICKET_TMP/review-gemini.md
   ```

6. Return a concise summary categorising each piece of feedback:
   - **High-confidence fixes**: Concrete bugs/regressions with clear failure scenarios. Apply automatically.
   - **Medium-confidence findings**: Plausible issues that need a quick local verification before applying.
   - **Low-confidence or architectural feedback**: Speculative concerns or design trade-offs. Flag for user decision.
   - Ignore style-only comments unless they point to a real defect.

   Also return the **checklist scorecard**: pass/fail for each item in the structured checklist, with one-line justification for any failures.

### Step 8: Apply review fixes and verify

Apply clear-cut fixes from the review. Flag ambiguous items for the user. Then **the orchestrator independently verifies the final state** — this is not delegatable.

1. **Apply clear-cut fixes** directly (don't re-launch a sub-agent for trivial changes)
2. **Flag ambiguous feedback** for user decision — only block on items that genuinely need input
3. **Run static analysis** on the changed files:
   - Go: `golangci-lint run ./...`
   - TypeScript/JavaScript: `npx eslint --no-warn-ignored` or `npx biome check`
   - Python: `ruff check` or `flake8`
   - Feed violations back to the code — fix them before proceeding. Gate on zero high-severity findings.
4. **Run the full test suite** from the repo root:
   - If a `/test` skill or `make ci` target exists, use it
   - Otherwise: `pytest` (Python), `npm test` (Node), `go test ./...` (Go)
   - ALL tests must pass. Zero tolerance.
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
8. **Quality check** — Read the key files produced:
   - Is the code idiomatic for the language?
   - Are there any obvious issues (missing error handling, security gaps, dead code)?
   - Does it follow existing patterns in the codebase?
   - Would you be proud to ship this?
9. **Clean up** — Stop any services you started (`docker compose down`)

If ANY verification fails: fix it directly, or re-launch the coding sub-agent with specific instructions for larger issues. Do NOT proceed to ship until verification passes.

### Step 9: Browser-use validation

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

### Step 10: Ship PR

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

   Every diagram MUST include the theme init block and classDef styles:
   ```mermaid
   %%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
   graph TD
       classDef existing fill:#003f5c,stroke:#2f4b7c,color:#fff
       classDef modified fill:#665191,stroke:#a05195,color:#fff
       classDef new fill:#d45087,stroke:#f95d6a,color:#fff
       classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
   ```

   Include at minimum a **Component Relationship Diagram** showing what was added/modified. Add a **Sequence Diagram** if data flow changed. Keep diagrams focused (max 15 nodes).

3. Create the PR with full documentation using a HEREDOC:

```bash
gh pr create \
  --repo "$owner_repo" \
  --title "$pr_title" \
  --body "$(cat <<'EOF'
## Summary
[2-3 sentences]

## Architecture
[Mermaid diagrams here — REQUIRED]

## Changes
[Bullet list of significant changes]

## Test plan
[Test evidence — include TDD summary: N tests written first, all passing]

## Contract compliance
[Which epic contracts/invariants this implementation satisfies — omit if no epic context]

Closes #issue_number

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" \
  --base "$DEFAULT_BRANCH"
```

4. Link to the original issue via `Closes #N` in the body

### Step 11: Verify CI green

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

### Step 12: Update traceability matrix

If epic context exists, update the traceability matrix to reflect the tests written:

```bash
# Update status from "pending" to "covered" for each AC this ticket addressed
```

This can be a comment on the epic milestone or a commit to the traceability file, depending on whether other tickets are in flight.

Close the loop:
- Ask if the issue should be updated with a comment linking to the PR

# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → implement → **verify** → review → validate → ship.

## Ownership

**You own the solution, not just the code.** Before shipping ANY implementation, ask yourself:
- Am I proud of this work?
- Is it clean and elegant?
- Have I verified it actually works end-to-end?

If the answer to any of these is no — fix it. Don't ship "good enough."

**The validation loop is not negotiable.** Sub-agents will take shortcuts. The orchestrator is the quality gate. Never trust a sub-agent's self-reported "tests pass" — independently verify.

**Never punt to the user.** If Docker isn't running, start it. If a dependency is missing, install it. If you can't run the tests, that's YOUR problem to solve. "Want to skip this step?" is never the right question.

## Autonomy

**Run the full pipeline autonomously.** Do NOT pause between steps to ask "ready to proceed?" or "want to skip this?". Move through every step without asking for permission unless you genuinely need user input (e.g., ambiguous requirements in Step 2, a design decision where approaches conflict and there's no clear winner, or a blocking error you can't resolve).

Checkpoints where you MUST get user input:
- **Step 2** (Clarify requirements): Only if requirements are genuinely unclear or ambiguous
- **Step 3 Phase B** (Approach reconciliation): Only if approaches fundamentally conflict with no clear winner — present the trade-off and ask
- **Step 7** (Final check): Present the summary, then proceed to ship unless the user intervenes

Everything else — just do it.

## Usage
```
/implement <owner/repo#number> [--skip-browser-use] [--skip-review]
```

## Parameters
- `owner/repo#number`: GitHub issue to implement (e.g., `patwork/dgrd#42`)
- `--skip-browser-use`: Skip browser-use validation step (useful if target repo has no browser-use setup)
- `--skip-review`: Skip multi-AI review step

## Workflow

### Step 0: Resolve paths

Resolve the chief-wiggum install directory and the target repo path. **Never hardcode paths.**

```bash
CW_HOME=$(python3 "$(dirname "$0")/../../scripts/repo.py" home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
DEFAULT_BRANCH=$(gh repo view "$owner_repo" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
```

**Important**: `$CW_TMP` uses a unique session ID so concurrent `/implement` runs don't clobber each other's temp files.

All subsequent steps should work within `$TARGET_REPO`. Use `$CW_HOME` for chief-wiggum scripts/templates. Use `$CW_TMP` for temporary files (not `/tmp/`). Use `$DEFAULT_BRANCH` instead of hardcoding `main`.

### Step 1: Pick and read the ticket

Fetch the issue details:

```bash
gh issue view "$issue_number" --repo "$owner_repo" --json title,body,labels,assignees,milestone,comments
```

Present to the user:
- Title and description
- Acceptance criteria
- Labels and current status
- Any comments with additional context

### Step 2: Clarify requirements (only if needed)

Present a concise summary of what needs building (scope in/out). Only ask the user questions if the ticket is genuinely ambiguous — unclear acceptance criteria, conflicting requirements, or missing critical details. If the ticket is well-specified, state your understanding and move on.

If you do need to ask, keep it tight:
1. Summarise your understanding of what needs to be built
2. Ask ONLY questions where the answer isn't inferrable from the ticket or codebase
3. Confirm scope and proceed

### Step 3: Consult AIs on approach

This step has two phases, each in its own sub-agent. This keeps the heavy codebase exploration and synthesis out of the main context window.

#### Phase A: Gather approaches (parallel)

Run three consultations in parallel:

1. **Codex + Gemini** — Launch as background bash commands:
   ```bash
   python3 "$CW_HOME/scripts/consult_ai.py" codex $CW_TMP/approach-prompt.md -o $CW_TMP/approach-codex.md &
   python3 "$CW_HOME/scripts/consult_ai.py" gemini $CW_TMP/approach-prompt.md -o $CW_TMP/approach-gemini.md &
   wait
   ```

2. **Opus exploration** — Launch an **Opus sub-agent** (`subagent_type: "general-purpose"`, `model: "opus"`) in parallel with the above. This sub-agent should:
   - Explore the target repo codebase thoroughly (read key files, understand patterns)
   - Form its own implementation approach
   - Write its findings to `$CW_TMP/approach-opus.md`

Before launching, prepare the approach prompt at `$CW_TMP/approach-prompt.md` including:
- Ticket title, description, and acceptance criteria
- Codebase context (key files, architecture notes, relevant patterns)
- Question: "Propose an implementation approach including: files to modify/create, step-by-step plan, design decisions and trade-offs, risks/gotchas, testing strategy"

#### Phase B: Reconcile into implementation plan

Once all three approaches are ready, launch a **second Opus sub-agent** (`subagent_type: "general-purpose"`, `model: "opus"`) to reconcile them. This sub-agent should:

1. Read all three approach files (`approach-codex.md`, `approach-gemini.md`, `approach-opus.md`)
2. Identify consensus, conflicts, and unique insights
3. Produce a **comprehensive implementation plan** detailed enough that a Sonnet coding agent can execute it mechanically. The plan must include:
   - **Files to create/modify** with specific paths
   - **Ordered implementation steps** — each step should specify exactly what to do, in which file, with enough detail that no further codebase exploration is needed
   - **Code patterns to follow** — reference specific existing files/functions as templates
   - **Key design decisions** — where AIs agreed vs diverged, with a clear recommendation
   - **Test plan** — specific test cases to write and how to run them
   - **Open questions** for the user (if any)
4. Write the full plan to `$CW_TMP/implementation-plan.md`
5. Return a concise summary for the main thread

Present a concise summary to the user. If there are open questions that genuinely need user input (e.g., conflicting approaches with no clear winner), ask. Otherwise, proceed directly to Step 4.

### Step 4: Implement

Launch a **Sonnet sub-agent** in a worktree to do the implementation (`subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`). Sonnet is fast and cost-effective for coding tasks. Pass it the **full implementation plan** from `$CW_TMP/implementation-plan.md` (produced in Step 3 Phase B) plus any user feedback. The plan should be detailed enough that Sonnet can execute it step-by-step without needing to explore the codebase.

**Important**: The sub-agent should work in the target repo, not in chief-wiggum.

The sub-agent should:
1. Create a feature branch named after the ticket (e.g., `feat/42-add-dark-mode`)
2. Implement the approved approach
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

### Step 4.5: Orchestrator Validation

**This step is not delegatable.** The orchestrator (main thread) independently verifies the implementation. Do not trust the coding sub-agent's self-reported results — verify everything yourself.

1. **Navigate to the implementation worktree** (the sub-agent returns the worktree path)

2. **Run the full test suite** from the repo root:
   - If a `/test` skill or `make ci` target exists, use it
   - Otherwise: `pytest` (Python), `npm test` (Node), `go test ./...` (Go)
   - ALL tests must pass. Zero tolerance.

3. **Start services** and verify they work:
   - If `docker-compose.yml` exists: `docker compose up -d` and wait for healthy
   - If Docker isn't running, start it (`open -a Docker` on macOS, `sudo systemctl start docker` on Linux) and wait
   - Hit key endpoints (health checks, any endpoints the ticket specifies)
   - Verify responses match expectations

4. **Walk the acceptance criteria** from the ticket:
   - For each checkbox in the AC, verify it's actually met — not just "code exists" but "it works"
   - If the ticket says "health endpoint returns 200", curl it and confirm
   - If the ticket says "tests pass", run them yourself and confirm

5. **Quality check** — Read the key files the sub-agent produced:
   - Is the code idiomatic for the language?
   - Are there any obvious issues (missing error handling, security gaps, dead code)?
   - Does it follow existing patterns in the codebase?
   - Would you be proud to ship this?

6. **Clean up** — Stop any services you started (`docker compose down`)

If ANY verification fails: fix it directly (don't send it back to a sub-agent for trivial fixes), or re-launch the coding sub-agent with specific instructions for larger issues. Do NOT proceed to review until validation passes.

### Step 5: Multi-AI code review

**IMPORTANT**: Run this entire step inside a **Sonnet sub-agent** (`subagent_type: "general-purpose"`, `model: "sonnet"`). The main thread should only receive the synthesized review summary with actionable items.

The sub-agent should:

1. Get the diff from the implementation:
   ```bash
   git diff "$DEFAULT_BRANCH"...HEAD > $CW_TMP/impl-diff.txt
   ```

2. Prepare a review prompt using `$CW_HOME/templates/review-prompt.md` as a base. Read the template, replace the `{{TICKET_TITLE}}`, `{{TICKET_DESCRIPTION}}`, `{{ACCEPTANCE_CRITERIA}}`, and `{{DIFF}}` placeholders with actual values, and write to `$CW_TMP/review-prompt.md`.

3. Run external AI reviews in parallel:
   ```bash
   python3 "$CW_HOME/scripts/consult_ai.py" codex $CW_TMP/review-prompt.md -o $CW_TMP/review-codex.md &
   python3 "$CW_HOME/scripts/consult_ai.py" gemini $CW_TMP/review-prompt.md -o $CW_TMP/review-gemini.md &
   wait
   ```

4. Perform its own review of the diff.

5. Synthesize using:
   ```bash
   python3 "$CW_HOME/scripts/synthesize_reviews.py" $CW_TMP/review-codex.md $CW_TMP/review-gemini.md
   ```

6. Return a concise summary categorising each piece of feedback:
   - **Clear-cut fixes** (typos, obvious bugs, missing error handling): Apply automatically
   - **Style/preference issues**: Skip unless all reviewers agree
   - **Ambiguous or architectural feedback**: Flag for user decision

### Step 6: Browser-use validation

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

### Step 7: Ship PR

Create the PR using the `/ship` skill workflow:

1. Push the branch
2. Generate mermaid diagrams
3. Create the PR with full documentation
4. Link to the original issue

```bash
gh pr create \
  --repo "$owner_repo" \
  --title "$pr_title" \
  --body "$pr_body" \
  --base "$DEFAULT_BRANCH"
```

### Step 8: Verify CI green

**Do not declare the PR done until CI is green.** This is a hard gate — no exceptions.

1. After pushing, poll CI status:
   ```bash
   gh pr checks <pr_number> --repo "$owner_repo" --watch
   ```
2. If any check fails:
   - Fetch the failed job logs: `gh run view <run_id> --repo "$owner_repo" --log-failed`
   - Fix the failures (including pre-existing ones — every PR must leave CI green)
   - Push fixes and re-check
   - Repeat until all checks pass
3. Only after all checks are green, present the final summary:
   - **Summary**: What was implemented (files changed, approach taken)
   - **Test results**: CI status (all green)
   - **Review feedback**: What was addressed, what was deferred
   - **Browser-use results**: Screenshots and pass/fail (if applicable)
   - **Pre-existing fixes**: Any broken tests/lint we fixed that weren't ours
   - **Lingering questions**: Anything unresolved
4. Show the PR URL

Close the loop:
- Ask if the issue should be updated with a comment linking to the PR

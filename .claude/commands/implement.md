# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → implement → review → validate → ship.

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
3. Run the project's test suite:
   - Look for `Makefile`, `package.json`, or common test commands
   - Go projects: `go test ./...`
   - Node projects: `npm test`
   - Python projects: `pytest`
4. Run Playwright/E2E tests if they exist in the target repo
5. Fix issues iteratively until tests pass
6. If stuck after 3 attempts at the same error, report back to the user

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

Check if the target repo has a browser-use setup:

```bash
ls tests/browser-use/run.py 2>/dev/null || ls e2e/ 2>/dev/null || ls tests/e2e/ 2>/dev/null
```

If browser-use exists in the target repo:
1. Identify which scenarios are relevant to this ticket (match by tags or description)
2. Run the relevant scenarios:
   ```bash
   cd "$TARGET_REPO" && python3 tests/browser-use/run.py --scenario <ids>
   ```
3. Capture results and screenshots
4. Report pass/fail with details

If no browser-use setup exists, skip this step (or note it as a gap).

### Step 7: Final check & ship

Present a concise summary to the user:

1. **Summary**: What was implemented (files changed, approach taken)
2. **Test results**: All test output (unit, integration, E2E)
3. **Review feedback**: What was addressed, what was deferred
4. **Browser-use results**: Screenshots and pass/fail (if applicable)
5. **Lingering questions**: Anything unresolved

Then **proceed to ship** unless there are unresolved blockers. Do not ask "ready to ship?" — just do it.

### Step 8: Ship PR

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

Close the loop:
- Show the PR URL
- Ask if the issue should be updated with a comment linking to the PR

# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → implement → review → validate → ship.

## Usage
```
/implement <owner/repo#number> [--skip-browser-use] [--skip-review]
```

## Parameters
- `owner/repo#number`: GitHub issue to implement (e.g., `patwork/dgrd#42`)
- `--skip-browser-use`: Skip browser-use validation step (useful if target repo has no browser-use setup)
- `--skip-review`: Skip multi-AI review step

## Workflow

### Step 0: Resolve target repo

Resolve the `owner/repo` to a local path (clones via `gh` if not cached):

```bash
TARGET_REPO=$(python3 ~/repos/chief-wiggum/scripts/repo.py resolve "$owner_repo")
```

All subsequent steps should work within `$TARGET_REPO`.

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

### Step 2: Clarify requirements

Interactive session with the user:

1. Summarise your understanding of what needs to be built
2. Ask clarifying questions:
   - Are there edge cases not covered in the acceptance criteria?
   - What's the expected error behaviour?
   - Are there UI/UX preferences?
   - What files or areas of the codebase are likely affected?
3. Confirm the scope — what's in, what's out
4. Agree on how to verify it works (which tests, which scenarios)

Do NOT proceed until the user confirms the requirements are clear.

### Step 3: Consult AIs on approach

Prepare a prompt describing the ticket, the codebase context, and ask for an implementation approach. Write it to a temp file using the Write tool. The prompt should include:

- Ticket title, description, and acceptance criteria
- Codebase context (key files, architecture notes, relevant patterns)
- Question: "Propose an implementation approach including: files to modify/create, step-by-step plan, design decisions and trade-offs, risks/gotchas, testing strategy"

Save it to `/tmp/cw-approach-prompt.md`.

Run consultations in parallel:

```bash
python3 ~/repos/chief-wiggum/scripts/consult_ai.py codex /tmp/cw-approach-prompt.md > /tmp/cw-review-codex.md 2>&1 &
python3 ~/repos/chief-wiggum/scripts/consult_ai.py gemini /tmp/cw-approach-prompt.md > /tmp/cw-review-gemini.md 2>&1 &
wait
```

Also generate your own (Opus) approach analysis.

Synthesize all three approaches:
- **Where all agree**: High confidence, likely the right path
- **Where they disagree**: Present the trade-offs to the user
- **Unique suggestions**: Note which AI suggested it and why it might matter

Present the synthesized approach to the user and get approval before proceeding.

### Step 4: Implement

Launch a sub-agent in a worktree to do the implementation:

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

Get the diff from the implementation:

```bash
git diff main...HEAD > /tmp/cw-impl-diff.txt
```

Prepare a review prompt using `~/repos/chief-wiggum/templates/review-prompt.md` as a base. Read the template, replace the `{{TICKET_TITLE}}`, `{{TICKET_DESCRIPTION}}`, `{{ACCEPTANCE_CRITERIA}}`, and `{{DIFF}}` placeholders with actual values, and write to `/tmp/cw-review-prompt.md`.

Run reviews in parallel:

```bash
python3 ~/repos/chief-wiggum/scripts/consult_ai.py codex /tmp/cw-review-prompt.md > /tmp/cw-review-codex.md 2>&1 &
python3 ~/repos/chief-wiggum/scripts/consult_ai.py gemini /tmp/cw-review-prompt.md > /tmp/cw-review-gemini.md 2>&1 &
wait
```

Also perform your own (Opus) review of the diff.

Synthesize using:

```bash
python3 ~/repos/chief-wiggum/scripts/synthesize_reviews.py /tmp/cw-review-codex.md /tmp/cw-review-gemini.md
```

For each piece of feedback:
- **Clear-cut fixes** (typos, obvious bugs, missing error handling): Apply automatically
- **Style/preference issues**: Skip unless all reviewers agree
- **Ambiguous or architectural feedback**: Present to user for decision

### Step 6: Browser-use validation

Check if the target repo has a browser-use setup:

```bash
ls tests/browser-use/run.py 2>/dev/null || ls e2e/ 2>/dev/null || ls tests/e2e/ 2>/dev/null
```

If browser-use exists in the target repo:
1. Identify which scenarios are relevant to this ticket (match by tags or description)
2. Run the relevant scenarios:
   ```bash
   cd "$target_repo" && python3 tests/browser-use/run.py --scenario <ids>
   ```
3. Capture results and screenshots
4. Report pass/fail with details

If no browser-use setup exists, skip this step (or note it as a gap).

### Step 7: Final check

Present to the user:

1. **Summary**: What was implemented (files changed, approach taken)
2. **Test results**: All test output (unit, integration, E2E)
3. **Review feedback**: What was addressed, what was deferred
4. **Browser-use results**: Screenshots and pass/fail (if applicable)
5. **Lingering questions**: Anything unresolved

Ask: "Ready to ship this as a PR?"

### Step 8: Ship PR

If the user approves, create the PR using the `/ship` skill workflow:

1. Push the branch
2. Generate mermaid diagrams
3. Create the PR with full documentation
4. Link to the original issue

```bash
gh pr create \
  --repo "$owner_repo" \
  --title "$pr_title" \
  --body "$pr_body" \
  --base main
```

Close the loop:
- Show the PR URL
- Ask if the issue should be updated with a comment linking to the PR

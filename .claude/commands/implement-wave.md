# Implement Wave - Parallel Epic Implementation

Takes an epic and implements all its tickets in dependency-ordered waves. Tickets within a wave have no interdependencies and run as parallel sub-agents, each in its own worktree. After each wave lands, the next wave starts from an updated main.

This is the high-throughput alternative to running `/implement` sequentially per ticket. Use it when:
- The epic has 3+ tickets
- `/architect` has already run (contracts and invariants exist)
- You want maximum parallelism within dependency constraints

## Ownership

Same principles as `/implement`: you own the solution, not just the code. The validation loop is not negotiable. But here, validation happens at **two levels**:
- **Per-ticket**: Each parallel sub-agent runs the full `/implement` loop (Steps 5-8)
- **Per-wave**: After merging a wave, the orchestrator runs integration checks before starting the next wave

## Usage
```
/implement-wave <owner/repo> --epic "<milestone name>" [--skip-browser-use] [--max-parallel <N>]
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format
- `--epic`: The milestone name (e.g., `"Epic: Order Lifecycle"`)
- `--skip-browser-use`: Skip browser-use validation on all tickets
- `--max-parallel`: Maximum concurrent implementations (default: 3). Limits API rate pressure. Each ticket spawns 3 AI consultations, so `--max-parallel 3` means up to 9 concurrent API calls.

## Workflow

### Step 1: Resolve paths and load epic context

```bash
CW_HOME=$(python3 "$(dirname "$0")/../../scripts/repo.py" home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
DEFAULT_BRANCH=$(gh repo view "$owner_repo" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
```

Load epic artifacts from `$TARGET_REPO/docs/epics/[epic-slug]/`:
- `contracts.md` — REQUIRES/ENSURES
- `state-machines.md` — valid transitions
- `invariants.md` — cross-cutting rules
- `traceability.md` — AC to test mapping

**If epic artifacts don't exist, STOP.** Run `/architect` first. Wave implementation without contracts is unsafe — parallel tickets will diverge on design decisions.

### Step 2: Build the wave plan

Fetch all open tickets in the epic milestone:

```bash
gh issue list --repo "$owner_repo" --milestone "$epic_name" --state open --limit 100 --json number,title,body,labels
```

Parse the dependency graph from the epic plan. The `/plan-epic` output in the milestone description contains an **Implementation Order** with explicit `depends on #N` annotations. Parse these to build an adjacency list:

```
For each ticket:
  - Extract "depends on #X, #Y" from the implementation order
  - Build: { ticket_number: [dependency_numbers] }
```

Compute waves using topological sort:
- **Wave 1**: All tickets with zero unmet dependencies (no `depends on` or all dependencies already closed)
- **Wave 2**: Tickets whose dependencies are all in Wave 1
- **Wave N**: Tickets whose dependencies are all in Waves 1..N-1

Also identify **integration risks** from the epic plan — tickets within the same wave that touch the same files/components. Flag these for the merge step.

Present the wave plan to the user:

```markdown
## Wave Plan: [Epic Name]

### Wave 1 (parallel)
- #42 - Define order data model (S)
- #45 - Order notifications (S)

### Wave 2 (parallel, after Wave 1 merges)
- #43 - Order API endpoints (M) — depends on #42
- #46 - Order customer portal (L) — depends on #42

### Wave 3 (parallel, after Wave 2 merges)
- #44 - Order admin UI (M) — depends on #43
  ⚠️ Integration risk: shares OrderList component with #46 (Wave 2)

### Estimated timeline
- Waves: 3
- Max tickets per wave: 2
- Total tickets: 5
```

**CHECKPOINT**: Ask the user to confirm the wave plan. They may want to adjust (move a ticket between waves, split a wave, etc.).

### Step 3: Pre-flight checks

Before launching any implementation, verify all tools are working:

```bash
# Verify AI tool auth
echo "test" | codex exec --sandbox read-only - >/dev/null 2>&1 && echo "codex: OK" || echo "codex: FAIL"
echo "test" | gemini --yolo --output-format text -p "" >/dev/null 2>&1 && echo "gemini: OK" || echo "gemini: FAIL"

# Verify target repo is clean and on default branch
cd "$TARGET_REPO"
git checkout "$DEFAULT_BRANCH"
git pull --ff-only
git status --porcelain  # must be empty
```

**If any AI tool fails auth, fix it now.** Do not discover auth failures 20 minutes into a parallel wave. If the user needs to run an interactive login, tell them to run `! codex auth login` or similar.

**If the repo is not clean, STOP.** Uncommitted changes will cause worktree conflicts.

### Step 4: Execute waves

For each wave, in order:

#### 4a: Launch parallel implementations

For each ticket in the current wave (up to `--max-parallel`):

1. Create a **ticket-scoped temp directory**:
   ```bash
   TICKET_TMP="$CW_TMP/$ticket_number"
   mkdir -p "$TICKET_TMP"
   ```

2. Launch a **sub-agent in a worktree** (`subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`, `run_in_background: true`).

   The sub-agent prompt must include:
   - The full ticket details (title, body, acceptance criteria)
   - The epic context (contracts, invariants, state machines, traceability)
   - The implementation plan approach: run the **full `/implement` Steps 4-8** internally:
     - Step 4: Consult 3 AIs on approach (Codex + Gemini as background processes, self as the third perspective), reconcile into plan
     - Step 5: Write failing tests (TDD)
     - Step 6: Implement to make tests green
     - Step 7: Multi-AI code review (Codex + Gemini in parallel)
     - Step 8: Apply review fixes, run full test suite, run linting
   - **HARD RULES**:
     - Do NOT create or merge pull requests. Return the branch name and a summary.
     - You are in a git worktree. Verify with `git rev-parse --show-toplevel`. Never operate on the main checkout.
     - Write all temp files to `$TICKET_TMP/` (pass the path explicitly).
     - If you encounter a blocking error after 3 retries, report it and stop — do not silently skip steps.
   - The target repo path and default branch name
   - Instructions to report back: branch name, test results, review findings, any issues

3. If the wave has more tickets than `--max-parallel`, queue the excess. As each sub-agent completes, launch the next queued ticket.

**While sub-agents run**, the orchestrator should monitor for completion notifications. Do not poll — the Agent tool will notify when each background agent finishes.

#### 4b: Collect results

As each sub-agent in the wave completes, collect:
- **Branch name** and worktree path
- **Test results**: did the full suite pass?
- **Review findings**: what was flagged, what was fixed?
- **Issues**: any blockers or unresolved items?

If a sub-agent reports failure:
- **Test failures**: Attempt to diagnose. If it's a real issue, flag it. If it's a flaky test or pre-existing failure, note it.
- **Blocking errors**: Log the error. The ticket may need to be moved to a later wave or handled manually.
- **Timeout**: The sub-agent may still be running. Check its status before declaring failure.

Do not proceed to the merge step until ALL sub-agents in the wave have completed (successfully or with documented failures).

#### 4c: Merge wave to main

For each successfully completed ticket in the wave:

1. **Merge the worktree branch to the default branch**:
   ```bash
   cd "$TARGET_REPO"
   git checkout "$DEFAULT_BRANCH"
   git pull --ff-only
   git merge --no-ff "feat/$ticket_number-..." -m "feat: implement #$ticket_number — [title]"
   ```

2. **If merge conflicts occur** (expected when tickets in the same wave touch shared files):
   - Log which files conflict
   - Attempt automatic resolution for trivial conflicts (e.g., both sides added different imports)
   - For non-trivial conflicts: launch a **Sonnet sub-agent** to resolve the conflict, passing it both branches' changes and the epic contracts as constraints
   - After resolution, run the full test suite to verify the merge is clean

3. **Push to remote** after all tickets in the wave are merged:
   ```bash
   git push origin "$DEFAULT_BRANCH"
   ```

#### 4d: Wave integration check

After merging all tickets in the wave, **before starting the next wave**, run a quick integration check:

1. **Full test suite**: `go test ./...` / `npm test` / `pytest` — all must pass
2. **Linting**: `golangci-lint run ./...` / `npx eslint` — zero high-severity findings
3. **Build**: Verify the project compiles/builds cleanly
4. **Smoke test**: If services can be started, start them and verify health endpoints respond

If the integration check fails:
- **Test failure caused by merge**: Fix it before proceeding. Launch a Sonnet sub-agent to diagnose and fix.
- **Pre-existing failure**: Note it, fix it, commit the fix to main.
- **Build failure**: This is a hard blocker. Fix before next wave.

Only proceed to the next wave when the integration check passes.

#### 4e: Update traceability

After the wave merges, update the traceability matrix for all tickets in the wave:
- Mark acceptance criteria as `covered` where tests were written
- Note any gaps for the `/close-epic` retrospective

Commit the traceability update to main.

### Step 5: Create PRs (optional)

By the time all waves complete, all code is already on main (merged directly). However, if the user prefers PRs for audit trail:

For each ticket implemented, create a **retroactive PR** (already merged) by commenting on the issue with:
- Summary of changes
- Test evidence
- Review findings
- Link to the merge commit

Alternatively, create a single **epic-level PR** from a branch that contains all the wave commits, for a consolidated review.

Ask the user which approach they prefer, or if direct-to-main is fine.

### Step 6: Final report

```markdown
## Wave Implementation Complete: [Epic Name]

### Waves executed: N
| Wave | Tickets | Status | Duration |
|------|---------|--------|----------|
| 1 | #42, #45 | merged | ~25 min |
| 2 | #43, #46 | merged | ~35 min |
| 3 | #44 | merged | ~20 min |

### Per-ticket summary
| Ticket | Branch | Tests | Review | Merge | Issues |
|--------|--------|-------|--------|-------|--------|
| #42 | feat/42-... | 12 pass | 2 fixes applied | clean | — |
| #43 | feat/43-... | 8 pass | 1 fix applied | clean | — |
| #44 | feat/44-... | 15 pass | 3 fixes applied | conflict resolved | OrderList merge |

### Merge conflicts resolved: N
- [Details of each conflict and how it was resolved]

### Integration check results
- All waves passed integration checks
- Pre-existing failures fixed: N

### Next steps
1. `/close-epic owner/repo --epic "Epic: [Name]"` — run integration tests, mutation testing, and full epic validation
```

### Step 7: Offer next steps

After all waves complete:
1. If all tickets in the epic are now closed: suggest `/close-epic`
2. If some tickets remain open: list them and ask if they should be implemented in another wave
3. Close implemented issues:
   ```bash
   gh issue close $ticket_number --repo "$owner_repo" --comment "Implemented in wave $wave_number. Merged to $DEFAULT_BRANCH."
   ```

## Key Principles

- **Waves respect the dependency graph.** A ticket never starts until all its dependencies have merged to main. This is a hard constraint, not an optimisation hint.
- **Each sub-agent runs the full /implement loop.** No shortcuts — TDD, multi-AI review, linting, tests. The parallelism is in running multiple tickets simultaneously, not in cutting steps per ticket.
- **The orchestrator owns the merge.** Sub-agents produce branches. The orchestrator merges, resolves conflicts, and runs integration checks. Sub-agents never merge or create PRs.
- **Integration checks between waves catch seam bugs early.** A test that passes in isolation but fails after merge is exactly the kind of bug this workflow is designed to catch.
- **Max-parallel limits API pressure.** Three concurrent tickets means up to 9 simultaneous AI API calls (3 consultations per ticket). Respect rate limits.
- **Failed tickets don't block the wave.** If one ticket in a wave fails, the other tickets in that wave can still merge. The failed ticket is retried or deferred to a later wave.
- **Pre-flight catches auth failures early.** Discovering expired Codex auth 20 minutes into a 3-ticket wave wastes all 3 tickets' work. Check before launching.

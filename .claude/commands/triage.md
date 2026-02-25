# Triage - Read & Prioritise Issues

Read all open issues from a GitHub repo and present a prioritised view for sprint planning.

## Usage
```
/triage <owner/repo>
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format (e.g., `patwork/dgrd`)

## Execution Strategy

**IMPORTANT**: Run the entire triage workflow (Steps 1-4) inside a **sonnet subagent** using the Task tool (`subagent_type: "general-purpose"`, `model: "sonnet"`). This keeps the heavy data fetching and analysis out of the main context window.

The subagent prompt should instruct it to:
1. Fetch all open issues (Step 1)
2. Group, categorise, and rank them (Steps 2-3)
3. Return ONLY the concise summary output described in Step 4

Then present the subagent's summary to the user and proceed to Step 5 (interactive discussion) in the main thread.

## Workflow (executed by subagent)

### Step 1: Fetch all open issues

```bash
gh issue list --repo "$owner_repo" --state open --limit 200 --json number,title,labels,assignees,milestone,createdAt,updatedAt,body
```

### Step 2: Group and categorise

Organise issues into groups based on labels:
- **Bugs** (labels containing: bug, fix, defect)
- **Features** (labels containing: feature, enhancement, feat)
- **Chores** (labels containing: chore, refactor, docs, ci)
- **Unlabelled** (no labels — flag these for triage)

Within each group, extract from the issue body:
- Severity (if mentioned)
- Effort estimate (if mentioned, or infer: S/M/L/XL based on description complexity)
- Dependencies (does it reference other issues?)

### Step 3: Suggest priority ordering

Rank issues using this heuristic:
1. **Critical bugs** first (production broken, data loss risk)
2. **High-severity bugs** (significant UX impact)
3. **Features blocking other work** (dependency chains)
4. **Quick wins** (small effort, high value)
5. **Medium features** (significant value, moderate effort)
6. **Nice-to-haves** (low priority, can wait)

### Step 4: Return concise summary

The subagent must return ONLY:

1. **Prioritised table** — all open issues in priority order:
```
Pri | # | Title | Type | Effort | Labels
----|---|-------|------|--------|-------
1   | 42| Fix login crash | bug | S | bug, critical
2   | 38| Add dark mode   | feat| L | feature
...
```

2. **Stats line** — total count and breakdown (X bugs, Y features, Z chores, W unlabelled)

3. **Top 3 picks** — the 3 best candidates for next sprint, each with a one-sentence rationale

Nothing else. No raw issue bodies, no verbose analysis.

### Step 5: Interactive discussion (main thread)

After presenting the subagent's summary, ask the user:
1. Does this priority ordering look right?
2. Any items that should be promoted or demoted?
3. Are there issues that should be closed (stale, duplicate, won't-fix)?
4. Ready to plan a sprint? (suggest `/plan-sprint`)

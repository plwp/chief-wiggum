# Triage - Read & Prioritise Issues

Read all open issues from a GitHub repo and present a prioritised view for sprint planning.

## Usage
```
/triage <owner/repo>
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format (e.g., `patwork/dgrd`)

## Workflow

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

### Step 4: Present the backlog

Display a prioritised table:

```
Priority | # | Title | Type | Effort | Labels
---------|---|-------|------|--------|-------
1        | 42| Fix login crash | bug | S | bug, critical
2        | 38| Add dark mode   | feat| L | feature
...
```

Then provide:
- Total open issues count
- Breakdown by type (X bugs, Y features, Z chores)
- Issues needing triage (unlabelled or unclear)
- Suggested "next sprint" shortlist (top 5-8 items)

### Step 5: Interactive discussion

Ask the user:
1. Does this priority ordering look right?
2. Any items that should be promoted or demoted?
3. Are there issues that should be closed (stale, duplicate, won't-fix)?
4. Ready to plan a sprint? (suggest `/plan-sprint`)

# Plan Sprint - Interactive Sprint Planning

Facilitate an interactive discussion about what to build next, resulting in a prioritised sprint plan.

## Usage
```
/plan-sprint <owner/repo>
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format

## Workflow

### Step 1: Load current backlog

Fetch the open issues (same as `/triage`):

```bash
gh issue list --repo "$owner_repo" --state open --limit 200 --json number,title,labels,assignees,milestone,createdAt,updatedAt,body
```

Also check what's currently in progress:

```bash
gh issue list --repo "$owner_repo" --state open --limit 50 --json number,title,labels,assignees --jq '.[] | select(.assignees | length > 0)'
```

### Step 2: Present the landscape

Show the user:
- **In progress**: Issues currently assigned
- **Ready to build**: Prioritised backlog (top 10-15)
- **Recently closed**: Last 5 closed issues (for momentum/context)

```bash
gh issue list --repo "$owner_repo" --state closed --limit 5 --json number,title,closedAt
```

### Step 3: Facilitate discussion

This is an interactive session. Guide the conversation:

1. **What's the goal for this sprint?** Ask about themes, deadlines, or release targets.
2. **Review the top candidates.** For each, briefly explain what it involves and estimated effort.
3. **Capacity check.** How many S/M/L tickets can fit in this sprint? (rule of thumb: 1 L = 2 M = 4 S)
4. **Dependencies.** Flag if any selected tickets depend on each other and suggest ordering.
5. **Risks.** Note any tickets that are vague, have unclear requirements, or need client input.

### Step 4: Agree on the plan

Once the user is happy, produce a sprint plan:

```markdown
## Sprint Plan — [Date Range]

### Goal
[One-sentence sprint goal]

### Selected Tickets (in order)
1. #42 - Fix login crash (S, bug) — assigned to: TBD
2. #38 - Add dark mode (L, feature) — assigned to: TBD
3. ...

### Total Effort
- Small: X tickets
- Medium: Y tickets
- Large: Z tickets

### Risks & Dependencies
- #38 depends on #42 (fix must land first)
- #45 needs client clarification before starting

### Out of Sprint
- #50, #51, #52 — deferred to next sprint
```

### Step 5: Offer next steps

Ask the user:
1. Should I assign these tickets to anyone?
2. Should I add a milestone label to these issues?
3. Ready to start implementing? (suggest `/implement owner/repo#42` for the first ticket)

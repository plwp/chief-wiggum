# Plan Epic - Dependency-Ordered Epic Planning

Group related issues into a coherent epic with a dependency graph, integration risks, and ticket ordering. An epic is a coherent unit of user value, not an arbitrary time-box.

## Usage
```
/plan-epic <owner/repo> [epic description or issue numbers]
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format
- Optional: free-text epic description ("multi-tenant auth") or explicit issue numbers ("#12 #15 #18")

## Workflow

### Step 1: Load the backlog

Fetch all open issues and recent context:

```bash
gh issue list --repo "$owner_repo" --state open --limit 200 --json number,title,labels,assignees,milestone,createdAt,updatedAt,body
gh issue list --repo "$owner_repo" --state open --limit 50 --json number,title,labels,assignees --jq '.[] | select(.assignees | length > 0)'
gh issue list --repo "$owner_repo" --state closed --limit 10 --json number,title,closedAt,labels
```

Also fetch milestones to see if there's existing epic-level organisation:
```bash
gh api repos/$owner_repo/milestones --jq '.[] | {title, description, open_issues, closed_issues}'
```

### Step 2: Identify the epic

If the user provided issue numbers, fetch those specific issues and infer the epic theme.

If the user provided a description (e.g., "booking flow" or "auth overhaul"), scan the backlog and group issues that belong to this theme. Use title, labels, description, and cross-references to cluster.

If neither was provided, present the backlog grouped by natural themes and ask the user which cluster to plan as an epic.

Present the candidate epic:
- **Epic name**: A short, descriptive name (e.g., "Booking State Machine", "Multi-Tenant Auth")
- **Goal**: One sentence — what user value does this epic deliver when complete?
- **Candidate tickets**: List with number, title, type, effort estimate

Ask the user to confirm or adjust the ticket set.

### Step 3: Build the dependency graph

For each ticket in the epic, determine:
1. **Hard dependencies**: "X must land before Y" (data model before API, API before UI)
2. **Soft dependencies**: "X is easier after Y but could be done in either order"
3. **Integration points**: Where will two or more tickets modify the same file, API, data model, or screen?

Present as an ordered implementation sequence:

```markdown
## Epic: [Name]

### Implementation Order
1. #42 - Define booking data model (S) — foundation, blocks everything
2. #43 - Booking API endpoints (M) — depends on #42
3. #44 - Booking admin UI (M) — depends on #43, shares screen with #46
   ⚠️ Integration risk: #44 and #46 both modify BookingList component
4. #45 - Booking notifications (S) — depends on #43
5. #46 - Booking customer portal (L) — depends on #43
   ⚠️ Integration risk: #44 and #46 both read booking status differently

### Dependency Graph
[mermaid diagram showing the dependency DAG]

### Integration Risks
- BookingList component: touched by #44 and #46 — define shared contract in /architect
- Booking status field: read by #44, #45, #46 with different assumptions — need single source of truth
- Notification trigger: #45 hooks into #43's create endpoint — coordinate API contract
```

### Step 4: Identify cross-cutting concerns

These are things that span multiple tickets and MUST be resolved before implementation begins (in `/architect`):

- **Shared data models**: What entities do multiple tickets read/write?
- **API contracts**: What endpoints do multiple tickets depend on?
- **State machines**: What states/transitions span tickets?
- **Shared UI components**: What screens or components do multiple tickets touch?
- **Invariants**: What rules must hold across the full epic? (e.g., "a booking always has a client_id after confirmation")

Present these explicitly — they become the input to `/architect`.

### Step 5: Confirm the plan

Present the full epic plan:

```markdown
## Epic Plan: [Name]

### Goal
[One sentence]

### Tickets (in implementation order)
[Ordered list with dependencies noted]

### Cross-Cutting Concerns (for /architect)
- [List of shared models, contracts, state machines, invariants]

### Integration Risks
- [Where tickets overlap — these get integration tests in /architect]

### Estimated Total Effort
- Small: X, Medium: Y, Large: Z

### Not in This Epic
- [Tickets deliberately excluded, with reason]
```

Ask the user to confirm. Adjust if needed.

### Step 6: Create the GitHub milestone

Create a milestone to track the epic:

```bash
gh api repos/$owner_repo/milestones -f title="Epic: [Name]" -f description="[Goal]. Tickets: #42, #43, #44, #45, #46"
```

Add all epic tickets to the milestone:
```bash
gh issue edit $issue_number --repo "$owner_repo" --milestone "Epic: [Name]"
```

### Step 7: Offer next steps

```markdown
Epic planned. Next steps:
1. `/architect owner/repo --epic "Epic: [Name]"` — define contracts, invariants, and integration tests before implementation
2. Then `/implement owner/repo#42` for the first ticket (it will pick up the epic context)
```

## Key Principles

- **Epics are goal-shaped, not time-shaped.** "Booking flow works end-to-end" is an epic. "Two weeks of work" is not.
- **Order by dependencies, not priority.** A P2 data model ticket that blocks three P1 UI tickets goes first.
- **Name the integration risks explicitly.** Every pair of tickets that touches the same surface area gets flagged. These become integration tests in `/architect`.
- **Cross-cutting concerns are the input to /architect.** Don't try to resolve them here — just identify them clearly.

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

### Step 0: Resolve CW_HOME

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
```

### Step 1: Load the backlog

Resolve the backlog via `tracker.py` instead of calling `gh issue` directly —
this is what makes the workflow backend-agnostic (GitHub today, `local` or
others per `docs/cw/tracker.json` in the target repo). See `docs/tracker.md`
for the full interface.

```bash
python3 "$CW_HOME/scripts/tracker.py" list "$owner_repo"
```

This returns every issue (any state) with full metadata (`ref`, `title`,
`body`, `state`, `labels`, `assignee`, `epic`, `url_or_path`). Derive the
open/closed/assigned views you need from this single JSON list client-side
(filter on `state`/`assignee`) rather than issuing separate queries.

Also fetch milestones to see if there's existing epic-level organisation
(milestones are GitHub-specific metadata, not part of the tracker-agnostic
`Issue` shape, so this stays a direct `gh api` call):
```bash
gh api repos/$owner_repo/milestones --jq '.[] | {title, description, open_issues, closed_issues}'
```

### Step 2: Identify the epic

If the user provided issue numbers, fetch those specific issues and infer the epic theme.

If the user provided a description (e.g., "order flow" or "auth overhaul"), scan the backlog and group issues that belong to this theme. Use title, labels, description, and cross-references to cluster.

If neither was provided, present the backlog grouped by natural themes and ask the user which cluster to plan as an epic.

Present the candidate epic:
- **Epic name**: A short, descriptive name (e.g., "Order Lifecycle", "Multi-Tenant Auth")
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
1. #42 - Define order data model (S) — foundation, blocks everything
2. #43 - Order API endpoints (M) — depends on #42
3. #44 - Order admin UI (M) — depends on #43, shares screen with #46
   ⚠️ Integration risk: #44 and #46 both modify OrderList component
4. #45 - Order notifications (S) — depends on #43
5. #46 - Order customer portal (L) — depends on #43
   ⚠️ Integration risk: #44 and #46 both read order status differently

### Dependency Graph
[mermaid diagram showing the dependency DAG]

### Integration Risks
- OrderList component: touched by #44 and #46 — define shared contract in /architect
- Order status field: read by #44, #45, #46 with different assumptions — need single source of truth
- Notification trigger: #45 hooks into #43's create endpoint — coordinate API contract
```

### Step 4: Identify cross-cutting concerns

These are things that span multiple tickets and MUST be resolved before implementation begins (in `/architect`):

- **Shared data models**: What entities do multiple tickets read/write?
- **API contracts**: What endpoints do multiple tickets depend on?
- **State machines**: What states/transitions span tickets?
- **Shared UI components**: What screens or components do multiple tickets touch?
- **Invariants**: What rules must hold across the full epic? (e.g., "an order always has a customer_id after confirmation")

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

Create a milestone to track the epic. The milestone description **must** include a machine-readable dependency block so that `/implement-wave` can parse the DAG without brittle markdown parsing.

Generate the `<!-- DEPENDENCIES -->` block from a JSON adjacency map with the tested formatter (it de-dupes and sorts deps so the output always matches the parser), then embed it in the milestone description:

```bash
DEPS=$(python3 "$CW_HOME/scripts/epic_metadata.py" format-deps '{"42": [], "43": [42], "44": [43], "45": [43], "46": [42]}')
gh api repos/$owner_repo/milestones -f title="Epic: [Name]" -f description="$(cat <<EOF
[Goal]

$DEPS
EOF
)"
```

The block is an HTML comment (invisible in rendered markdown) with one line per ticket in the format `#N: [#dep1, #dep2]`; empty brackets `[]` means no dependencies. This block is the **canonical source** for the dependency graph — `/implement-wave` parses it to compute waves. Do not hand-write it; always generate it with `format-deps` so it round-trips through the parser.

Add all epic tickets to the milestone via `tracker.py group` (the milestone
already exists from the step above, so this only assigns each issue to it):
```bash
python3 "$CW_HOME/scripts/tracker.py" group "Epic: [Name]" "gh:$owner_repo#42" "gh:$owner_repo#43" "gh:$owner_repo#44"
```

### Step 7: Offer next steps

```markdown
Epic planned. Next steps:
1. `/architect owner/repo --epic "Epic: [Name]"` — define contracts, invariants, and integration tests before implementation
2. Then `/implement owner/repo#42` for the first ticket (it will pick up the epic context)
3. Or `/implement-wave owner/repo --epic "Epic: [Name]"` to implement all tickets in parallel waves
```

## Key Principles

- **Epics are goal-shaped, not time-shaped.** "Order flow works end-to-end" is an epic. "Two weeks of work" is not.
- **Order by dependencies, not priority.** A P2 data model ticket that blocks three P1 UI tickets goes first.
- **Name the integration risks explicitly.** Every pair of tickets that touches the same surface area gets flagged. These become integration tests in `/architect`.
- **Cross-cutting concerns are the input to /architect.** Don't try to resolve them here — just identify them clearly.

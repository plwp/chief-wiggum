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

### Step 0: Resolve CW_HOME, the target repo root, and the tracker backend

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
target_root=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
backend=$(python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" backend)
```

`$target_root` is the local checkout of the target repo; every `tracker.py`
call below passes `--repo-root "$target_root"` so the target repo's
`docs/cw/tracker.json` backend config is honored regardless of the current
working directory. `$backend` (`github`, `local`, ...) gates the
GitHub-specific milestone plumbing below — when the repo is configured
`local`, this workflow must never mutate GitHub.

### Step 1: Load the backlog

Resolve the backlog via `tracker.py` instead of calling `gh issue` directly —
this is what makes the workflow backend-agnostic. See `docs/tracker.md` for
the full interface.

```bash
python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" list "$owner_repo"
```

This returns every issue (any state) with full metadata (`ref`, `title`,
`body`, `state`, `labels`, `assignee`, `epic`, `url_or_path`). Derive the
open/closed/assigned views you need from this single JSON list client-side
(filter on `state`/`assignee`) rather than issuing separate queries. Keep the
`ref` values — they are what Step 6 groups (`gh:owner/repo#N` for the
`github` backend, `local:docs/issues/NNNN.md` for `local`).

For the `github` backend only, also fetch milestones to see if there's
existing epic-level organisation (milestones are GitHub-specific metadata,
not part of the tracker-agnostic `Issue` shape). For other backends the
`epic` field on each issue already carries the grouping.
```bash
if [ "$backend" = "github" ]; then
  gh api repos/$owner_repo/milestones --jq '.[] | {title, description, open_issues, closed_issues}'
fi
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

### Step 6: Record the epic (grouping + dependency graph)

The epic needs two durable artifacts: the **grouping** (which issues belong to
it) and the **dependency graph** (a machine-readable block `/implement-wave`
parses to compute waves, without brittle markdown parsing).

First generate the `<!-- DEPENDENCIES -->` block from a JSON adjacency map
with the tested formatter (it de-dupes and sorts deps so the output always
matches the parser). This is backend-independent:

```bash
DEPS=$(python3 "$CW_HOME/scripts/epic_metadata.py" format-deps '{"42": [], "43": [42], "44": [43], "45": [43], "46": [42]}')
```

The block is an HTML comment (invisible in rendered markdown) with one line per ticket in the format `#N: [#dep1, #dep2]`; empty brackets `[]` means no dependencies. This block is the **canonical source** for the dependency graph. Do not hand-write it; always generate it with `format-deps` so it round-trips through the parser.

**Where the block is stored depends on the backend.** For `github` it lives in
a milestone description (as today); for any other backend it lives in
`docs/epics/<slug>/epic.md` in the target repo, committed to git, so
`/implement-wave` has a storage path with no GitHub dependency. Never run the
`gh api` milestone mutation when the repo is configured with a non-github
backend:

```bash
if [ "$backend" = "github" ]; then
  gh api repos/$owner_repo/milestones -f title="Epic: [Name]" -f description="$(cat <<EOF
[Goal]

$DEPS
EOF
)"
else
  EPIC_SLUG=$(python3 "$CW_HOME/scripts/env.py" slug "Epic: [Name]")
  mkdir -p "$target_root/docs/epics/$EPIC_SLUG"
  cat > "$target_root/docs/epics/$EPIC_SLUG/epic.md" <<EOF
# Epic: [Name]

[Goal]

$DEPS
EOF
  git -C "$target_root" add "docs/epics/$EPIC_SLUG/epic.md"
  git -C "$target_root" commit -m "docs: record epic plan for Epic: [Name]"
fi
```

Then assign all epic tickets to the epic via `tracker.py group`, using the
`ref` values captured in Step 1 (`gh:owner/repo#N` refs for `github` — the
milestone above already exists, so this only assigns issues to it;
`local:docs/issues/NNNN.md` refs for `local`, where grouping is a frontmatter
`epic` field):

```bash
python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" group "Epic: [Name]" "$ref1" "$ref2" "$ref3"
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

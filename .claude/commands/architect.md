# Architect - Epic-Level Design & Contract Specification

Runs once per epic, before any ticket is implemented. Produces the architectural artifacts that every subsequent `/implement` call inherits: data contracts, invariants, state machines, an ADR, integration test specifications, and a requirements traceability matrix.

## Usage
```
/architect <owner/repo> --epic "<milestone name>"
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format
- `--epic`: The milestone name created by `/plan-epic` (e.g., `"Epic: Order Lifecycle"`)

## Autonomy

This is an interactive skill. The user confirms each major artifact before it's committed. Checkpoints:
- **Step 6**: Contracts, invariants, and state machines review (after multi-AI validation)
- **Step 9**: Full architecture package review before commit

Everything else runs autonomously.

## Workflow

### Step 1: Resolve paths and load epic context

```bash
CW_HOME=$(python3 "$(dirname "$0")/../../scripts/repo.py" home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
DEFAULT_BRANCH=$(gh repo view "$owner_repo" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
```

Fetch all issues in the epic milestone:
```bash
gh issue list --repo "$owner_repo" --milestone "$epic_name" --state open --limit 100 --json number,title,body,labels
```

Read each issue's full body to extract acceptance criteria, technical notes, and cross-references.

Also load any existing architecture docs from the target repo:
```bash
cat "$TARGET_REPO/ARCHITECTURE.md" 2>/dev/null
cat "$TARGET_REPO/CLAUDE.md" 2>/dev/null
ls "$TARGET_REPO/docs/adr/" 2>/dev/null
```

### Step 2: Explore the codebase

Launch an **Explore sub-agent** (`subagent_type: "Explore"`, thoroughness: "very thorough") to understand the current state of the areas this epic will touch. The agent should report:

- Current data models (structs, types, schemas) relevant to the epic
- Existing API endpoints that will be extended or consumed
- UI components/screens that will be modified
- Test infrastructure (what frameworks, what coverage exists)
- Existing patterns and conventions in the affected areas

Write findings to `$CW_TMP/codebase-context.md`.

### Step 3: Multi-AI architectural consultation

Prepare a consultation prompt at `$CW_TMP/architect-prompt.md` including:
- Epic goal and ticket list with acceptance criteria
- Codebase context from Step 2
- Cross-cutting concerns identified by `/plan-epic`
- Specific questions:
  1. What is the canonical data model for the entities this epic touches? Define the single source of truth.
  2. What state machines exist? Define all valid states and transitions.
  3. What invariants must hold across the full epic? (e.g., "an order always has a customer_id after confirmation")
  4. What are the API contracts — preconditions, postconditions, error cases?
  5. Where are the integration risks and how should we test them?
  6. What could go wrong between tickets? (dual sources of truth, race conditions, inconsistent reads)

Fire all three consultations in parallel:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" codex $CW_TMP/architect-prompt.md -o $CW_TMP/architect-codex.md --cwd "$TARGET_REPO" &
python3 "$CW_HOME/scripts/consult_ai.py" gemini $CW_TMP/architect-prompt.md -o $CW_TMP/architect-gemini.md --cwd "$TARGET_REPO" &
wait
```

Launch an **Opus sub-agent** (`subagent_type: "general-purpose"`, `model: "opus"`) in parallel to explore the codebase and produce its own architectural analysis at `$CW_TMP/architect-opus.md`.

**HARD RULE**: Wait for ALL THREE before proceeding.

### Step 4: Synthesise into architectural artifacts

Launch a **second Opus sub-agent** to reconcile all three consultations into the following artifacts. Each artifact is a separate file in `$CW_TMP/`:

#### 4a. Data Contracts (`contracts.md`)

For every entity and API endpoint the epic touches, define:

```markdown
## Entity: Order

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| id | ObjectID | always | MongoDB _id | immutable |
| customer_id | ObjectID | after confirmation | customers collection | MUST be set during confirmation transition |
| status | enum | always | order.status | valid: draft, pending, confirmed, in_progress, completed, cancelled |
| items | []ObjectID | after confirmation | items collection | NEVER store item names as strings — always reference |

### API: POST /api/v1/orders
- **REQUIRES**: authenticated staff OR valid public submission token; at least one item_id; valid date range (end_date > start_date)
- **ENSURES**: order created with status "draft"; order.id returned; order visible on admin list within 1s
- **ERROR CASES**: 400 if dates invalid; 401 if no auth; 409 if capacity exceeded

### API: POST /api/v1/orders/:id/confirm
- **REQUIRES**: order exists; status is "pending"; customer_id is set; at least one item_id
- **ENSURES**: status transitions to "confirmed"; confirmation notification sent (or error surfaced if notification service unavailable)
- **INVARIANT**: After this call, order.customer_id is NEVER null
```

#### 4b. State Machines (`state-machines.md`)

For every state machine in the epic, define all states and valid transitions:

```markdown
## Order Status State Machine

### States
- `draft` — initial, admin-created, incomplete data
- `pending` — submitted via intake, awaiting confirmation
- `confirmed` — approved, awaiting start
- `in_progress` — work in progress
- `completed` — complete
- `cancelled` — terminated before completion

### Transitions
| From | To | Trigger | Guard Conditions |
|------|----|---------|-----------------|
| draft | pending | submit | customer_id set, items non-empty, dates valid |
| pending | confirmed | confirm | capacity available, pre-start checks complete |
| pending | cancelled | cancel | — |
| confirmed | in_progress | start | resource assigned |
| confirmed | cancelled | cancel | refund policy applied |
| in_progress | completed | complete | balance settled OR ack_unpaid |

### Invalid Transitions (must be rejected)
- draft → confirmed (skips pending validation)
- completed → in_progress (irreversible)
- cancelled → any (terminal state)

### Invariants
- An order in `confirmed` or later MUST have: customer_id, item_ids (non-empty), valid dates
- An order in `in_progress` MUST have: resource_id
- `cancelled` is terminal — no transitions out
```

#### 4c. Invariants (`invariants.md`)

Cross-cutting rules that must hold at ALL times, not just within a single transition:

```markdown
## Epic Invariants

### Data Integrity
1. **Single source of truth**: Item names are NEVER stored as strings on orders. Always reference item_ids and resolve at read time.
2. **Customer linkage**: Every order with status >= "pending" has a non-null customer_id that references a valid customer document.
3. **Date validity**: end_date > start_date on every order, enforced at write time.

### Consistency
4. **Screen agreement**: Dashboard, List View, Calendar, and Detail View MUST derive record counts from the same query/aggregation. No screen-specific counting logic.
5. **Capacity truth**: Capacity View and Summary View MUST use the same occupancy calculation. Define once, use everywhere.

### Operational Safety
6. **No false success**: If an operation depends on an external service (email, payment), the success toast MUST NOT display unless the service call succeeded. If the service is unavailable, surface the error — never silently swallow.
7. **Backend capability gating**: Before calling an endpoint that requires configuration (email, payment, SMS), check the capability endpoint. Disable the UI affordance if the capability is unavailable.
```

#### 4d. ADR (`adr.md`)

Architectural Decision Record capturing the key decisions for this epic:

```markdown
# ADR: [Epic Name]

## Status
Accepted

## Context
[What problem are we solving? What constraints exist?]

## Decisions

### 1. [Decision Title]
- **Decision**: [What we chose]
- **Alternatives considered**: [What we rejected and why]
- **AI consensus**: [Where the three AIs agreed/diverged]
- **Trade-offs**: [What we're giving up]

### 2. ...

## Consequences
- [Positive consequences]
- [Negative consequences / tech debt accepted]
- [Follow-up work needed]
```

#### 4e. Integration Test Specification (`integration-tests.md`)

Tests that validate cross-ticket behaviour. These are NOT run per-ticket — they're run by `/close-epic` after all tickets land.

```markdown
## Integration Tests: [Epic Name]

### Test 1: Order visible on all surfaces after creation
- **Setup**: Create a confirmed order via API
- **Assert**:
  - Order appears in admin List View
  - Order appears on Calendar for the correct date
  - Order appears on Detail View
  - Order appears in Customer Portal (if applicable)
  - All surfaces show the same customer name, item names, dates, and status
- **Why**: Catches dual-source-of-truth pattern

### Test 2: Invalid state transitions rejected
- **Setup**: Create orders in each state
- **Assert**: Every invalid transition from the state machine returns 400/409
- **Why**: Catches missing-guard-condition pattern

### Test 3: Capability gating prevents false success
- **Setup**: Disable email service configuration
- **Assert**: Email-dependent operations return an error, UI disables the action
- **Why**: Catches false-success pattern
```

#### 4f. Requirements Traceability Matrix (`traceability.md`)

Maps every acceptance criterion from every ticket to the test(s) that will verify it:

```markdown
## Traceability Matrix: [Epic Name]

| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |
|--------|---------------------|-----------|-----------------|----------|--------|
| #42 | GET /health returns 200 | api_test.go:TestHealth | — | — | pending |
| #42 | Order model has all fields | model_test.go:TestOrderFields | — | — | pending |
| #43 | Create order returns 201 | api_test.go:TestCreateOrder | IT-1: visible on all surfaces | — | pending |
| #44 | Admin can see orders list | — | IT-1 | playwright/orders.spec | pending |
| #44 | Start requires confirmed status | — | IT-2: invalid transitions | — | pending |
```

Each row's "Status" column starts as `pending` and is updated to `covered` when `/implement` writes the test, then `passing` when `/close-epic` verifies it.

### Step 5: Multi-AI validation of artifacts

The synthesised artifacts are the most critical output in the pipeline — every ticket inherits them. Get a second opinion before presenting to the user.

Prepare a validation prompt at `$CW_TMP/validate-artifacts-prompt.md` containing:
- The full contents of all six artifacts (contracts, state machines, invariants, ADR, integration tests, traceability)
- The epic goal and ticket list
- Specific questions:
  1. Are there any contracts that contradict each other?
  2. Are there state transitions missing from the state machine? Any unreachable states?
  3. Are the invariants complete — could a ticket satisfy all listed invariants and still ship a bug?
  4. Does the integration test suite cover the highest-risk cross-ticket interactions?
  5. Are there acceptance criteria in the traceability matrix that have no planned test?

Run Codex and Gemini in parallel:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" codex $CW_TMP/validate-artifacts-prompt.md -o $CW_TMP/validate-codex.md --cwd "$TARGET_REPO" &
python3 "$CW_HOME/scripts/consult_ai.py" gemini $CW_TMP/validate-artifacts-prompt.md -o $CW_TMP/validate-gemini.md --cwd "$TARGET_REPO" &
wait
```

Review both responses. Apply clear improvements to the artifacts. Flag genuine disagreements for the user in Step 6.

### Step 6: Present artifacts to user

**CHECKPOINT**: Show the user a summary of all artifacts:
- Contracts: key entities and their REQUIRES/ENSURES
- State machines: states and transitions
- Invariants: the cross-cutting rules
- ADR: key decisions with trade-offs
- Integration tests: what will be validated at epic close
- Traceability: coverage gaps (any AC without a planned test)

Ask for feedback. Iterate if needed. This is the most important review in the entire pipeline — getting the contracts wrong here means every ticket inherits the wrong constraints.

### Step 7: Commit artifacts to target repo

Create an `epic/` directory structure in the target repo:

```bash
mkdir -p "$TARGET_REPO/docs/epics/[epic-slug]"
```

Copy all artifacts:
```bash
cp $CW_TMP/contracts.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/state-machines.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/invariants.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/adr.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/integration-tests.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/traceability.md "$TARGET_REPO/docs/epics/[epic-slug]/"
```

Commit and push:
```bash
cd "$TARGET_REPO"
git add docs/epics/
git commit -m "arch: add epic architecture — [Epic Name]

Contracts, state machines, invariants, ADR, integration test spec,
and traceability matrix for the [Epic Name] epic.

Generated by /architect"
git push
```

### Step 8: Update issue descriptions

For each ticket in the epic, append a reference to the architectural artifacts:

```bash
gh issue comment $issue_number --repo "$owner_repo" --body "## Epic Architecture

This ticket is part of **[Epic Name]**. Before implementing, read:
- [Contracts](../docs/epics/[epic-slug]/contracts.md) — REQUIRES/ENSURES for APIs and entities
- [State Machines](../docs/epics/[epic-slug]/state-machines.md) — valid transitions
- [Invariants](../docs/epics/[epic-slug]/invariants.md) — rules that must hold across all tickets
- [Traceability](../docs/epics/[epic-slug]/traceability.md) — which tests cover which AC

Your implementation must satisfy the contracts and invariants. The /implement skill will enforce this."
```

### Step 9: Report

```markdown
## Architecture Complete: [Epic Name]

### Artifacts committed
- `docs/epics/[slug]/contracts.md` — X entities, Y endpoints
- `docs/epics/[slug]/state-machines.md` — Z state machines
- `docs/epics/[slug]/invariants.md` — N invariants
- `docs/epics/[slug]/adr.md` — M decisions
- `docs/epics/[slug]/integration-tests.md` — P integration tests
- `docs/epics/[slug]/traceability.md` — Q acceptance criteria mapped

### Coverage gaps
- [Any AC without a planned test — these need attention during implementation]

### Next steps
1. `/implement owner/repo#[first-ticket]` — start implementing (will inherit epic context)
2. After all tickets: `/close-epic owner/repo --epic "[Epic Name]"` — run integration tests and mutation testing
```

## Key Principles

- **Contracts are executable, not decorative.** Every REQUIRES/ENSURES block becomes a runtime guard in the implementation. The review checklist verifies this.
- **Invariants span tickets.** An invariant is an epic-level rule that every ticket must respect.
- **The traceability matrix closes the loop.** If an acceptance criterion has no test, it's a gap. `/close-epic` flags these.
- **State machines prevent impossible transitions.** If the state machine says a transition is invalid, the implementation MUST reject it. No exceptions.

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

Launch a **second Opus sub-agent** to reconcile all three consultations into **structured formal models** (JSON) plus supporting prose artifacts. Each artifact is a separate file in `$CW_TMP/`.

**The sub-agent MUST produce structured JSON models first.** The prose markdown is then generated mechanically from the JSON — never the other way around. This ensures the machine-readable and human-readable artifacts stay in sync.

Include the JSON Schema files from `$CW_HOME/templates/formal-models/` in the sub-agent prompt as reference, plus the worked example from `$CW_HOME/docs/formal-methods/examples/order-lifecycle.*.json` as a concrete template to follow.

#### 4a. Data Contracts — structured model (`contracts.json`)

Produce a JSON file conforming to `$CW_HOME/templates/formal-models/contracts-schema.json`.

For every entity and API endpoint the epic touches, define:
- Entity name, description, and canonical fields (type, required, source of truth, immutability, notes)
- Operations with REQUIRES (preconditions), ENSURES (postconditions), ERROR CASES, state transitions touched, invariants touched
- Each condition carries a `description` (human-readable) and an `expression` (machine-checkable pseudo-code)
- Provenance: every element carries `derived_from` linking back to tickets, acceptance criteria, or epic invariants

**Verifier-in-the-loop**: After the sub-agent produces `contracts.json`, validate it:
```bash
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/contracts.json"
```
If validation fails, feed the errors back to the sub-agent and have it fix the JSON. Repeat until valid.

#### 4b. State Machines — structured model (`state-machines.json`)

Produce a JSON file conforming to `$CW_HOME/templates/formal-models/state-machine-schema.json`.

For every state machine in the epic, define:
- All states with type (initial/normal/terminal), description, entry/exit actions
- All valid transitions with events, guards (description + expression), and actions
- All invalid transitions with reasons (these become negative test cases)
- All invariants with ID, description, expression, scope (global or state-specific), and category
- Context fields with types, required-in-states, and source of truth
- Provenance on every element

**Verifier-in-the-loop**: Validate and run graph analysis:
```bash
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/state-machines.json"
python3 "$CW_HOME/scripts/formal_models.py" graph "$CW_TMP/state-machines.json"
```
Check for: unreachable states, dead states (non-terminal with no outgoing transitions), terminal states that aren't reachable. Fix any issues before proceeding.

#### 4c. Generate prose from models

Once the structured JSON models are valid, generate the prose markdown mechanically:

```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view human --output "$CW_TMP/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view human --output "$CW_TMP/"
```

This produces `contracts.md` and `state-machines.md` in the same format as before — the downstream pipeline and human reviewers see identical prose. The difference is that the prose is now a derived artifact, not the source of truth.

**Also generate the machine and test views** for the commit:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view all --output "$CW_TMP/models/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view all --output "$CW_TMP/models/"
```

#### 4d. Invariants (`invariants.md`)

Cross-cutting rules that must hold at ALL times, not just within a single transition. These are ALSO captured in the state machine JSON (in the `invariants` array), but `invariants.md` serves as the consolidated human-readable reference across all models.

Write `invariants.md` by extracting and consolidating all invariants from both `contracts.json` and `state-machines.json`, grouped by category:

```markdown
## Epic Invariants

### Data Integrity
1. **INV-001 — Single source of truth**: Item names are NEVER stored as strings on orders. Always reference item_ids and resolve at read time.
2. **INV-002 — Customer linkage**: Every order with status >= "pending" has a non-null customer_id that references a valid customer document.

### Consistency
3. **INV-005 — Screen agreement**: Dashboard, List View, Calendar, and Detail View MUST derive record counts from the same query/aggregation.

### Operational Safety
4. **INV-007 — No false success**: If an operation depends on an external service, the success toast MUST NOT display unless the service call succeeded.
```

Each invariant MUST have a unique ID (e.g., INV-001) that matches its ID in the JSON models. This enables traceability from prose → model → test → code.

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

### Step 5: Formal model validation + multi-AI review

The synthesised artifacts are the most critical output in the pipeline — every ticket inherits them. Validate mechanically first, then get a second opinion from external AIs.

#### 5a. Mechanical validation

Run structural validation on the formal models:

```bash
# Schema validation
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/contracts.json"
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/state-machines.json"

# Graph analysis — check for structural defects
python3 "$CW_HOME/scripts/formal_models.py" graph "$CW_TMP/state-machines.json"
```

Check the graph analysis output for:
- **Unreachable states**: States that can't be reached from the initial state — these are model bugs
- **Dead states**: Non-terminal states with no outgoing transitions — entities get stuck here
- **Unreachable terminal states**: If a terminal state can't be reached, the happy/sad path is broken
- **Invariant count**: Ensure invariants exist — zero invariants means the model is likely too shallow

If any of these checks fail, fix the JSON models and regenerate prose before proceeding.

#### 5b. Multi-AI validation

Prepare a validation prompt at `$CW_TMP/validate-artifacts-prompt.md` containing:
- The full contents of all artifacts (contracts JSON, state machine JSON, invariants, ADR, integration tests, traceability)
- The graph analysis output from 5a
- The epic goal and ticket list
- Specific questions:
  1. Are there any contracts that contradict each other?
  2. Are there state transitions missing from the state machine? Any unreachable states the graph analysis missed semantically?
  3. Are the invariants complete — could a ticket satisfy all listed invariants and still ship a bug?
  4. Does the integration test suite cover the highest-risk cross-ticket interactions?
  5. Are there acceptance criteria in the traceability matrix that have no planned test?
  6. Does every precondition have a corresponding error case? (REQUIRES without an ERROR CASE means a silent failure)
  7. Are the `expression` fields in preconditions/postconditions/invariants reasonable and implementable?

Run Codex and Gemini in parallel:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" codex $CW_TMP/validate-artifacts-prompt.md -o $CW_TMP/validate-codex.md --cwd "$TARGET_REPO" &
python3 "$CW_HOME/scripts/consult_ai.py" gemini $CW_TMP/validate-artifacts-prompt.md -o $CW_TMP/validate-gemini.md --cwd "$TARGET_REPO" &
wait
```

Review both responses. Apply clear improvements to the JSON models. Regenerate prose if models changed:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view human --output "$CW_TMP/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view human --output "$CW_TMP/"
```

Flag genuine disagreements for the user in Step 6.

### Step 6: Present artifacts to user

**CHECKPOINT**: Show the user a summary of all artifacts:
- **State machines**: Mermaid diagram (from the generated prose), states and transitions, graph analysis results
- **Contracts**: key entities and their REQUIRES/ENSURES
- **Invariants**: the cross-cutting rules with IDs
- **Model health**: graph analysis — any unreachable states, dead states, missing paths
- **Test coverage preview**: number of test paths that will be mechanically generated (from the test view)
- ADR: key decisions with trade-offs
- Integration tests: what will be validated at epic close
- Traceability: coverage gaps (any AC without a planned test)

The Mermaid diagrams and counterexample traces are the primary review artifacts — stakeholders review visuals and scenarios, not JSON.

Ask for feedback. Iterate if needed. If the user requests changes, update the JSON models and regenerate prose:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view human --output "$CW_TMP/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view human --output "$CW_TMP/"
```

This is the most important review in the entire pipeline — getting the contracts wrong here means every ticket inherits the wrong constraints.

### Step 7: Commit artifacts to target repo

Create an `epic/` directory structure in the target repo with a `models/` subdirectory for formal model artifacts:

```bash
mkdir -p "$TARGET_REPO/docs/epics/[epic-slug]/models"
```

Copy prose artifacts (human-readable, backward-compatible):
```bash
cp $CW_TMP/contracts.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/state-machines.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/invariants.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/adr.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/integration-tests.md "$TARGET_REPO/docs/epics/[epic-slug]/"
cp $CW_TMP/traceability.md "$TARGET_REPO/docs/epics/[epic-slug]/"
```

Copy formal model artifacts (machine-readable, consumed by `/implement`):
```bash
cp $CW_TMP/contracts.json "$TARGET_REPO/docs/epics/[epic-slug]/models/"
cp $CW_TMP/state-machines.json "$TARGET_REPO/docs/epics/[epic-slug]/models/"
```

Generate and copy machine + test views:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view machine --output "$TARGET_REPO/docs/epics/[epic-slug]/models/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view machine --output "$TARGET_REPO/docs/epics/[epic-slug]/models/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view test --output "$TARGET_REPO/docs/epics/[epic-slug]/models/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view test --output "$TARGET_REPO/docs/epics/[epic-slug]/models/"
```

Commit and push **directly to the default branch**. Architecture artifacts are docs, not code — they don't need a feature branch or PR. Avoid creating a branch, as it leads to unnecessary stash/cherry-pick detours when returning to the default branch.

```bash
cd "$TARGET_REPO"
git checkout "$DEFAULT_BRANCH"
git pull --ff-only
git add docs/epics/
git commit -m "arch: add epic architecture — [Epic Name]

Contracts, state machines, invariants, ADR, integration test spec,
traceability matrix, and formal models for the [Epic Name] epic.

Formal models include structured JSON (contracts + state machines),
XState machine, Hypothesis test skeleton, and mechanically generated
test paths.

Generated by /architect"
git push
```

**Important**: Ensure you are on `$DEFAULT_BRANCH` before committing. If there are uncommitted changes in the working tree, stash them first, commit the architecture, then pop the stash.

**If `git push` fails** (e.g., branch protection rules forbid direct pushes to the default branch), fall back to creating a PR:
```bash
git checkout -b "arch/$epic_slug"
git push -u origin "arch/$epic_slug"
gh pr create --repo "$owner_repo" --title "arch: add epic architecture — [Epic Name]" --body "Architecture artifacts for the [Epic Name] epic." --label documentation
gh pr merge --squash --auto
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

### Formal models
- `docs/epics/[slug]/models/contracts.json` — structured contracts (machine-readable)
- `docs/epics/[slug]/models/state-machines.json` — structured state machines (machine-readable)
- `docs/epics/[slug]/models/xstate-machine.json` — XState v5 machine (for path generation)
- `docs/epics/[slug]/models/test_state_machine.py` — Hypothesis RuleBasedStateMachine
- `docs/epics/[slug]/models/test-paths.json` — N mechanically generated test paths
- `docs/epics/[slug]/models/test-plan.md` — test plan with positive/negative cases

### Model health
- States: X reachable, Y terminal, Z unreachable
- Transitions: X valid, Y invalid (negative test cases)
- Invariants: N total (X data integrity, Y consistency, Z operational safety)
- Test paths: N (covering X% of states, Y% of transitions)

### Coverage gaps
- [Any AC without a planned test — these need attention during implementation]

### Next steps
1. `/implement owner/repo#[first-ticket]` — start implementing (will inherit epic context + formal models)
2. After all tickets: `/close-epic owner/repo --epic "[Epic Name]"` — run integration tests and mutation testing
```

## Key Principles

- **JSON models are the source of truth, prose is derived.** The structured JSON models (`contracts.json`, `state-machines.json`) are the canonical artifacts. Prose markdown is generated from them by `render_models.py`. If they ever diverge, the JSON wins — regenerate the prose.
- **Contracts are executable, not decorative.** Every REQUIRES/ENSURES block becomes a runtime guard in the implementation. The formal model generates these guards mechanically — the review checklist verifies they're present.
- **Test generation is mechanical, not improvised.** `@xstate/graph` generates path coverage tests from the state machine model. Hypothesis `RuleBasedStateMachine` tests invariants via random exploration. These supplement LLM-written tests, not replace them.
- **Invariants span tickets.** An invariant is an epic-level rule that every ticket must respect. Each invariant has a unique ID for traceability across models, tests, and code.
- **The traceability matrix closes the loop.** If an acceptance criterion has no test, it's a gap. `/close-epic` flags these.
- **State machines prevent impossible transitions.** If the state machine says a transition is invalid, the implementation MUST reject it. The model mechanically generates negative test cases for every invalid transition.
- **Verifier-in-the-loop for LLM-generated models.** LLMs produce formal models; `formal_models.py validate` and `formal_models.py graph` mechanically check them. Never accept an unvalidated model — generate → validate → fix → repeat.

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
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# One tested call resolves CW_HOME, CW_TMP, TARGET_REPO, DEFAULT_BRANCH, EPIC_SLUG, EPIC_DIR.
# Capture first and check status so a resolver failure aborts cleanly.
CW_CTX=$(python3 "$CW_HOME/scripts/workflow_context.py" "$owner_repo" --epic "$epic_name" --shell) || {
  echo "workflow_context failed for $owner_repo" >&2; exit 1; }
eval "$CW_CTX"
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
cat "$TARGET_REPO/docs/domain-context.md" 2>/dev/null
ls "$TARGET_REPO/docs/adr/" 2>/dev/null
```

**New-product check.** DST (deterministic-simulation testing, FoundationDB/TigerBeetle/Antithesis-style) is only cheap if determinism is designed in from day one — retrofitting it into a codebase with wall-clock reads and unseeded randomness scattered through business logic is brutal. This is the one window where it's free, so detect whether this is the epic that first architects the product. The default heuristic: no prior CW epic artifacts AND no existing CW quality state:

```bash
IS_NEW_PRODUCT=$([ ! -d "$TARGET_REPO/docs/epics" ] && [ ! -f "$TARGET_REPO/docs/quality/ratchet.json" ] && echo true || echo false)
```

This is a **default the operator can override**, not a proof — a repo can have real history without either marker (e.g. imported code CW never touched), or be effectively greenfield despite a stray artifact. If the signal looks wrong for this repo, say so at the Step 6 checkpoint and let the user decide. If `IS_NEW_PRODUCT` is `true`, Step 4b's structured model gains DST-readiness invariants (rendered into `invariants.md` via Step 4e) and Step 4f's ADR notes the clock/random/IO seam scaffolding (see below). Skip both for an established repo — retrofitting the invariants onto existing code is a separate, deliberate decision, not something to silently stamp in.

`docs/domain-context.md` (written by `/seed` Step 2.5) is the **ground truth for data contracts**: canonical metric definitions, real schema names, source caveats, and mined use cases — each with citations. If the epic touches an existing data source and this file doesn't exist, run the `/seed` Step 2.5 ingestion now (semantic layer, schema introspection, transformation-repo history) before writing any data contract. Contracts authored from guessed table/column names are how query layers get built against names that don't exist.

**Adopted patterns.** If the product has adopted registry patterns (via `/apply-pattern`), load their invariant clusters now — they are **pre-derived contracts** this epic must not re-invent:

```bash
python3 "$CW_HOME/scripts/apply_pattern.py" --target-dir "$TARGET_REPO" --list-adopted --format json
```

Each adopted pattern reports its `id`, its invariant cluster (stable `INV-<ABBR>-NNN` ids + statements), its `contract_pack` doc (`docs/patterns/<id>/invariants.md`), and any `unresolved` (unbound required) parameters. Note which of these patterns **this epic realizes** — you'll fold their clusters into `invariants.md` (Step 4e) by their existing stable ids, thread their integration tests (Step 4g), and any `unresolved` parameter is a hard blocker to resolve before contracts depend on it (`check_unresolved.py` already gates the stamped `TBD:` markers).

### Step 2: Explore the codebase

Launch an **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) to understand the current state of the areas this epic will touch. *Claude Code adapter:* `subagent_type: "Explore"`, thoroughness "very thorough". The worker should report:

- Current data models (structs, types, schemas) relevant to the epic
- Existing API endpoints that will be extended or consumed
- UI components/screens that will be modified
- Test infrastructure (what frameworks, what coverage exists)
- Existing patterns and conventions in the affected areas

Write findings to `$CW_TMP/codebase-context.md`.

**Hotspot + coupling context (measured, not declared — #187).** Refresh `docs/quality/hotspots.json` so architectural scrutiny concentrates where change-risk is *measured* from git history, not guessed:

```bash
python3 "$CW_HOME/scripts/hotspot_discovery.py" --repo "$TARGET_REPO" \
  --out "$TARGET_REPO/docs/quality/hotspots.json" --format text
```

This is a report-only composer — it degrades gracefully (no lizard, no history, empty repo all produce a well-formed but empty/noted record) and **never gates `/architect`**. Note the top-decile files and their `coupled_with` partners that overlap the epic's touched areas; fold that list into the Step 3 consultation prompt below. `docs/quality/hotspots.json` itself carries no stable IDs and is never referenced by `@cw-trace` — it's a measured, rebuildable artifact, not a contract (INV-fh-007).

### Step 3: Multi-AI architectural consultation

Prepare a consultation prompt at `$CW_TMP/architect-prompt.md` including:
- Epic goal and ticket list with acceptance criteria
- Codebase context from Step 2
- Hotspot + change-coupling context (`docs/quality/hotspots.json`, if present): the top-decile files this epic touches, their churn/complexity scores, and their coupled partners — a measured risk *prior*, not a defect finding, and absence of rank is not evidence of health
- Cross-cutting concerns identified by `/plan-epic`
- Specific questions:
  1. What is the canonical data model for the entities this epic touches? Define the single source of truth.
  2. What state machines exist? Define all valid states and transitions.
  3. What invariants must hold across the full epic? (e.g., "an order always has a customer_id after confirmation")
  4. What are the API contracts — preconditions, postconditions, error cases?
  5. Where are the integration risks and how should we test them? Cross-reference the hotspot/coupling context above — a file this epic touches that's ALSO a measured top-decile hotspot (or tightly coupled to one) deserves deeper contract scrutiny than the hotspot report alone would suggest; the absence of a hotspot entry is not a reason to skip scrutiny elsewhere.
  6. What could go wrong between tickets? (dual sources of truth, race conditions, inconsistent reads)

Fire the `architecture_critic` quorum (codex + gemini in parallel, with retries + output validation). Every provider gets the **identical** prompt above — the value is in natural divergence, not roleplay — but `config/providers.json` may additionally assign each provider a bounded **review lens** (`role.lenses`, e.g. `codex: refute-soundness`, `gemini: completeness`, `claude-interactive: adoption-cost`; charters live in `config/lenses.json`). When a lens is assigned, `consult_ai.py` appends that provider's charter as a clearly-delimited `## Your charter` section — the shared prompt itself never changes. This decorrelates the reviewers on purpose: a soundness-refuter and a completeness-checker reading the *same* context reliably surface disjoint findings instead of converging on the same top issue three times:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role architecture_critic $CW_TMP/architect-prompt.md \
  --output-dir "$CW_TMP/architect-consult" --cwd "$TARGET_REPO"
```

Responses land at `$CW_TMP/architect-consult/architecture_critic-<provider>.md` with status in `architecture_critic-manifest.json`. Launch an **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) in parallel to explore the codebase and produce its own architectural analysis at `$CW_TMP/architect-opus.md`. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`.

**HARD RULE**: Wait for ALL THREE before proceeding.

### Step 4: Synthesise into architectural artifacts

Launch a **synthesis worker** (contract: `docs/worker-contracts.md#synthesis-worker`) to reconcile all three consultations into **structured formal models** (JSON) plus supporting prose artifacts. Each artifact is a separate file in `$CW_TMP/`.

**Reconciliation expects disjoint findings, not convergence.** When the quorum is lensed (Step 3), agreement across providers is the exception, not the confirmation signal — each reviewer was scoped to look for something different. Reconcile by **union, then cross-verify contested items**: fold in every finding from every provider (a soundness issue the refuter caught is not weaker for being unique to it), and only for items where providers actively *disagree* on a fact (not merely "only one mentioned it") should the synthesis worker re-check against the codebase before deciding which side is right. Do not discard a lensed provider's finding for lacking a second vote — that would defeat the reason lenses were assigned.

**The worker MUST produce structured JSON models first.** The prose markdown is then generated mechanically from the JSON — never the other way around. This ensures the machine-readable and human-readable artifacts stay in sync.

Include the JSON Schema files from `$CW_HOME/templates/formal-models/` in the worker prompt as reference, plus the worked example from `$CW_HOME/docs/formal-methods/examples/order-lifecycle.*.json` as a concrete template to follow.

#### 4a. Data Contracts — structured model (`contracts.json`)

Produce a JSON file conforming to `$CW_HOME/templates/formal-models/contracts-schema.json`.

For every entity and API endpoint the epic touches, define:
- Entity name, description, and canonical fields (type, required, source of truth, immutability, notes)
- Operations with REQUIRES (preconditions), ENSURES (postconditions), ERROR CASES, state transitions touched, invariants touched
- Each condition carries a `description` (human-readable) and an `expression` (machine-checkable pseudo-code)
- Provenance: every element carries `derived_from` linking back to tickets, acceptance criteria, or epic invariants

**Ground every data fact in a real source.** Field names, metric definitions, and external identifiers must come from `docs/domain-context.md` (or direct introspection) — cite the source in `notes`. Where a fact genuinely cannot be confirmed yet (external system unavailable, awaiting client answer), write an explicit `TBD:` marker in the field — e.g. `"notes": "TBD: confirm column name against dbt model orders_daily"`. Markers are machine-detected and **gate dependent tickets** in `/implement-wave`; a silent guess does not.

**Verifier-in-the-loop**: After the worker produces `contracts.json`, validate it:
```bash
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/contracts.json"
```
If validation fails, feed the errors back to the worker and have it fix the JSON. Repeat until valid.

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

#### 4d. UI Specification — structured model (`ui-spec.json`)

Produce a JSON file conforming to `$CW_HOME/templates/formal-models/ui-spec-schema.json`.

This is the **UI contract** that prevents workers from making conflicting design decisions. Without it, one agent builds a sidebar panel, another builds a separate page, and a third uses a dropdown — for the same feature.

The UI spec defines:

1. **Page inventory**: What routes/pages exist, what layout each uses (sidebar-main, centered, canvas, etc.)
2. **Component tree per page**: Flat map of component ID → spec. Each component has a type, children (by ID), data bindings, and interactions.
3. **Shared patterns**: Reusable UI patterns defined once and referenced by ID. Examples:
   - `entity-menu`: "3-dot icon → dropdown with edit/archive/delete"
   - `inline-edit`: "click text → input with blue ring, Enter saves, Escape cancels"
   - `sidebar-panel`: "slides in from right, 320px, full-screen on mobile"
   - `confirm-dialog`: "browser confirm() for destructive actions"
4. **Interaction contracts**: For each interactive component, what happens when the user does something: `{ trigger: "click", action: "open-sidebar", target: "settings-panel" }`. This is what prevents "is this a dropdown or a tab or a modal?" confusion.
5. **Navigation graph**: XState-format state machine where pages are states and links are transitions. Reuses the same format as data state machines.
6. **Visual design contract** (`design` section): the part that makes "on-brand" checkable. A frontend can satisfy every interaction contract and still ship generic and ugly — 121 passing tests prove nothing about how it looks.

   **If `docs/design/design.json` exists in the target repo** (produced by `/design`), fold it in verbatim — it is already in this section's exact format, mechanically extracted from the mockups the human approved. Do NOT re-author or "improve" its tokens from prose; the contract must not drift from the approved design. Register the screenshots in `docs/design/reference/` as `reference-screenshot` assets and bind each epic page to its screenshot via `design_refs`. Add only what's epic-specific (e.g. an asset for a new page, validated against the existing tokens).

   **If no `docs/design/` exists** and the epic has frontend tickets, recommend running `/design` first — a brainstormed token list is a weaker contract than an approved rendered design. If the user declines, author the section from the seed decisions:
   - `source`: where the design comes from — the design system / reference product / brand kit discovered by `/seed` Step 3, with references (URLs, repo paths, reference screenshots). Only `net-new` if `/seed` confirmed nothing exists to match.
   - `tokens`: concrete values — colors (primary required; include brand gradients), typography (fonts + scale), spacing, radii, shadows. Derived from the seed design source when present; deliberate choices (with rationale in the ADR) when net-new. **Never leave a component library's default theme as the implicit answer.**
   - `component_library`: which library and whether to `adopt`, `extend`, or go `custom` — workers must not hand-roll components when the contract says adopt.
   - `assets`: logo/wordmark/illustrations/reference screenshots, each with `applies_to` pages. Per-page `design_refs` link pages to the reference screenshots they must visually match.
   - `voice`: tone and empty-state guidelines, so empty states get personality instead of "No data".

   The design contract is consumed by the design-fidelity gate in `/implement` Step 9 — screenshots of the rendered app are reviewed against these tokens and references, and a frontend that ignores them fails the ticket.

**Critical constraint**: Every component that could be implemented as multiple UI patterns (dropdown vs tab vs modal vs page) MUST have an explicit interaction contract specifying which pattern to use.

Include the worked example from `$CW_HOME/docs/formal-methods/examples/kanban-app-ui-spec.json` in the worker prompt as a template.

**Verifier-in-the-loop**: After the worker produces `ui-spec.json`, validate it:
```bash
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/ui-spec.json"
```

**Generate human-readable UI spec**: Render the UI spec to a page-by-page markdown doc:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/ui-spec.json" --view human --output "$CW_TMP/"
```

#### 4e. Invariants (`invariants.md`)

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

**Fold in adopted-pattern clusters (do not re-derive).** For each adopted pattern this epic realizes (from Step 1's `--list-adopted`), add its invariant cluster under a dedicated subsection, **keeping the pattern's own stable ids verbatim** (`INV-<ABBR>-NNN` — e.g. `INV-FOWR-004`, `INV-EAS-003`). These are the pattern's contract; re-numbering them or paraphrasing away their meaning breaks the link back to the contract pack and the ratchet. Cite the source so the provenance is legible:

```markdown
### Adopted pattern: fetch-on-webhook-reconcile
*(installed by /apply-pattern — cluster from docs/patterns/fetch-on-webhook-reconcile/invariants.md)*
- **INV-FOWR-001** — Trigger-only: the webhook handler never reads mutable state from the payload; state comes from a live fetch.
- **INV-FOWR-004** — Unknown external id is fatal: no write, no floor fallback, alert.
```

The connected requirements arrive as a set — adopt the **whole** cluster for a realized pattern, not a subset. Any `unresolved` parameter reported for that pattern must be resolved (or explicitly re-marked `TBD:`) before an invariant depends on it.

**DST-readiness invariants (new products only — `IS_NEW_PRODUCT=true` from Step 1).** Stamp a small, fixed cluster of determinism-readiness invariants so the seams exist from ticket one instead of getting retrofitted later. **Structured model first**: these go into `state-machines.json`'s top-level `invariants` array as schema-valid invariant objects (Step 4b), and the prose in `invariants.md` is then consolidated from the models like every other invariant — never authored verbatim into the prose alone (JSON is the source of truth; prose is derived):

```json
{
  "id": "INV-dst-001",
  "description": "No wall-clock reads outside the clock seam: production code reads the current time only through the designated clock module (e.g. internal/clock), never time.Now() / datetime.now() / Date.now() directly.",
  "expression": "forall call in production_code: is_wall_clock_read(call) => module(call) == clock_seam",
  "scope": "global",
  "category": "operational_safety"
},
{
  "id": "INV-dst-002",
  "description": "No unseeded randomness outside the random seam: production code draws randomness only through a designated, seedable source (e.g. internal/rand), never default math/rand package functions / Python random module / Math.random() directly.",
  "expression": "forall call in production_code: is_default_source_random(call) => module(call) == rand_seam",
  "scope": "global",
  "category": "operational_safety"
},
{
  "id": "INV-dst-003",
  "description": "IO behind seam interfaces: network/filesystem/DB calls are made only from designated IO packages, behind an interface a test can substitute. Checked by code review, not check_dst_readiness.py — grepping 'any IO outside package X' is too noisy for a v1 regex tier.",
  "expression": "forall call in production_code: is_io(call) => package(call) in io_seam_packages",
  "scope": "global",
  "category": "operational_safety"
}
```

After `render_models.py` regenerates the prose, verify the three IDs appear in `invariants.md` under their own subsection (e.g. "Determinism & Simulation Readiness"). These are advisory, not blocking: `scripts/check_dst_readiness.py` mechanically checks INV-dst-001/002 (report-only by default, FOREVER — this is design-readiness signal, not enforcement) and is run in Step 5a. INV-dst-003 has no mechanical check yet; it's scaffolding for code review to hold workers to. Keep the IDs stable so downstream `/implement` tickets and `check_dst_readiness.py`'s `**/clock*` / `**/rand*` / `**/telemetry/**` seam allowlist line up with real packages.

#### 4f. ADR (`adr.md`)

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

**New products (`IS_NEW_PRODUCT=true`) add a decision entry for the DST seams**, naming the actual packages this codebase will use — not a hypothetical:

```markdown
### N. Determinism & simulation readiness (clock/random/IO seams)
- **Decision**: All wall-clock reads go through `internal/clock` (a single `Now() time.Time`
  seam); all randomness goes through `internal/rand` (seeded, injectable); network/DB/FS
  calls are made behind interfaces owned by the designated IO packages listed here: [...].
- **Alternatives considered**: Leaving `time.Now()`/`rand.Intn()` inline — rejected because
  retrofitting determinism later means touching every call site instead of one seam.
- **AI consensus**: [Where the three AIs agreed/diverged]
- **Trade-offs**: One extra layer of indirection for time/randomness/IO from ticket one, in
  exchange for this codebase staying simulation-testable (fast, reproducible, shrinkable bug
  repros) instead of needing a painful determinism retrofit later.
```

This is the scaffolding note that makes INV-dst-001/002/003 (Step 4e) actionable — the
invariant says "no wall-clock reads outside the clock seam"; this decision says which
package IS the clock seam, so `/implement` workers build against it from ticket one instead
of learning about it after the fact.

#### 4g. Integration Test Specification (`integration-tests.md`)

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

**Adopted-pattern tests.** For each adopted pattern this epic realizes, thread the executable proof its cluster implies — e.g. `multi-tenant-isolation` contributes a standing cross-tenant isolation test (denial AND no-mutation across vectors); `fetch-on-webhook-reconcile` contributes an out-of-order/stale-event convergence test; `elevated-access-session` contributes a TTL-expiry + revocation + deny-list test. Reference the folded `INV-<ABBR>-NNN` ids in the **Why** so `/close-epic` can confirm the pattern's cluster held.

#### 4h. Requirements Traceability Matrix (`traceability.md`)

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

#### 4i. System Architecture Model (`docs/system/architecture.json`) — once per product

Unlike 4a–4h, this artifact is **system-level, not epic-level**: it lives at `docs/system/architecture.json` in the target repo (schema: `$CW_HOME/templates/formal-models/architecture-schema.json`) and is authored **once per product**, then updated only when an epic adds/removes/retires a deployable or connector — most epics touching an existing product will find it already present and just extend it.

**Check first**: `test -f "$TARGET_REPO/docs/system/architecture.json"`.
- **Absent** (first epic that touches system architecture, or a product that has never modeled it): author it fresh. Include `$CW_HOME/templates/formal-models/examples/voice-agent-architecture.json` in the worker prompt as a worked example (client → gateway → STT/LLM/TTS externals with `ASM-` refs) — a concrete template for the node/edge shape, not a fixture to copy verbatim.
- **Present**: read it, and only add/amend the `ARC-`/`EDG-` nodes and edges THIS epic actually introduces (a new service, a new external vendor call, a connector being retired). Leave everything else untouched — this file is a standing product model, not a per-epic scratchpad.

For every deployable and connector this epic's tickets introduce or change, define:
- **Nodes** (`ARC-`): `id`, `name`, `kind` (`service`|`worker`|`db`|`queue`|`bucket`|`cron`|`external`), `repo` (URN/path, or `null` for external), `external`, `trust_zone` (`public`|`dmz`|`internal`|`restricted`), `region`, `failure_domain`, `criticality_tier` (`tier-1`|`tier-2`|`tier-3` — **never omit this**: a missing tier is reported as a finding by the checker, not silently skipped, so leaving it out just defers the problem), `emits` (telemetry binding names — bind these to the same names used in `docs/system/system-contracts.json` budget nodes' `telemetry_ref`, if that artifact exists, so the cross-artifact check in 5a has something to verify), `status` (`active`|`deprecated`|`retired`), `asm_refs` (REQUIRED, non-empty, when `external: true` and the node is reached by a `hard` edge — vendor SLA/assumption references, `ASM-` ids).
- **Edges** (`EDG-`): `from`/`to` (must resolve to declared `ARC-` node ids), `protocol`, `mode` (`sync`|`async`), `criticality` (`hard`|`soft` — an AVAILABILITY dependency only, distinct from `carries` and from a trust-zone crossing: a low-tier logging sink may carry sensitive data without being availability-critical, and a low-tier auth provider may be availability-critical without carrying any payload), `on_failure` (`fallback`/`degrade_to`), `carries` (data-class labels, the monotone lattice `public < internal < pii < secret < official-sensitive`), `auth`, `timeout_ms`, `ordering`, `dlq`, `active`. **Never author `trust_zone_crossing`/`region_crossing`** — those two fields are computed ONLY by `check_architecture.py` from the node attributes; leave them `null` (or omit them) and let the checker derive them. An authored non-null value is itself a finding (INV-fh-006) — a hand-written "safe" crossing label is exactly the kind of thing that could mask a real trust-zone violation.

**Verifier-in-the-loop**: validate against the schema as soon as the worker produces it:
```bash
python3 "$CW_HOME/scripts/formal_models.py" validate "$CW_TMP/architecture.json" --type architecture
```
Fix and re-validate until clean. The deeper consistency checks (dangling endpoints, retired-node edges, unlabelled externals, tier inversion, label propagation, cross-artifact drift) run in Step 5a below — schema validity here is necessary but not sufficient.

**What this proves, and what it doesn't.** `check_architecture.py` proves the DECLARED model in `architecture.json` is internally consistent. It does **not** prove the running code matches this model — that conformance/reflexion question is deliberately deferred (`#171`; see `docs/system-layer.md`). Declaring the model is cheap; treat it as a design-time contract the epic's `contracts.md`/`ui-spec.json` may reference by `ARC-`/`EDG-` id, not as a guarantee about deployed reality.

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

# Unresolved-unknowns scan — TBD/UNRESOLVED/PLACEHOLDER markers across all artifacts
python3 "$CW_HOME/scripts/check_unresolved.py" "$CW_TMP" --format text

# Traceability soundness gate — orphan business rules + dangling/invalid links in
# the contract/invariant graph (see docs/traceability.md). Code/test coverage is
# checked later by /close-epic; at architect time this validates the doc-level
# graph. Assign stable BR-/CTR-/INV- IDs and `@cw-trace realizes` links so this
# passes; it degrades gracefully when no IDs exist yet. (Suspect-link propagation
# and `--write-links` are a /close-epic concern — no code/test links exist to
# record yet at this stage.)
python3 "$CW_HOME/scripts/check_traceability.py" "$CW_TMP" --gate soundness --format text

# Single-writer soundness gate — validates that "single write path"/"single source
# of truth" invariants carry well-formed `controls_field` + `sanctioned_writers`
# metadata, and (with --source) surfaces every existing writer of the controlled
# fields so a second, unsanctioned mutator is visible at design time (see
# docs/single-writer.md). Degrades gracefully when no such invariant exists.
python3 "$CW_HOME/scripts/check_single_writer.py" "$CW_TMP" --source "$TARGET_REPO" --gate soundness --format text
```

**Architecture model consistency (`docs/system/architecture.json`, when 4i produced or updated one).** Report-only — this checker is NOT gated in `/architect` yet (ADR-fh-07: gating requires the #168 gate-validation protocol plus a passing `#184` validation record for `check_architecture`, which freezes its `CHECKS` inventory as a prerequisite). Run it so findings are visible at design time even though they don't block:

```bash
python3 "$CW_HOME/scripts/check_architecture.py" "$CW_TMP/architecture.json" \
  --system-contracts "$TARGET_REPO/docs/system/system-contracts.json" --format text
```

(Omit `--system-contracts` if the target repo has no budget-tree doc yet — the checker reports that cross-artifact leg as "not checked", distinct from "passed", never silently skipped.) The authority line is printed every run: *"proves the DECLARED model is internally consistent; does not prove the code matches the model."* Fix any dangling endpoints, retired-node edges, unlabelled externals, tier inversions, label-propagation violations, or authored (non-null) crossing labels before proceeding — these are cheap to fix now and expensive to discover once tickets have started referencing the wrong `ARC-`/`EDG-` ids.

Any invariant that says "single write path" or "single source of truth" for a field/state MUST name its `controls_field` and `sanctioned_writers` — either as arrays on the structured `state-machines.json` invariant, or via a `<!-- @cw-writes INV-... controls_field=a.b sanctioned_writers=Sym,path.go -->` tag next to its `**INV-...**` label in `invariants.md`. Without this metadata the invariant is prose only and a second mutator (e.g. a legacy admin control) can silently violate it. Review the surfaced writer inventory: if the gate lists a writer outside the sanctioned set, that is a pre-existing violation to fix or explicitly sanction before the epic builds on the invariant.

**DST-readiness baseline (new products only — `IS_NEW_PRODUCT=true`).** Run the scanner report-only against the target repo to document the current wall-clock/random-call baseline — this NEVER blocks (advisory forever, per the design decision behind it):

```bash
python3 "$CW_HOME/scripts/check_dst_readiness.py" "$TARGET_REPO" --format text
```

Note the finding counts in Step 6's summary (a fresh repo should have ~zero; a real baseline scan may surface some pre-existing calls worth folding into the seam allowlist or fixing before the first ticket lands). This never gates — `--gate` exists in the script for a repo that later opts in via its own ratchet config, but `/architect` never passes it.

Check the graph analysis output for:
- **Unreachable states**: States that can't be reached from the initial state — these are model bugs
- **Dead states**: Non-terminal states with no outgoing transitions — entities get stuck here
- **Unreachable terminal states**: If a terminal state can't be reached, the happy/sad path is broken
- **Invariant count**: Ensure invariants exist — zero invariants means the model is likely too shallow

If any of these checks fail, fix the JSON models and regenerate prose before proceeding.

For the unresolved scan: each finding is either **resolvable now** (go confirm it — introspect the schema, read the transformation repo, ask the user) or **genuinely external** (keep the marker). Resolve everything resolvable before proceeding. Surviving markers are not errors, but they MUST be surfaced in Step 6 and Step 9, and they will gate dependent tickets in `/implement-wave` — never silently delete a marker without actually resolving the fact behind it.

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

Run the `reviewer` quorum (codex + gemini in parallel, with retries + output validation). As in Step 3, `reviewer` may carry a lens assignment in `config/providers.json` — check its `lenses` map before assuming two providers scoped alike:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role reviewer $CW_TMP/validate-artifacts-prompt.md \
  --output-dir "$CW_TMP/validate-artifacts" --cwd "$TARGET_REPO"
```

Responses land at `$CW_TMP/validate-artifacts/reviewer-<provider>.md` (status in `reviewer-manifest.json`).

Review both responses. **Union the findings rather than looking for agreement** — a lensed quorum is expected to produce disjoint findings; only cross-verify against the models when two providers make contested/contradictory claims about the same fact. Apply clear improvements to the JSON models. Regenerate prose if models changed:
```bash
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/contracts.json" --view human --output "$CW_TMP/"
python3 "$CW_HOME/scripts/render_models.py" "$CW_TMP/state-machines.json" --view human --output "$CW_TMP/"
```

Flag genuine disagreements for the user in Step 6.

**Record validation telemetry.** The review's cost already flows (its reviewer consults + the worker tokens); record its *value* — emit one gate event with the count of substantive findings the multi-AI validation raised (contradictions, missing transitions, incomplete invariants, uncovered ACs — exclude nits). No-op unless telemetry is enabled, never blocks:

```bash
python3 "$CW_HOME/scripts/factory_log.py" emit --event gate --name architect-review \
  --result "$([ "$n_findings" -gt 0 ] && echo fail || echo pass)" --caught "$n_findings" --repo "$owner_repo"
```

(Convention: `docs/factory-telemetry.md` → "LLM validations report their value". The deterministic soundness gates in 5a — traceability/single-writer — already emit their own.)

### Step 6: Present artifacts to user

**CHECKPOINT**: Show the user a summary of all artifacts:
- **State machines**: Mermaid diagram (from the generated prose), states and transitions, graph analysis results
- **Contracts**: key entities and their REQUIRES/ENSURES
- **Invariants**: the cross-cutting rules with IDs
- **Model health**: graph analysis — any unreachable states, dead states, missing paths
- **Open unknowns**: every surviving `TBD:`/`UNRESOLVED:` marker, which tickets each one blocks, and the plan to resolve it (who/what/when)
- **Test coverage preview**: number of test paths that will be mechanically generated (from the test view)
- **DST readiness** (new products only): the INV-dst-001/002/003 invariants stamped, the clock/random/IO seam packages named in the ADR, and `check_dst_readiness.py`'s baseline finding counts
- **System architecture model** (when 4i produced/updated one): node/edge counts, any `check_architecture.py` findings (report-only — never blocks here), and which `ARC-`/`EDG-` ids this epic's contracts reference
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

Install the artifacts with the tested helper. It validates the required artifacts are present, creates `docs/epics/<slug>/models/`, copies the prose + JSON (UI spec optional), initializes the transition-map baseline, generates the machine + test views, prepares the issue-comment body, and commits — **refusing to commit on a dirty target repo unless `--allow-dirty`**:

```bash
cd "$TARGET_REPO"
git checkout "$DEFAULT_BRANCH" && git pull --ff-only
python3 "$CW_HOME/scripts/install_epic_artifacts.py" \
  --source "$CW_TMP" --epic-dir "$EPIC_DIR" \
  --epic-name "$epic_name" --epic-slug "$EPIC_SLUG" --target-repo "$TARGET_REPO"
git push
```

The transition-map baseline marks existing-code transitions `covered` and new model transitions `planned`; it evolves as tickets are implemented. Use `--dry-run` to preview what would be installed (and the issue-comment body) without writing, and `--no-commit` to install without committing. The JSON output lists `copied`, `generated`, `transition_map`, `issue_comment`, and any `warnings`.

**If Step 4i produced or updated `architecture.json`**, commit it separately to `docs/system/` — it is a system-level, once-per-product artifact and does NOT belong under `docs/epics/$EPIC_SLUG/`:
```bash
mkdir -p "$TARGET_REPO/docs/system"
cp "$CW_TMP/architecture.json" "$TARGET_REPO/docs/system/architecture.json"
git -C "$TARGET_REPO" add docs/system/architecture.json
git -C "$TARGET_REPO" commit -m "arch: update docs/system/architecture.json for $EPIC_SLUG"
```
(This can be folded into the same commit/push as the epic artifacts above if the installer step hasn't pushed yet.)

**If `git push` fails** (e.g., branch protection rules forbid direct pushes to the default branch), fall back to creating a PR:
```bash
git checkout -b "arch/$epic_slug"
git push -u origin "arch/$epic_slug"
gh pr create --repo "$owner_repo" --title "arch: add epic architecture — [Epic Name]" --body "Architecture artifacts for the [Epic Name] epic." --label documentation
gh pr merge --squash --auto
```

**Ratchet baseline** (see `docs/ratchet.md`): once the artifacts are on the default branch, enter the approved contract definitions into the quality ratchet's high-water mark so implementation workers can't weaken them silently. If the target repo has no ratchet config yet, initialize it first:

```bash
python3 "$CW_HOME/scripts/ratchet.py" init --repo "$TARGET_REPO"   # no-op if config exists
python3 "$CW_HOME/scripts/ratchet.py" score --repo "$TARGET_REPO" --no-tests
python3 "$CW_HOME/scripts/ratchet.py" record --repo "$TARGET_REPO" \
  --event baseline --ref "$EPIC_SLUG" --merged --notes "epic architecture approved"
git -C "$TARGET_REPO" add docs/quality && git -C "$TARGET_REPO" commit -m "chore: ratchet baseline for $EPIC_SLUG" && git -C "$TARGET_REPO" push
```

If the user later revises a contract (a legitimate re-architecture, not a workaround), the revision is journaled at `/close-epic` with `--amend`/`--retire` — never by silently editing the docs.

### Step 8: Update issue descriptions

For each ticket in the epic, append a reference to the architectural artifacts. `install_epic_artifacts.py` (Step 7) already emits a ready-to-post `issue_comment` body in its JSON output — reuse it (it links every artifact, including the UI spec when present) rather than hand-writing the body:

```bash
# Reuse the installer's issue_comment, or post the equivalent below:
gh issue comment $issue_number --repo "$owner_repo" --body "## Epic Architecture

This ticket is part of **[Epic Name]**. Before implementing, read:
- [Contracts](../docs/epics/$EPIC_SLUG/contracts.md) — REQUIRES/ENSURES for APIs and entities
- [State Machines](../docs/epics/$EPIC_SLUG/state-machines.md) — valid transitions
- [Invariants](../docs/epics/$EPIC_SLUG/invariants.md) — rules that must hold across all tickets
- [Traceability](../docs/epics/$EPIC_SLUG/traceability.md) — which tests cover which AC

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
- `docs/epics/[slug]/models/transition-map.json` — transition ↔ ticket mapping (baseline)
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

### DST readiness (new products only)
- Invariants stamped: INV-dst-001 (clock seam), INV-dst-002 (random seam), INV-dst-003 (IO seam)
- Seam packages named in the ADR: [e.g. `internal/clock`, `internal/rand`]
- `check_dst_readiness.py` baseline: N findings (report-only — advisory forever, not a blocker)
- [Omit this section for a repo that already has prior epics]

### Open unknowns (blockers)
- [Each surviving TBD/UNRESOLVED marker: what it is, which tickets it blocks, how to resolve it]
- [Omit this section only if `check_unresolved.py` reports zero findings]

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

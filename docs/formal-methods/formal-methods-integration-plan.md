# Duplicat-Rex × Formal Methods: Integration Plan

## The Core Idea

Your existing pipeline is: **Recon → Spec → Test → Build → Compare → Gap → Loop**

The formal methods upgrade turns the middle of that pipeline from "LLM-synthesized prose specs" into "executable formal models that mechanically generate tests and constrain code generation." The key seL4 insight applies directly: **an executable specification in a practical language acts as both a testable prototype and the basis for formal reasoning.**

Your `Fact → Hypothesis → Spec → Test → Gap` lifecycle already has the right shape. What formal methods adds is mathematical rigor to each transition, and mechanical (not LLM-hallucinated) test generation.

---

## What Changes and What Doesn't

**Stays the same:**
- Multi-source recon with authority ranking (this is gold, don't touch it)
- The convergence loop structure
- Chief-wiggum as the build engine
- Multi-model adjudication for intelligence synthesis
- The Fact provenance model (source, authority, confidence, freshness, status)

**Changes:**
- Specs gain a formal layer between "typed JSON" and "code"
- Test generation becomes mechanical, not LLM-improvised
- Conformance checking gets a mathematical definition (ioco)
- The gap analyzer can precisely classify *what kind* of gap it found
- Code generation is constrained by the formal model, not just guided by prose

---

## The Three-Layer Model (Adapted from seL4)

seL4 used: Abstract Spec → Executable Spec (Haskell) → C Implementation

Duplicat-rex should use:

### Layer 1: Abstract Behavioral Model (XState Statecharts)
**What it captures:** Observable UI workflows, page transitions, CRUD lifecycles, auth flows, drag-drop sequences — everything your browser recon can directly see.

**Why XState:** It's JSON/TypeScript, LLMs generate it extremely well, it's directly executable (the spec *is* the code for UI state management), and `@xstate/graph` mechanically generates all reachable paths for test coverage. Your Playwright recon already captures exactly the kind of state-transition data XState consumes.

**Concretely for Trello:**
- Board lifecycle: `created → active → starred/unstarred → archived → closed → deleted`
- Card lifecycle: `created → moved → labeled → assigned → archived → deleted`
- Auth flow: `anonymous → login_form → authenticating → authenticated → session_expired`
- Drag-drop: `idle → dragging → over_valid_target → dropped → reordering → settled`

**How it connects to your existing models:** Each XState machine is *derived from* verified Facts. The machine definition carries provenance back to the Facts that justified each state and transition. A Fact with status=CONTRADICTED triggers re-evaluation of any machine that depends on it.

### Layer 2: Behavioral Invariants (Quint/TLA+)
**What it captures:** Properties that span multiple workflows — things XState can't express. Temporal ordering, concurrency constraints, data consistency guarantees.

**Why Quint:** TypeScript-like syntax (LLM-friendly), has a dedicated LLM Kit, checks via Apalache (which now has a JSON-RPC API an agent can drive programmatically). You don't model the *whole system* in Quint — only the critical invariants.

**Concretely for Trello:**
- "A card that has been moved between lists always appears in exactly one list" (no duplication, no loss)
- "If user A moves a card while user B is viewing the board, B eventually sees the update" (real-time consistency)
- "Deleting a board eventually cascades to all its lists and cards" (cleanup completeness)
- "A user cannot access a board they haven't been invited to" (authorization)

**How many specs?** Maybe 10-20 Quint invariants for a Trello-class app. Not hundreds. These are the properties where bugs would be most damaging and hardest to catch with example-based tests.

### Layer 3: Structural Constraints (Alloy or JSON Schema+)
**What it captures:** Entity relationships, data schema invariants, referential integrity — the shape of the data at rest.

**Why this matters:** Your existing `spec-schema.json` already does some of this. The upgrade is making it checkable: "every card references a valid list," "every list references a valid board," "labels are scoped to boards, not global." Alloy can find counterexamples (test data that would violate constraints) automatically.

**Pragmatic alternative:** For v1, you might get 80% of the value by enriching your JSON Schema with `deal`/`icontract` Design-by-Contract decorators on the generated Python code, rather than jumping to Alloy. This is the lightweight formal methods path.

---

## The Upgraded Pipeline

```
Phase 1: RECON (unchanged, but with alphabet extraction)
  │
  │  Browser automation, API docs, videos, community...
  │  NEW: LLM extracts "alphabet" — the set of abstract actions
  │       (create_board, move_card, invite_member, etc.)
  │       This is the input alphabet for automata learning.
  │
  ▼
Phase 2: MODEL CONSTRUCTION (new phase, replaces pure spec synthesis)
  │
  │  2a. Passive automata learning (AALpy RPNI)
  │      Input: interaction traces from recon
  │      Output: initial Mealy machine models
  │
  │  2b. LLM generates XState machines from Facts + traces
  │      Grammar-constrained generation (valid XState JSON only)
  │      Cross-validated against RPNI output
  │
  │  2c. LLM generates Quint invariants from verified Facts
  │      Apalache model-checks each invariant (feedback loop)
  │      ~10-20 critical properties, not exhaustive
  │
  │  2d. Active automata learning (AALpy L*/KV)
  │      Refines passive models by querying live system
  │      Uses browser automation as the "teacher"
  │
  │  2e. Structural constraints
  │      Enrich JSON Schema from API response analysis
  │      Daikon-style invariant detection on response data
  │      Design-by-Contract decorators on generated code
  │
  ▼
Phase 3: MECHANICAL TEST GENERATION (replaces LLM-improvised tests)
  │
  │  3a. XState → path coverage tests via @xstate/graph
  │      Every reachable state, every transition exercised
  │      Automatically generates Playwright test scripts
  │
  │  3b. Quint invariants → property-based tests
  │      Hypothesis RuleBasedStateMachine from Mealy machines
  │      Schemathesis for API conformance from OpenAPI spec
  │
  │  3c. Dual-execution test harness (unchanged concept)
  │      Same tests run against target AND clone
  │      But now tests are mechanically derived, not hand-crafted
  │
  ▼
Phase 4: BUILD (via chief-wiggum, with formal constraints)
  │
  │  XState machines → executable TypeScript (spec IS code)
  │  OpenAPI spec → server stubs via openapi-generator
  │  Business logic constrained by Design-by-Contract decorators
  │  LLM generates implementation within formal guardrails
  │
  ▼
Phase 5: CONFORMANCE CHECKING (upgraded compare)
  │
  │  5a. Differential testing (unchanged — strongest oracle)
  │      Same inputs → compare outputs, original vs clone
  │
  │  5b. Model conformance (new — ioco-style)
  │      Does the clone's behavior conform to the formal model?
  │      Counterexamples from model checker = specific failing scenarios
  │
  │  5c. Property verification (new)
  │      Do Quint invariants hold on the clone?
  │      Do Design-by-Contract postconditions pass?
  │
  ▼
Phase 6: GAP ANALYSIS (upgraded — formally classified gaps)
  │
  │  Gap types now have formal categories:
  │  - STATE GAP: clone is missing a state or transition
  │  - INVARIANT VIOLATION: clone breaks a temporal property
  │  - SCHEMA VIOLATION: clone's data shape is wrong
  │  - BEHAVIORAL DIVERGENCE: clone produces different output
  │  - COVERAGE GAP: model has paths the clone doesn't exercise
  │
  │  Each gap traces back to specific model elements,
  │  which trace back to specific Facts with provenance.
  │
  └──→ Loop back to Phase 4 (or Phase 2 if model needs updating)
```

---

## Implementation Roadmap

### Wave 1: Foundation (est. 1-2 weeks)
**Goal:** Get XState model generation working end-to-end for one Trello workflow.

1. Add AALpy as a dependency (`pip install aalpy`)
2. Build an alphabet extractor: LLM takes recon Facts and outputs a list of abstract actions + observations
3. Implement trace capture during browser recon (action/response sequences in AALpy's format)
4. Run RPNI on captured traces → get initial Mealy machine
5. Build an LLM prompt that takes Facts + Mealy machine → XState JSON definition
6. Validate: can `@xstate/graph` walk the machine and produce test paths?

**Deliverable:** For the Trello board CRUD workflow, an XState machine derived from recon that generates 15-30 test paths automatically.

### Wave 2: Test Generation (est. 1-2 weeks)
**Goal:** Mechanical test generation from formal models replaces hand-crafted tests.

1. XState test paths → Playwright E2E test scripts (template-based generation)
2. Schemathesis integration: feed it the inferred OpenAPI spec, get API tests for free
3. Hypothesis `RuleBasedStateMachine`: encode the Mealy machine as rules, get stateful property tests
4. Dual-execution harness: run generated tests against both Trello and clone
5. Wire test results back into the gap analyzer with formal gap classification

**Deliverable:** A test suite that's 80%+ mechanically generated, runs against both target and clone, and classifies failures by gap type.

### Wave 3: Invariants and Constraints (est. 2-3 weeks)
**Goal:** Add the Quint/TLA+ layer for critical behavioral properties.

1. Install Quint toolchain + Apalache
2. Identify 5-10 critical invariants from verified Facts (authorization, data consistency, real-time propagation)
3. LLM generates Quint specs with grammar-constrained output
4. Apalache model-checks each spec (verifier-in-the-loop: generate → check → fix → repeat)
5. Quint invariants → Hypothesis property tests (manual bridge for now)
6. Add `deal` or `icontract` Design-by-Contract decorators to generated business logic
7. Wire contract violations into gap analyzer

**Deliverable:** Critical invariants are formally specified, model-checked, and generate property-based tests that catch violations the path-coverage tests miss.

### Wave 4: Active Learning and Refinement (est. 2-3 weeks)
**Goal:** Close the loop — the system actively queries the target to refine its model.

1. Implement AALpy active learning (L* for Mealy machines) with browser automation as the teacher
2. `reset()` = navigate to known state; `step(action)` = execute action, observe response
3. Equivalence oracle: random walk + W-method approximation
4. Merge actively-learned model with passive model and LLM-generated XState
5. Conflict resolution: when models disagree, the active learner wins (it's ground truth)
6. Model diff → targeted recon: "the model says X should happen but it doesn't — investigate"

**Deliverable:** The system can autonomously refine its understanding of the target by systematically probing it, not just passively observing.

---

## Key Architectural Decisions

### Where to store formal models
In the output repo under `docs/models/`:
```
docs/models/
├── statecharts/          # XState JSON definitions
│   ├── board-lifecycle.json
│   ├── card-lifecycle.json
│   └── auth-flow.json
├── invariants/           # Quint specifications
│   ├── authorization.qnt
│   ├── data-consistency.qnt
│   └── realtime-propagation.qnt
├── mealy-machines/       # AALpy learned models (DOT format)
│   ├── board-api.dot
│   └── card-api.dot
└── schemas/              # Enriched OpenAPI + JSON Schema
    └── openapi.yaml
```

### Model ↔ Fact provenance
Every model element (state, transition, invariant) carries a `derived_from: [fact_id, ...]` field linking back to your existing Fact store. When a Fact is contradicted, all dependent model elements are flagged for re-evaluation. This is your existing provenance chain, extended one layer deeper.

### When to use which formalism
- **"What does the user see?"** → XState (observable workflows)
- **"What must always/never be true?"** → Quint invariant (temporal property)
- **"What shape must the data have?"** → JSON Schema + Design-by-Contract
- **"How does the API behave as a black box?"** → Mealy machine (automata learning)

### What NOT to formalize
- Visual layout (pixel positions, CSS, animations)
- Performance characteristics (latency, throughput)
- Third-party integration details (Stripe webhooks, OAuth provider quirks)
- Marketing copy and branding

These stay in prose specs. Formal methods add value where *correctness matters* — state transitions, data integrity, authorization, concurrency.

---

## The seL4 Lessons Applied

| seL4 Lesson | Duplicat-Rex Application |
|---|---|
| Executable spec as secret weapon | XState machines are both spec AND executable code |
| Three-layer refinement | XState (observable) → Quint (invariants) → Code (implementation) |
| Design for verification | Choose output stack that supports Design-by-Contract (Python `deal`) |
| Invariants dominate effort | Focus Quint specs on ~10-20 critical properties, not exhaustive modeling |
| Small changes break proofs | When Facts change, trace impact through model → test → code chain |
| Invest in automation early | Build the XState→test and Quint→property-test generators in Wave 1-2 |
| The spec is where bugs are found | The modeling process itself will surface spec bugs your recon missed |

---

## Risk Mitigation

**Risk: LLMs generate invalid XState/Quint**
Mitigation: Grammar-constrained generation. XState has a JSON schema; Quint has a grammar. Validate output mechanically before accepting. Verifier-in-the-loop: generate → validate → fix → repeat.

**Risk: Active automata learning is too slow (too many queries)**
Mitigation: Start with passive learning (RPNI) which needs zero queries. Use active learning selectively on high-uncertainty areas. Cache all responses. Rate-limit queries to stay under target's bot detection.

**Risk: Over-engineering — formal methods overhead exceeds value**
Mitigation: Wave 1 is deliberately small (one workflow, one XState machine). Measure: does mechanical test generation find bugs that LLM-generated tests missed? If yes, expand. If no, reconsider.

**Risk: Quint/Apalache toolchain is too academic/fragile**
Mitigation: Wave 3 is last for a reason. XState + Hypothesis/Schemathesis in Waves 1-2 deliver 80% of the value. Quint is the stretch goal for the remaining 20%.

---

## Bottom Line

The formal methods layer doesn't replace your intelligence pipeline — it gives it teeth. Instead of "the LLM thinks the spec is right," you get "the model checker proved these 15 invariants hold, the path generator covered 47 state transitions, and the property-based tester ran 10,000 random action sequences without finding a violation."

The pragmatic order is: **XState first** (highest value, lowest risk, most LLM-friendly), **then Hypothesis/Schemathesis** (mechanical test generation), **then AALpy** (active model refinement), **then Quint** (formal invariants for the critical stuff). Each layer is independently valuable — you don't need all four to benefit.

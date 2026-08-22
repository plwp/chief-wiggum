# Invariants — Epic: Dynamic Evidence-Driven Execution DAG (`dag`)

No DST-readiness invariants are added: Chief Wiggum is an established product.

## Business rules

- **BR-dag-001** — Parallel workers never use shared `refs/stash`; work is preserved by worktree-local commits. (#376)
- **BR-dag-002** — Intent, execution, evidence, mutation and authority records have versioned provider-neutral contracts before runtime exists. (#384)
- **BR-dag-003** — Replay of the transactional event journal is authoritative and deterministic. (#385)
- **BR-dag-004** — Evidence may propose graph changes but never grants itself authority. (#386)
- **BR-dag-005** — Continuous scheduling advances all and only derived-ready nodes, with fenced leases and safe replanning. (#387)
- **BR-dag-006** — Routing is capability-based, deterministic, cheap-first and escalates only on objective evidence. (#388)
- **BR-dag-007** — Candidate comparison is isolated, verifier-independent, identity-blind and promotes at most one hard-gate-passing result. (#389)
- **BR-dag-008** — Every state, decision and control is journal-explainable and structurally redacted. (#390)
- **BR-dag-009** — Experiment claims are pre-registered, reproducible, uncertainty-qualified and causally limited to randomized factors. (#391)
- **BR-dag-010** — Agentic OpenRouter execution uses the shared protocol without exposing credentials or hard-coding a model. (#392)

## Canonical-state and replay invariants

**INV-dag-001** — The transactional GraphJournal is the sole writer of canonical execution/control state.  
<!-- @cw-trace realizes BR-dag-002 BR-dag-003 BR-dag-004 BR-dag-005 -->
`controls_field: journal.graph_revision`; `sanctioned_writers: scripts/chief_wiggum/dag/journal.py`.

**INV-dag-002** — Committed events are hash-chained. Only an uncommitted partial tail may be discarded; complete or mid-journal corruption fails closed.  
<!-- @cw-trace realizes BR-dag-003 -->

**INV-dag-003** — `journal_seq` advances for every durable event; `graph_revision` advances once only for accepted readiness/authority-affecting changes.  
<!-- @cw-trace realizes BR-dag-003 BR-dag-004 -->

**INV-dag-004** — Replay reproduces distinct `schedule_hash` and `audit_state_hash` values byte-for-byte.  
<!-- @cw-trace realizes BR-dag-003 BR-dag-008 -->

**INV-dag-005** — Tracker/ratchet remain authoritative for human intent; execution imports pinned facts one-way.  
<!-- @cw-trace realizes BR-dag-002 BR-dag-004 -->

**INV-dag-006** — Static projection preserves all six `plan_waves.py` fields, sorting, warnings and exit codes 0/1/2 exactly.  
<!-- @cw-trace realizes BR-dag-002 BR-dag-003 BR-dag-005 -->

## Scheduling and execution invariants

**INV-dag-007** — Readiness is a pure derived predicate, never independently mutable.  
<!-- @cw-trace realizes BR-dag-005 -->

**INV-dag-008** — All exogenous decision inputs are journaled or content-hash pinned; replay never reads live time, tracker, config or health.  
<!-- @cw-trace realizes BR-dag-003 BR-dag-005 BR-dag-006 BR-dag-009 -->

**INV-dag-009** — Claim and dispatch-request outbox commit atomically; recovery reconciles the outbox before reclaim.  
<!-- @cw-trace realizes BR-dag-005 -->

**INV-dag-010** — Worker, promotion and Git effects require matching graph/node/attempt/claim/epoch/schedule/payload identity and an active unexpired lease. Git promotion is an idempotent at-least-once outbox update with expected-old-SHA CAS, not an impossible cross-store transaction.  
<!-- @cw-trace realizes BR-dag-005 BR-dag-007 -->

**INV-dag-011** — Immutable attempt outcome and candidate disposition are orthogonal.  
<!-- @cw-trace realizes BR-dag-002 BR-dag-007 -->

**INV-dag-012** — At most one independently verified, hard-gate-passing candidate is promoted per group under CAS.  
<!-- @cw-trace realizes BR-dag-007 -->

**INV-dag-013** — Fan-out requires isolation of worktrees and declared shared resources; `git stash` is forbidden.  
<!-- @cw-trace realizes BR-dag-001 BR-dag-007 -->

**INV-dag-014** — Pause/cancel take effect only at a durable safe boundary after worker quiescence and lease release/fencing.  
<!-- @cw-trace realizes BR-dag-005 BR-dag-008 -->

## Routing, worker and experiment invariants

**INV-dag-015** — Routing is deterministic from pinned capabilities, health, budget, evidence and policy.  
<!-- @cw-trace realizes BR-dag-006 BR-dag-008 -->

**INV-dag-016** — Escalation requires objective evidence; self-reported model confidence alone cannot escalate.  
<!-- @cw-trace realizes BR-dag-006 -->

**INV-dag-017** — Artifact author and verifier belong to different configured independence groups; absence fails closed.  
<!-- @cw-trace realizes BR-dag-006 BR-dag-007 -->

**INV-dag-018** — OpenRouter execution is model-agnostic; slugs occur only in provider configuration and experiment manifests.  
<!-- @cw-trace realizes BR-dag-006 BR-dag-010 -->

**INV-dag-019** — Credentials never enter model-controlled argv, environment, prompt, task files, metadata or logs, and the model sandbox cannot invoke the helper.  
<!-- @cw-trace realizes BR-dag-010 -->

**INV-dag-020** — Pre-registration is ratchet-bound before the first run and freezes arms, strata, metrics, controls, stopping and analysis.  
<!-- @cw-trace realizes BR-dag-009 -->

**INV-dag-021** — Experimental arms match nuisance controls; causal claims are limited to actually randomized crossed factors.  
<!-- @cw-trace realizes BR-dag-009 -->

**INV-dag-022** — Ox Alpha is `external-preview-tier`, not `open-tier`, absent public weights/licence evidence; its high observed input usage is a calibration/circuit-breaker input.  
<!-- @cw-trace realizes BR-dag-006 BR-dag-009 BR-dag-010 -->

**INV-dag-023** — Static fallback preserves both the exact six-field `plan_waves.py` projection and wave-barrier dispatch, gating and failure semantics.  
<!-- @cw-trace realizes BR-dag-003 BR-dag-005 -->

**INV-dag-024** — Candidate scoring is identity-blind, and only winner selection—not ordinary attempt success—may originate a Git promotion outbox event.  
<!-- @cw-trace realizes BR-dag-007 -->

**INV-dag-025** — Experiment runs follow the preregistered blocked-randomization/interleaving schedule; causal language requires a compliant assignment log and stable identity treatment.  
<!-- @cw-trace realizes BR-dag-009 -->

**INV-dag-026** — Every `blocked_safe` node has a journaled wake condition/deadline and eventually becomes reclaimable, cancelled, or a diagnosed liveness failure under logical time.  
<!-- @cw-trace realizes BR-dag-005 BR-dag-008 -->

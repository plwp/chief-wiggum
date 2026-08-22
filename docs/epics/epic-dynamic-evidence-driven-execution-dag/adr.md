# ADR — Dynamic Evidence-Driven Execution DAG

Status: proposed for epic #383.

## Decisions

1. **Two graph planes, one evidence/decision journal.** Tracker/ratchet own human
   intent; a pinned compiler imports it into an execution view. Evidence and
   decisions are immutable journal events. Execution never writes intent back.
2. **Transactional SQLite is authoritative.** Use an append-only `graph_events`
   table inside an exclusive transaction with unique idempotency keys, revision
   CAS and hash-chained rows. JSONL is an export, not the write store. This avoids
   implementing multiprocess serialization, framing and fsync recovery on top of
   the ratchet's deliberately non-transactional precedent.
3. **Derived readiness.** Persist admission, edges, blocks, leases, controls,
   outcomes and observations; compute readiness. `ready` is never a second writer.
4. **Separate counters and hashes.** `journal_seq` orders every audit event;
   `graph_revision` changes only for accepted state-affecting decisions. Both are
   non-negative signed 64-bit integers because SQLite `INTEGER` is signed.
   `schedule_hash` proves dispatch-equivalent state; `audit_state_hash` proves the
   complete materialized audit view.
5. **Separate lifecycle dimensions.** Attempt outcome is immutable. Candidate
   disposition, evidence lifecycle, approval, lease and runtime controls are
   orthogonal event projections. This resolves the #384/#389 succeeded→superseded
   contradiction.
6. **Fenced outboxes at external boundaries.** Dispatch and Git integration are
   recoverable at-least-once operations with idempotency keys, lease epochs and
   expected-old-SHA CAS. The graph guarantees one logical winner, not an atomic
   transaction spanning SQLite and Git. A fence binds graph, node, attempt, claim,
   epoch, schedule hash and payload hash and is valid only while its lease is active.
7. **Static compatibility remains an oracle.** Adapt ticket-level graph input to
   existing `planning.plan_waves`; preserve `waves`, `gated`, `skipped`,
   `warnings`, `integration_risks`, `gate_reasons`, and exits 0/1/2. Static fallback
   also preserves wave barriers, gating and failure semantics; projection parity alone
   is insufficient.
8. **Provider roles and execution adapters remain data.** A versioned provider
   policy distinguishes `consult_adapter` from `execution_adapter`, capabilities,
   tier, independence group, budgets and fallback. #392 implements a generic
   Responses-compatible coding worker; Ox Alpha is configuration only.
9. **Credential helper is an orchestrator capability, not a shell tool.** The
   trusted parent resolves credentials outside the model-controlled workspace and
   injects authentication through a protected provider boundary. If the model can
   invoke the helper, execution fails closed because the helper becomes a secret
   oracle.
10. **#391 is factorial where feasible.** Cross static/dynamic orchestration with
    provider policy/model tiers under identical harness, tools, sandbox, verifier
    and budget. A preregistered deterministic blocked-randomization/interleaving
    algorithm and immutable assignment log control temporal/provider drift.
    Missing/non-randomized/noncompliant cells allow association, not causal claims.
11. **Canonical bytes are versioned.** Hash inputs use schema-versioned UTF-8 JSON
    with sorted object keys, declared list/set ordering, NFC Unicode, integer-only
    numbers, explicit null/absence rules and LF endings. The genesis predecessor is
    64 lowercase zero hex characters.

## Consequences and sequencing

#376 precedes candidate fan-out. #384 defines schemas/semantics; #385 implements
the store; #386 admission; #387 scheduling; #392 may proceed once shared protocol
metadata stabilizes and must precede Ox runs; #388 routing; #390 read views before
controls; #389 candidates; #391 pre-registration before any measured run.

Related #371 and #375 are feature-detected dependencies. Their absence must be
journaled as degraded/unavailable, never converted to an empty success.

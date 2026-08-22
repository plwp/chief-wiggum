# Dynamic DAG data contract

Status: v1 (`schema_version: "1.0.0"`). This document defines data and validation only. Persistence, admission, scheduling, dispatch, and provider routing are implemented by later tickets.

## Three planes, one future journal

The contract separates three kinds of truth:

1. **Intent plane** — human-authoritative tracker and ratchet facts. Intent nodes and `depends_on` edges retain a pinned source reference and digest plus a closed scan status (`observed`, `degraded`, `unavailable`, `malformed`, or `unscanned`). Execution may import these facts; it may never write changes back to intent. Only `observed` ticket-projectable intent can produce a static wave plan; every other status fails closed.
2. **Execution plane** — machine-managed compiled nodes, schedulable edges, and typed non-scheduling relations. Each execution node pins the intent node and intent-graph digest from which it was compiled.
3. **Evidence plane** — append-only observations and proposal envelopes. Evidence supports decisions but does not silently mutate either graph. A later journal implementation will commit accepted facts and normalized rejection decisions.

A single mutable graph was rejected because it obscures authority: a derived conflict edge could otherwise become indistinguishable from a maintainer's declared dependency. The split also makes replay and static fallback testable.

Published schemas live in `schemas/dag/v1/`. `schema-catalog.json` is the offline record-type registry; `authority-matrix.json` is policy data consumed by semantic admission. Schemas are strict and additive changes within v1 must be explicit optional properties. Readers never guess a compatible version: missing or unsupported versions are typed failures.

## Records and independent state

The contract publishes records for intent nodes/edges/graphs, execution nodes, schedulable edges, non-scheduling relations, evidence, approvals, leases, controls, mutation envelopes, graph snapshots, and graph manifests.

Ticket lifecycle is:

```text
proposed -> admitted -> ready -> running -> succeeded | failed | superseded | cancelled
                         |          |
                         +-> blocked <-+
                              |
                              +-> ready
```

`ready` is a derived projection. It appears in the lifecycle vocabulary so snapshots and compatibility projections can report it, but a proposal cannot directly set it. `succeeded`, `failed`, `superseded`, and `cancelled` are terminal and immutable.

This ticket lifecycle is intentionally not the epic formal model's proposal/admission/claim control machine. States such as `received`, `validated`, `pending_approval`, `claimed`, `blocked_safe`, and `expired` describe proposal, lease, or attempt control in that machine; v1 represents those concerns through the independent approval, lease, control, and attempt fields below. Ticket #385 composes the machines. It must not replace one vocabulary with the other or make derived `ready` independently writable.

Attempt outcome, candidate disposition, approval state, lease state, and control state are separate fields. In particular, candidate promotion or supersession never rewrites an attempt's successful or failed outcome. A correction to a terminal record is represented by a `compensate` operation that references the earlier mutation and a distinct replacement record; the old terminal stays unchanged.

## Scheduling subset and direction

Only `depends_on` and `ordered_after` are schedulable. Both use the same direction: `source` runs after `target`. Cycle and topological checks inspect only these edges.

`supersedes`, `retry_of`, `candidate_of`, `verifies`, `derived_from`, and `conflicts_with` are stored relations and never affect ordering. `conflicts_with` is symmetric; an admission decision preserves that relation as evidence and separately proposes one asymmetric `ordered_after` edge. Cycles made only from non-scheduling relations are valid.

## Mutation and authority

Deterministic collectors and generative workers submit the same envelope. `actor` records provenance; `authority_class` records permission. Neither creates a privileged validation path.

Every envelope includes the graph/base revision, stable mutation and idempotency IDs, ordered operations, a closed reason code, evidence references, expected effect, integer budget delta, and approval flag. `authority-matrix.json` is the sole machine-readable mapping from operation type to automatic/approval-required policy. An automatic actor proposing any approval-required operation is invalid even when `requires_approval` is incorrectly set to false.

The v1 reason vocabulary is:

- `EV_DEP_DECLARED` — human/tracker dependency import
- `EV_DEP_DISCOVERED` — evidence-derived dependency proposal
- `EV_PATH_CONFLICT` — proven resource/path conflict
- `EV_GATE_FAILED` — required gate failure
- `EV_PROVIDER_UNAVAILABLE` — required role cannot be served
- `EV_LEASE_EXPIRED` — logical-time lease expiry
- `EV_BUDGET_EXCEEDED` — integer budget limit reached
- `EV_CANDIDATE_PROMOTED` — candidate disposition decision
- `EV_HUMAN_DECISION` — authenticated human authority
- `EV_COMPENSATING_EVENT` — correction that preserves immutable history

Free-text explanation may accompany future evidence, but it cannot replace the stable code.

## Canonical bytes

DAG hashes use UTF-8 JSON with no BOM, NFC-normalized keys and strings, sorted object keys, compact separators, integer-only numeric values, and exactly one trailing LF. Duplicate object keys, floats/exponent notation, CRLF, and non-canonical key ordering are rejected. No defaults are inserted during encoding. The CLI and canonical-byte validator reject records over 16 MiB; collection schemas also carry explicit bounds, and graph cycle validation is iterative rather than recursion-limited.

Arrays are either ordered sequences or deterministic collections. Mutation operations preserve order. Stable-record collections sort by their stable ID; `capabilities`, `derived_from`, and `evidence_refs` sort by canonical value. Optional means absent; required nullable fields use explicit `null`.

Legacy `plan_waves.py` output is deliberately not re-encoded with this profile. Compatibility means preserving its existing JSON shape and semantics exactly.

## Static compatibility

`chief_wiggum.dag.compatibility` is one-way:

```text
milestone DEPENDENCIES comment
  -> github.parse_dependency_block()
  -> intent nodes + depends_on edges
  -> planning.plan_waves()
  -> waves, gated, skipped, warnings, integration_risks, gate_reasons
```

The adapter calls both existing functions; it does not copy their logic. It preserves numeric sorting, parser warnings, closed/external dependency handling, gating, integration-risk strings, exit `1` for corrupt, missing, unobserved, or non-ticket-projectable input, and exit `2` for cycles. The Factory Hardening milestone fixture is the historical golden round-trip.

## Validation entry point

Validate structure and graph semantics:

```bash
python3 scripts/dag_contract.py validate tests/fixtures/dag/v1/valid/full-snapshot.json
```

Add `--canonical` when the source bytes themselves must satisfy the hash profile. `canonicalize` emits canonical bytes. `project-waves` imports a dependency description and emits the unchanged six-field static projection. Contract failures exit `2` with stable error codes; unreadable input exits `1`.

The reusable corpus under `tests/fixtures/dag/v1/` includes valid records plus cycles, every lifecycle pair, dangling references, approval boundaries, terminal compensation, divergent idempotency, canonical bytes, and historical compatibility inputs for tickets #385 and #386.

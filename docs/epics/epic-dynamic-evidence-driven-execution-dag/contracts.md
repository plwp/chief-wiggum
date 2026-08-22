## Entity: GraphJournal

Per-graph canonical transactional event store. Tracker intent, task directories, snapshots, factory telemetry and static plans are imports, outboxes or projections, never competing execution truth.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| graph_id | string | always | transactional SQLite graph_events database | immutable |
| journal_seq | nonnegative-int64 | always | SQLite INTEGER PRIMARY KEY sequence | immutable; Advances for every durable event, including normalized rejection decisions and evidence; maximum is SQLite signed 64-bit. |
| graph_revision | nonnegative-int64 | always | accepted graph/control mutation projection | Advances only for accepted state affecting readiness, authority, routing, leases, budgets, candidates or topology; maximum is SQLite signed 64-bit. |
| previous_record_hash | sha256 | always | preceding committed graph event | immutable; The genesis record uses 64 lowercase zero hex characters. |
| schedule_hash | sha256 | always | canonical scheduling projection | immutable; Covers nodes, schedulable edges, outcomes, controls, leases, resources, budgets, provider-policy snapshot and logical-time observations. |
| audit_state_hash | sha256 | always | canonical complete materialized projection | immutable; Additionally covers relations, evidence provenance, decisions, routing and candidate disposition. |
| payload | object | always | schema-validated and redacted event payload | immutable; Arbitrary raw tool output and credentials are prohibited; redactor version and policy hash are pinned. |

### POST /dag/graphs/:graph_id/events

- **REQUIRES**: The caller holds the exclusive SQLite write transaction and the envelope base revision equals the current graph revision.; The envelope is bounded, version-recognized and safely decodable into either a mutation candidate or a typed rejection envelope before the transaction commits.
- **ENSURES**: Exactly one hash-chained event commits atomically and accepted graph effects become visible together.; Rejected decisions advance journal_seq but not graph_revision; accepted state changes advance both exactly once.
- **ERROR CASES**: 409 if base revision is stale; 422 if schema, authority, cycle, evidence, terminal-state or semantic validation fails; 503 if exclusive writer transaction cannot be acquired; 500 if transaction durability cannot be established

### POST /dag/graphs/:graph_id/intent/import

- **REQUIRES**: The tracker adapter reports capability and scan status and supplies a source revision/hash; malformed or unavailable input is not interpreted as an empty graph.
- **ENSURES**: Human intent remains tracker/ratchet authoritative and is imported one-way as immutable version-bound facts.
- **ERROR CASES**: 422 if source status or digest is absent

### GET /dag/graphs/:graph_id/project/waves

- **REQUIRES**: Intent compilation has a verified source status and ticket-level projection is possible.
- **ENSURES**: The projection is byte-equivalent to plan_waves.py for all six fields waves, gated, skipped, warnings, integration_risks and gate_reasons, with exit 0 success, 1 invalid input and 2 cycle.
- **ERROR CASES**: 503 if intent scan is unavailable, degraded, malformed or unscanned; 422 if ticket projection is undefined or dependency input is invalid/corrupt; 409 if cycle exists across projected dependency nodes

- **INVARIANT**: A complete well-formed record with a bad hash or any mid-journal corruption fails closed; recovery discards only an uncommitted partial tail transaction.

## Entity: ExecutionControl

Execution node, mutation approval, lease, attempt outcome, candidate disposition and evidence lifecycle facts from which readiness is derived.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| node_id | string | always | GraphJournal admitted node-created event | immutable |
| attempt_outcome | enum[pending,running,succeeded,failed,cancelled,expired] | always | GraphJournal attempt events | Outcome is terminal and orthogonal to candidate disposition. |
| candidate_disposition | enum[not_candidate,pending,eligible,eliminated,promoted,superseded] | always | GraphJournal candidate-group events | — |
| lease_epoch | nonnegative-int64 | after attempt_outcome == running | scheduler claim event | — |
| ready | boolean | always | pure derived readiness predicate | Never independently mutated or journaled as authority. |
| evidence_status | enum[raw,validated,admitted,superseded,retracted,unavailable,malformed,unscanned] | always | evidence lifecycle projection | — |
| control_status | enum[active,pause_requested,paused,cancel_requested,cancelled,waiting_human,quiescent_failed] | always | operator and scheduler events | — |

### GET /dag/graphs/:graph_id/ready

- **REQUIRES**: All decision inputs are from the pinned journal projection, including logical ticks, provider health, budgets, policy, leases and resources.
- **ENSURES**: A node is ready iff admitted, nonterminal, all schedulable predecessors succeeded, no active block/control constraint exists, resources and provider capabilities are claimable, and retry/budget limits allow dispatch.
- **ERROR CASES**: 503 if a required readiness input is unavailable or unscanned

### POST /dag/nodes/:node_id/dispatch

- **REQUIRES**: The node is currently ready, the next lease epoch is atomically allocated, and all shared mutable resources are claimable.
- **ENSURES**: Claim acquisition and dispatch-request outbox event commit atomically before process launch; every result and Git action must present the current fencing token.
- **ERROR CASES**: 409 if node no longer ready or resource/lease collision; 412 if stale worker presents an older lease epoch
- **STATE TRANSITION**: admitted → claimed → running

### POST /dag/candidate-groups/:group_id/promote

- **REQUIRES**: All candidates have terminal attempt outcomes, the chosen candidate succeeded and passed the frozen independent verifier, isolation is proven, and the group has no winner.
- **ENSURES**: One atomic event marks exactly one candidate promoted and all other eligible candidates superseded without changing immutable attempt outcomes.
- **ERROR CASES**: 409 if winner already exists, graph revision is stale, or candidates are not all terminal; 412 if verifier hash, independence, identity blinding or isolation proof fails; 422 if chosen candidate did not succeed/pass hard gates or all candidates failed hard gates

### POST /dag/graphs/:graph_id/control

- **REQUIRES**: An authenticated runtime operator supplies identity, exact action, non-empty reason and the current base revision.
- **ENSURES**: The action is journaled; a running worker pauses or cancels only after a safe commit boundary and lease release/revocation.
- **ERROR CASES**: 400 if action is unknown or reason is empty; 401 if operator identity is absent; 409 if base revision is stale; 409 if safe boundary not yet reached

## Entity: ProviderExecutionDecision

Model-agnostic capability routing and agentic worker execution record; provider roles remain configuration, not hard-coded workflow branches.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| execution_adapter | string | always | versioned provider policy snapshot | immutable |
| consult_adapter | string | optional | versioned provider policy snapshot | immutable |
| capability_tier | enum[frontier-tier,micro-tier,open-tier,external-preview-tier] | always | provider configuration with evidence | immutable |
| independence_group | string | always | provider policy snapshot | immutable |
| resolved_model | string | after provider exposes resolved identity | worker event stream | immutable |
| metadata | object | always | shared delegate protocol schema | immutable; Contains no credentials, raw secrets or absolute home paths. |

### POST /dag/nodes/:node_id/route

- **REQUIRES**: Required capabilities, budget, health observations, objective escalation evidence and provider policy are pinned by digest.
- **ENSURES**: Selection is deterministic, cheapest capable first, escalation requires an objective trigger, and the author and verifier independence groups differ.
- **ERROR CASES**: 422 if any decision input is not content-hash pinned; 409 if no independent verifier or capable fallback exists; 402 if node or graph budget cannot fund the route

### POST /delegates/openrouter/tasks/:task_id/run

- **REQUIRES**: Execution is in a verified non-main worktree with workspace-write sandbox; a trusted orchestrator resolves the key through a command-backed helper outside the model-controlled workspace.
- **ENSURES**: Success atomically publishes schema-valid metadata, result, log and DONE; failure publishes ERROR with one stable reason; DONE and ERROR are mutually exclusive.
- **ERROR CASES**: 401 if credential missing or helper boundary unsafe; 412 if main checkout or unsupported tool/model behavior; 408 if worker timeout; 502 if non-zero exit, malformed stream or missing result

- **INVARIANT**: Any anonymous preview without recorded public weights and license evidence is classified external-preview-tier, never open-tier; specific slugs remain configuration/manifest data only.

## Entity: ExperimentRun

Pre-registered, immutable, budget-matched comparison of orchestration and provider-policy factors with causal claims limited to the randomized factors actually crossed.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| preregistration_hash | sha256 | always | ratchet-bound pre-registration committed before runs | immutable |
| orchestration_arm | enum[static,dynamic] | always | run manifest | immutable |
| provider_policy_arm | enum[current,cheap-first,micro-first,external-preview-first] | always | run manifest | immutable |
| corpus_version | string | always | immutable corpus manifest | immutable |
| verifier_hash | sha256 | always | run manifest | immutable |
| assignment_record_hash | sha256 | always | preregistered deterministic blocked-randomization assignment log | immutable |
| raw_result_hash | sha256 | always | content-addressed immutable result | immutable |
| decision | enum[go,hold,rollback] | after analysis complete | journaled experiment decision | — |

### POST /experiments/:experiment_id/runs

- **REQUIRES**: Pre-registration predates all runs and fixes corpus strata, factorial cells, deterministic blocked-randomization/interleaving algorithm, assignment unit/log, budgets, tools, sandbox, verifier, seeds, stopping rules, non-inferiority margin, gap formula and degenerate cases.
- **ENSURES**: Every run publishes an immutable manifest and raw result bound to the ratchet journal; headline slices include N and confidence intervals and retain protocol violations and negative results.
- **ERROR CASES**: 412 if pre-registration, randomized assignment/interleaving, contamination, budget matching, verifier freeze or manifest completeness fails

- **INVARIANT**: The primary design is a crossed orchestration × provider-policy factorial where feasible; if cells are missing or observational, report associations only and do not attribute a model effect to the DAG or a DAG effect to the model.

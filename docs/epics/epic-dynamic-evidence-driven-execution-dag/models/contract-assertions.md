# Contract Assertion Templates

Generated from formal contracts. Each operation has precondition and postcondition checks.

## Commit Graph Event (POST /dag/graphs/:graph_id/events)

### Precondition Tests
- [ ] **CTR-dag-001**: Verify The caller holds the exclusive SQLite write transaction and the envelope base revision equals the current graph revision.
  - Call WITHOUT this condition → expect error
- [ ] **CTR-dag-002**: Verify The envelope is bounded, version-recognized and safely decodable into either a mutation candidate or a typed rejection envelope before the transaction commits.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-003**: Verify Exactly one hash-chained event commits atomically and accepted graph effects become visible together.
  - Call correctly → assert postcondition holds
- [ ] **CTR-dag-004**: Verify Rejected decisions advance journal_seq but not graph_revision; accepted state changes advance both exactly once.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 409: base revision is stale
- [ ] Status 422: schema, authority, cycle, evidence, terminal-state or semantic validation fails
- [ ] Status 503: exclusive writer transaction cannot be acquired
- [ ] Status 500: transaction durability cannot be established

## Import Tracker Intent (POST /dag/graphs/:graph_id/intent/import)

### Precondition Tests
- [ ] **CTR-dag-005**: Verify The tracker adapter reports capability and scan status and supplies a source revision/hash; malformed or unavailable input is not interpreted as an empty graph.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-006**: Verify Human intent remains tracker/ratchet authoritative and is imported one-way as immutable version-bound facts.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 422: source status or digest is absent

## Project Static Waves (GET /dag/graphs/:graph_id/project/waves)

### Precondition Tests
- [ ] **CTR-dag-007**: Verify Intent compilation has a verified source status and ticket-level projection is possible.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-008**: Verify The projection is byte-equivalent to plan_waves.py for all six fields waves, gated, skipped, warnings, integration_risks and gate_reasons, with exit 0 success, 1 invalid input and 2 cycle.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 503: intent scan is unavailable, degraded, malformed or unscanned
- [ ] Status 422: ticket projection is undefined or dependency input is invalid/corrupt
- [ ] Status 409: cycle exists across projected dependency nodes

## Derive Ready Set (GET /dag/graphs/:graph_id/ready)

### Precondition Tests
- [ ] **CTR-dag-010**: Verify All decision inputs are from the pinned journal projection, including logical ticks, provider health, budgets, policy, leases and resources.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-011**: Verify A node is ready iff admitted, nonterminal, all schedulable predecessors succeeded, no active block/control constraint exists, resources and provider capabilities are claimable, and retry/budget limits allow dispatch.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 503: a required readiness input is unavailable or unscanned

## Acquire Fenced Lease and Dispatch (POST /dag/nodes/:node_id/dispatch)

### Precondition Tests
- [ ] **CTR-dag-012**: Verify The node is currently ready, the next lease epoch is atomically allocated, and all shared mutable resources are claimable.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-013**: Verify Claim acquisition and dispatch-request outbox event commit atomically before process launch; every result and Git action must present the current fencing token.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 409: node no longer ready or resource/lease collision
- [ ] Status 412: stale worker presents an older lease epoch

## Promote Candidate (POST /dag/candidate-groups/:group_id/promote)

### Precondition Tests
- [ ] **CTR-dag-014**: Verify All candidates have terminal attempt outcomes, the chosen candidate succeeded and passed the frozen independent verifier, isolation is proven, and the group has no winner.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-015**: Verify One atomic event marks exactly one candidate promoted and all other eligible candidates superseded without changing immutable attempt outcomes.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 409: winner already exists, graph revision is stale, or candidates are not all terminal
- [ ] Status 412: verifier hash, independence, identity blinding or isolation proof fails
- [ ] Status 422: chosen candidate did not succeed/pass hard gates or all candidates failed hard gates

## Apply Operator Control (POST /dag/graphs/:graph_id/control)

### Precondition Tests
- [ ] **CTR-dag-016**: Verify An authenticated runtime operator supplies identity, exact action, non-empty reason and the current base revision.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-017**: Verify The action is journaled; a running worker pauses or cancels only after a safe commit boundary and lease release/revocation.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 400: action is unknown or reason is empty
- [ ] Status 401: operator identity is absent
- [ ] Status 409: base revision is stale
- [ ] Status 409: safe boundary not yet reached

## Route Execution (POST /dag/nodes/:node_id/route)

### Precondition Tests
- [ ] **CTR-dag-018**: Verify Required capabilities, budget, health observations, objective escalation evidence and provider policy are pinned by digest.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-019**: Verify Selection is deterministic, cheapest capable first, escalation requires an objective trigger, and the author and verifier independence groups differ.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 422: any decision input is not content-hash pinned
- [ ] Status 409: no independent verifier or capable fallback exists
- [ ] Status 402: node or graph budget cannot fund the route

## Run Agentic OpenRouter Worker (POST /delegates/openrouter/tasks/:task_id/run)

### Precondition Tests
- [ ] **CTR-dag-020**: Verify Execution is in a verified non-main worktree with workspace-write sandbox; a trusted orchestrator resolves the key through a command-backed helper outside the model-controlled workspace.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-021**: Verify Success atomically publishes schema-valid metadata, result, log and DONE; failure publishes ERROR with one stable reason; DONE and ERROR are mutually exclusive.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: credential missing or helper boundary unsafe
- [ ] Status 412: main checkout or unsupported tool/model behavior
- [ ] Status 408: worker timeout
- [ ] Status 502: non-zero exit, malformed stream or missing result

## Execute Registered Arm (POST /experiments/:experiment_id/runs)

### Precondition Tests
- [ ] **CTR-dag-023**: Verify Pre-registration predates all runs and fixes corpus strata, factorial cells, deterministic blocked-randomization/interleaving algorithm, assignment unit/log, budgets, tools, sandbox, verifier, seeds, stopping rules, non-inferiority margin, gap formula and degenerate cases.
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **CTR-dag-024**: Verify Every run publishes an immutable manifest and raw result bound to the ratchet journal; headline slices include N and confidence intervals and retain protocol violations and negative results.
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 412: pre-registration, randomized assignment/interleaving, contamination, budget matching, verifier freeze or manifest completeness fails

# Test Plan: Dynamic DAG Execution Control

Generated from formal model. 19 paths covering 11/11 states and 19 transitions.

## Positive Test Cases (valid paths)

### Path 1: → validated
```
received--validate-->validated
```

### Path 2: → failed
```
received--validate-->validated → validated--queue_approval-->pending_approval → pending_approval--reject_or_expire-->failed
```

### Path 3: → failed
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--publish_failure-->failed
```

### Path 4: → failed
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--pause_or_block_at_safe_boundary-->blocked_safe → blocked_safe--liveness_failure-->failed
```

### Path 5: → failed
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--dispatch_failed_permanently-->failed
```

### Path 6: → failed
```
received--validate-->validated → validated--reject_policy-->failed
```

### Path 7: → failed
```
received--reject_invalid-->failed
```

### Path 8: → pending_approval
```
received--validate-->validated → validated--queue_approval-->pending_approval
```

### Path 9: → admitted
```
received--validate-->validated → validated--admit_validated-->admitted
```

### Path 10: → claimed
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed
```

### Path 11: → cancelled
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--pause_or_block_at_safe_boundary-->blocked_safe → blocked_safe--cancel_blocked-->cancelled
```

### Path 12: → cancelled
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--cancel_at_safe_boundary-->cancelled
```

### Path 13: → cancelled
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--revoke_before_start-->cancelled
```

### Path 14: → cancelled
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--cancel_before_dispatch-->cancelled
```

### Path 15: → running
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running
```

### Path 16: → expired
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--lease_expired-->expired
```

### Path 17: → expired
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--lease_expired_before_start-->expired
```

### Path 18: → succeeded
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--publish_success-->succeeded
```

### Path 19: → blocked_safe
```
received--validate-->validated → validated--admit_validated-->admitted → admitted--claim_ready-->claimed → claimed--worker_started-->running → running--pause_or_block_at_safe_boundary-->blocked_safe
```

## Negative Test Cases (must be rejected)

- **received → admitted**: Schema and semantic validation cannot be skipped. — expect 400/409
- **pending_approval → admitted**: Approval must return through current-revision validation. — expect 400/409
- **admitted → running**: A fenced lease and dispatch outbox must be committed first. — expect 400/409
- **running → admitted**: An attempt is never reset; retries are new nodes. — expect 400/409
- **running → claimed**: Lease epochs cannot move backwards. — expect 400/409
- **succeeded → failed**: Attempt outcomes are immutable. — expect 400/409
- **succeeded → cancelled**: Attempt outcomes are immutable. — expect 400/409
- **succeeded → running**: A retry is a new node. — expect 400/409
- **failed → running**: A retry is a new node. — expect 400/409
- **failed → succeeded**: Attempt outcomes are immutable. — expect 400/409
- **cancelled → running**: Cancellation is terminal and fenced. — expect 400/409
- **cancelled → admitted**: A cancelled attempt cannot be revived. — expect 400/409
- **expired → running**: Expired epoch outputs are fenced; retry uses a new node. — expect 400/409
- **expired → succeeded**: Late success from a stale epoch is rejected. — expect 400/409
- **blocked_safe → running**: Unblocking requires a new fenced claim epoch. — expect 400/409

## Invariant Checks (verify at each state)

- **INV-dag-001**: The transactional GraphJournal is the sole writer of graph_revision and canonical execution/control state.
- **INV-dag-002**: Every committed record is hash-chained; only an uncommitted partial tail is recoverable, while complete or mid-journal corruption fails closed.
- **INV-dag-003**: journal_seq advances for every durable event; graph_revision advances exactly once only for accepted readiness/authority-affecting changes.
- **INV-dag-004**: schedule_hash and audit_state_hash use schema-versioned canonical UTF-8 JSON with sorted object keys, defined list/set ordering, NFC Unicode, integer-only numbers, explicit null/absence rules and LF line endings; replay reproduces both byte-for-byte.
- **INV-dag-005**: Tracker/ratchet remain authoritative for human intent; execution imports pinned facts one-way and never writes intent back.
- **INV-dag-006**: Static projection preserves the exact six fields, sorting, warnings and exit-code contract of plan_waves.py.
- **INV-dag-007**: Readiness is a pure derived predicate, never independently mutable state.
- **INV-dag-008**: Every exogenous scheduling input is an immutable event or content-hashed policy/observation; replay never queries live time, tracker, config or health.
- **INV-dag-009**: Claim and dispatch-request outbox are atomic; recovery reconciles the outbox before issuing a new epoch.
- **INV-dag-010**: All worker outputs and promotion/Git effects require matching graph, node, attempt, claim, epoch, schedule and payload identities plus an active unexpired lease; stale workers may leave bytes but cannot admit or promote them.
- **INV-dag-011**: Attempt outcome and candidate disposition are orthogonal; promotion/supersession never rewrites terminal success or failure.
- **INV-dag-012**: At most one candidate per group is promoted, selected under CAS from hard-gate-passing successful candidates with frozen independent verifier hashes.
- **INV-dag-013**: Candidate fan-out is refused unless isolation covers worktrees plus repo-global refs/config/hooks, ports, databases, caches and other declared shared resources; git stash is forbidden.
- **INV-dag-014**: Pause and cancel become effective only at a durable safe boundary after the worker is quiescent and its lease is released or fenced.
- **INV-dag-015**: Provider routing is deterministic from pinned capabilities, health, budgets, evidence and policy, with explicit stable tie-breaks.
- **INV-dag-016**: Escalation requires an objective evidence trigger; model self-confidence alone cannot escalate.
- **INV-dag-017**: An artifact author and verifier must belong to different configured independence groups; absence fails closed.
- **INV-dag-018**: The OpenRouter execution adapter is model-agnostic and no workflow/routing code hard-codes model or vendor strings.
- **INV-dag-019**: Credentials never enter model-controlled argv, environment, prompt, task files, event metadata or logs; the helper is outside and uncallable from the model sandbox.
- **INV-dag-020**: Experiment pre-registration is ratchet-bound before the first run and fixes factorial arms, corpus/strata, metrics, budgets, tools, verifier, stopping and analysis rules.
- **INV-dag-021**: Experimental arms use the same harness, sandbox, tools, verifier and budget policy except for explicitly randomized factors; causal claims never exceed crossed randomized cells.
- **INV-dag-022**: Anonymous previews, including the currently configured Ox Alpha experiment arm, remain external-preview-tier absent public weights/license evidence; unusually high probe usage is a calibration risk and circuit-breaker input.
- **INV-dag-023**: Static fallback preserves both the exact plan_waves.py projection contract and wave-barrier dispatch, gating and failure semantics; journal failure never silently becomes a dynamic or partial executor.
- **INV-dag-024**: Candidate promotion scoring is identity-blind and Git publication can originate only from the winner-selected promotion outbox, never ordinary attempt success.
- **INV-dag-025**: Experiment runs follow the preregistered deterministic blocked-randomization and interleaving schedule; causal language requires assignment-log compliance and stable identity treatment.
- **INV-dag-026**: Every blocked_safe node has a journaled wake condition/deadline and eventually becomes reclaimable, cancelled or a diagnosed liveness failure under logical time.

## Coverage Summary

| Metric | Value |
|--------|-------|
| Total paths | 19 |
| States covered | 11/11 |
| Transitions covered | 19 |
| Invalid transitions to test | 15 |
| Invariants to verify | 26 |

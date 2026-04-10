# Test Plan: Order Status State Machine

Generated from formal model. 6 paths covering 6/6 states and 6 transitions.

## Positive Test Cases (valid paths)

### Path 1: → pending
```
draft--submit-->pending
```

### Path 2: → confirmed
```
draft--submit-->pending → pending--confirm-->confirmed
```

### Path 3: → cancelled
```
draft--submit-->pending → pending--confirm-->confirmed → confirmed--cancel-->cancelled
```

### Path 4: → cancelled
```
draft--submit-->pending → pending--cancel-->cancelled
```

### Path 5: → in_progress
```
draft--submit-->pending → pending--confirm-->confirmed → confirmed--start-->in_progress
```

### Path 6: → completed
```
draft--submit-->pending → pending--confirm-->confirmed → confirmed--start-->in_progress → in_progress--complete-->completed
```

## Negative Test Cases (must be rejected)

- **draft → confirmed**: Cannot skip pending validation — expect 400/409
- **completed → in_progress**: Completion is irreversible — expect 400/409
- **cancelled → draft**: Cancellation is terminal — no transitions out — expect 400/409
- **cancelled → pending**: Cancellation is terminal — no transitions out — expect 400/409
- **cancelled → confirmed**: Cancellation is terminal — no transitions out — expect 400/409

## Invariant Checks (verify at each state)

- **INV-001**: Item names are NEVER stored as strings on orders. Always reference item_ids and resolve at read time.
- **INV-002**: Every order with status >= pending has a non-null customer_id that references a valid customer.
- **INV-003**: end_date > start_date on every order, enforced at write time.
- **INV-004** (in states: in_progress): An order in in_progress MUST have a resource_id assigned.
- **INV-005**: Dashboard, List View, Calendar, and Detail View MUST derive record counts from the same query. No screen-specific counting logic.
- **INV-006**: Capacity View and Summary View MUST use the same occupancy calculation. Define once, use everywhere.
- **INV-007**: If an operation depends on an external service (email, payment), the success toast MUST NOT display unless the service call succeeded.

## Coverage Summary

| Metric | Value |
|--------|-------|
| Total paths | 6 |
| States covered | 6/6 |
| Transitions covered | 6 |
| Invalid transitions to test | 5 |
| Invariants to verify | 7 |

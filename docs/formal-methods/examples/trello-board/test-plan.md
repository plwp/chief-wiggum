# Test Plan: Board Lifecycle

Generated from formal model. 3 paths covering 3/3 states and 3 transitions.

## Positive Test Cases (valid paths)

### Path 1: → closed
```
active--close-->closed
```

### Path 2: → deleted
```
active--close-->closed → closed--delete-->deleted
```

### Path 3: → deleted
```
active--delete-->deleted
```

## Negative Test Cases (must be rejected)

- **deleted → active**: deleted is a terminal state — no transitions out — expect 400/409
- **deleted → closed**: deleted is a terminal state — no transitions out — expect 400/409

## Coverage Summary

| Metric | Value |
|--------|-------|
| Total paths | 3 |
| States covered | 3/3 |
| Transitions covered | 3 |
| Invalid transitions to test | 2 |
| Invariants to verify | 0 |

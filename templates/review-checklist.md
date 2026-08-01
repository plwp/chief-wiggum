# Structured Review Checklist

Score each item: **PASS**, **FAIL** (with one-line justification), or **N/A**.

## Behavior Preservation (refactor tickets — evaluate FIRST for `kind: refactor`)

For `kind: refactor` tickets (a `/plan-epic --from-debt` ticket, or any ticket labeled `refactor`), this section comes before everything else — a refactor that changes behavior has failed regardless of how clean the code is. N/A for other ticket kinds.

- [ ] Characterization/golden tests pinning CURRENT behavior were committed and green BEFORE the first refactor commit (baseline commit precedes refactor commits in history)
- [ ] No characterization test was weakened, deleted, or re-pinned to new values during the refactor — a deliberate behavior change is a different ticket, not a refactor
- [ ] The diff is structural: no user-visible output, API response, error message, or persisted format changed
- [ ] Mutation results (where a tool ran) show the pinned tests actually kill mutants in the refactored files; where no tool exists, the gap is stated, not silent

## Scope Discipline (adopted repos — all ticket kinds)

For repos with an adoption record (chief-wiggum#215), score these against the declared pathset and the review context's flagged sections. N/A for non-adopted repos.

- [ ] Every changed file is inside the declared pathset (or the declaration was updated with a stated reason) — no unexplained out-of-pathset hunks
- [ ] No in-diff opportunistic fixes: nothing was "improved while in here" outside the ticket's scope
- [ ] No unfiled discoveries: anything found mid-ticket (dead code, clone, smell) was filed as a `DEBT-` candidate (`debt_inventory.py append-candidate`) or an issue — found ≠ fixed, but found ≠ forgotten either
- [ ] No collateral improvement: zero formatting-only, rename-only, or style-only hunks outside the declared pathset

## Correctness
- [ ] Every acceptance criterion from the ticket has at least one test that verifies it
- [ ] All error paths are handled — not just the happy path
- [ ] No off-by-one errors, null dereference risks, or race conditions
- [ ] Functions return correct types and shapes in all branches

## Contracts (if epic context provided)
- [ ] Every REQUIRES block from the epic contracts appears as a guard clause / input validation
- [ ] Every ENSURES block is satisfied — postconditions hold after operations complete
- [ ] State machine transitions are guarded — invalid transitions return an error
- [ ] No field is stored in two different representations (e.g., both `item_ids` and `item_names`)

## Formal Model Conformance (if formal models exist in models/ directory)
- [ ] Every state machine transition in the model has a corresponding implementation path
- [ ] Every invalid transition in the model is explicitly rejected (negative test exists)
- [ ] Every REQUIRES precondition has a guard clause in the implementation code
- [ ] Every ENSURES postcondition is verified by at least one test
- [ ] No new states or transitions exist that aren't in the model — if the implementation introduces new behavior, the model should be updated first
- [ ] Model-derived tests (tagged `# DERIVED: model`) are adapted to the actual API, not just copied verbatim from the skeleton

## Consistency
- [ ] If multiple screens or endpoints read the same data, they use the same query / data source
- [ ] Enum values, status strings, and field names match across layers (API, DB, UI)
- [ ] Error messages and success toasts accurately describe what happened — no copy-paste from other contexts

## Security
- [ ] No hardcoded secrets, tokens, or credentials
- [ ] Inputs are validated at trust boundaries (user input, external API responses)
- [ ] Authentication and authorisation checks are present where required
- [ ] No SQL injection, XSS, or command injection vectors

## Null Safety
- [ ] What happens when any field in an API response is null, missing, or empty?
- [ ] Arrays are checked for null/undefined before `.map()`, `.filter()`, etc.
- [ ] Optional fields have explicit fallbacks — no silent `undefined` propagation

## Testing
- [ ] Tests assert behaviour, not implementation details
- [ ] At least one error-path test exists for each operation
- [ ] Tests would fail if the feature were removed (not just executing code without meaningful assertions)
- [ ] Property-based tests exist for pure functions / data transformations (if applicable)

## Operational Safety
- [ ] Operations that depend on external services (email, payment, SMS) check service availability before reporting success
- [ ] Failure of a non-critical dependency degrades gracefully — does not crash the operation
- [ ] Capacity / rate limits are respected

## Output Format

For each section, output:

```
### [Section Name]
- PASS: [item summary]
- FAIL: [item summary] — [one-line justification]
- N/A: [item summary]
```

End with an overall verdict: `CHECKLIST: ALL_PASS` or `CHECKLIST: HAS_FAILURES (N items)`

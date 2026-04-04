# Structured Review Checklist

Score each item: **PASS**, **FAIL** (with one-line justification), or **N/A**.

## Correctness
- [ ] Every acceptance criterion from the ticket has at least one test that verifies it
- [ ] All error paths are handled — not just the happy path
- [ ] No off-by-one errors, null dereference risks, or race conditions
- [ ] Functions return correct types and shapes in all branches

## Contracts (if epic context provided)
- [ ] Every REQUIRES block from the epic contracts appears as a guard clause / input validation
- [ ] Every ENSURES block is satisfied — postconditions hold after operations complete
- [ ] State machine transitions are guarded — invalid transitions return an error
- [ ] No field is stored in two different representations (e.g., both `pet_ids` and `pet_names`)

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

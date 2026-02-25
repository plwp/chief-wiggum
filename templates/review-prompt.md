# Code Review Request

You are reviewing a code change for a pull request. Provide actionable feedback.

## Context

**Ticket**: {{TICKET_TITLE}}
**Description**: {{TICKET_DESCRIPTION}}
**Acceptance Criteria**:
{{ACCEPTANCE_CRITERIA}}

## Diff

```diff
{{DIFF}}
```

## Instructions

Review this diff and provide feedback in the following categories:

### Correctness
- Does the implementation satisfy all acceptance criteria?
- Are there logic errors, off-by-one mistakes, or missing edge cases?
- Are error paths handled?

### Security
- Any injection risks (SQL, XSS, command)?
- Secrets or credentials exposed?
- Input validation at system boundaries?

### Performance
- N+1 queries or unnecessary loops?
- Missing indexes for new query patterns?
- Large allocations or memory leaks?

### Maintainability
- Is the code clear without excessive comments?
- Are names descriptive?
- Is complexity justified?

### Testing
- Are the tests sufficient for the changes?
- Are edge cases covered?
- Are tests deterministic?

## Output Format

For each issue found:
1. **File:Line** - Description of the issue
2. **Severity**: critical | warning | suggestion
3. **Fix**: What should be done

End with a summary: APPROVE, REQUEST_CHANGES, or COMMENT.

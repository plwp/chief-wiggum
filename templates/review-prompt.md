# Code Review Request

You are reviewing a code change for a pull request. Be thorough, but keep the output high-signal and structured.

## Context

**Ticket**: {{TICKET_TITLE}}
**Description**: {{TICKET_DESCRIPTION}}
**Acceptance Criteria**:
{{ACCEPTANCE_CRITERIA}}

## Diff

```diff
{{DIFF}}
```

## Review Standard

Review the diff for:
- correctness and behavioural regressions
- security, auth, permission, and validation risks
- performance problems introduced by the change
- maintainability issues that are likely to cause defects or make the change unsafe to evolve
- testing gaps, flaky tests, or assertions that do not actually prove the behaviour

Be thorough, but avoid filler:
- do not include praise, summaries of what the diff does, or style-only comments
- do not invent issues without a plausible failure mode
- if a concern is speculative, you may include it, but mark it clearly as an inference with lower confidence

For each finding, separate what is directly supported by the diff from what you are inferring.

## Output Format

If you find no meaningful issues, output exactly:

`NO_FINDINGS`

Otherwise, output one finding per section using this format:

### Finding N
- **Title**: Short bug-oriented label
- **Priority**: P0 | P1 | P2 | P3
- **Confidence**: high | medium | low
- **File**: `path/to/file.ext[:line]`
- **Category**: correctness | security | performance | maintainability | testing
- **Evidence**: What in the diff directly supports this concern
- **Inference**: What you are inferring, if anything. Use `none` if the concern is directly shown by the diff
- **Issue**: Describe the concrete problem and why it matters
- **Failure scenario**: Explain how this would break in practice
- **Fix**: State the smallest reasonable fix

After the findings, end with:

`VERDICT: REQUEST_CHANGES`

If the diff is acceptable but you still have only minor non-blocking notes, use:

`VERDICT: COMMENT`

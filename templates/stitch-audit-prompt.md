# Stitch Audit — Semantic Analysis

You are reviewing a cross-layer data flow audit for the **{{KEYWORD}}** feature in **{{REPO}}**.

## Extraction

The following schemas were extracted across the full stack (frontend forms, API handlers, database operations, admin views):

```json
{{EXTRACTION_JSON}}
```

## Diff Report

Automated boundary diffing found these mismatches:

```
{{DIFF_REPORT}}
```

## Git Provenance

For BREAK/WARN findings, here is the git history showing how each side was introduced:

```json
{{PROVENANCE}}
```

## Your Analysis

Go beyond what regex diffing can detect. Look for:

1. **Lost intent** — Is the semantic meaning of a field shifting across boundaries? e.g. a field called `interests` on the form becomes `tags` in the DB, losing the user-facing meaning.

2. **Dead data paths** — Data collected from users but never displayed, queried, or used downstream. This wastes user effort and may create GDPR liability.

3. **Validation gaps** — Frontend allows values that the backend will reject (or vice versa). Look at Zod validators vs Go struct validators vs MongoDB schema constraints.

4. **Convention drift** — Does this feature follow the same naming/typing patterns as the rest of the codebase? Flag divergences that will confuse future developers.

5. **Completeness** — Fields that *should* exist based on the feature's purpose but don't appear anywhere. Think about what a user would expect from this feature.

6. **Provenance interpretation** — Based on the git trail, explain *how* these breaks likely happened. Was it AI context drift (same PR, different passes)? Separate work streams? A refactor that missed one layer?

## Output Format

Structure your response as:

### Summary
One paragraph: overall health of this feature's data flow.

### Critical Findings
For each issue, state:
- **What**: The specific mismatch or gap
- **Why it matters**: User impact, data loss risk, or maintenance burden
- **How it likely happened**: Based on provenance data
- **Suggested fix**: Concrete action (not "review this")

### Observations
Lower-severity patterns or suggestions that don't need immediate action.

### Verdict
One of: CLEAN | NEEDS_FIXES | BROKEN
With a one-line justification.

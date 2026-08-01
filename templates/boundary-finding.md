# Boundary finding: {id}

**To the owning team.** This finding was surfaced by an automated debt
inventory run scoped to a *different* domain of this repository. Every
location it names falls outside our adopted scope, so it is yours to triage —
we are reporting it, not fixing it.

## Finding

- **ID**: `{id}` (stable, content-anchored — the same finding keeps this ID
  across runs; it disappears from the inventory when fixed)
- **Engine / kind**: `{engine}` / `{kind}`
- **Severity** (mechanical rubric): {severity}
- **Detail**: {detail}

## Evidence

Locations (at target SHA `{target_sha}`):

{locations}

## Why we are not fixing this

Our adopted scope is: {scope}

Per the domain authority split (chief-wiggum#213), findings whose every
location is outside our scope are **never ticketed into our remediation
epics and never auto-fixed** — a fix from outside the owning team pollutes
your churn history and produces diffs you have no reason to trust. This
issue is the hand-off.

## Suggested next step

Re-run the inventory scoped to your domain to confirm, then triage: fix,
waive with an expiry, or close with a reason.

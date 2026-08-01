# Pattern: Feature Entitlements Read-Model

- **Category:** saas-infra
- **Trust class:** the resolver and the tier ceiling are billing-adjacent authorization — a protected path
- **Status:** specified (spec complete; `scaffold/` not yet built)
- **Depends on:** [`entitlement-overlay`](../entitlement-overlay) — reuses its mined override-over-tier layering discipline
- **Feeds:** [`tiered-subscription`](../tiered-subscription) gates, [`frictionless-onboarding`](../frictionless-onboarding) and UI (both query it), [`self-serve-billing-portal`](../self-serve-billing-portal) (subscription state degrades through it)

## What it is

**One resolver that answers "what can this account do,"** consumed identically
by backend gates and the frontend. It derives per-key capability flags from
tier + per-account overrides + an explicit grandfather stamp, and it is the
read-model everything else queries — onboarding, UI affordances, API guards,
billing degrade.

Why a pattern and not "just check the plan field": the failure mode is
**scatter**. Audits of shipped production SaaS found three disjoint flag
systems in one app (env-derived deployment capabilities, a tenant JSON flag
bag, a standalone opt-in boolean) with no code path consulting more than one;
a partial override that zeroed every absent key (whole-struct replacement, so
"turn one thing off" silently turned everything else off); and an unknown
account silently resolving to a *default tenant's* capabilities. Each observed
failure is an invariant below, inverted.

## When to apply

- More than one surface (API guards, UI, onboarding) needs "can this account
  do X" and must agree.
- Tiers exist and per-account exceptions (overrides, grandfathering) are real.
- Subscription state (past_due, canceled) must degrade capabilities somewhere
  other than a dashboard badge.

## Mechanism — generic components

- **Single resolver.** Exactly one implementation resolves entitlements;
  backend gates call it and the frontend consumes its served output. Parallel
  derivations (a second flag table in another language) are the drift
  mechanism the pattern exists to kill. *(INV-FE-001.)*
- **Per-key layering.** Each capability resolves as tier-base ← account
  override ← grandfather stamp, **per key**: an override that sets one key
  never resets absent keys to zero values. The `entitlement-overlay` pattern
  realizes this per-field layering shape in its mined, raise-only form; the
  bidirectional form here (an override may also narrow, under INV-FE-006's
  ceiling) is design-derived. *(INV-FE-002.)*
- **Fail-closed on unknown principal.** An unknown or missing account resolves
  to *no capabilities* (or an explicit error) — never to a default account's
  capabilities, never to permissive defaults. *(INV-FE-003.)*
- **Explicit grandfathering.** Legacy accommodation is a dated, per-account
  stamp (`grandfathered_until`, plan-version pin) — never an implicit
  "absent config means everything enabled" default. *(INV-FE-004.)*
- **Deployment capability ≠ entitlement.** What the *deployment* can do
  (providers configured, email wired) and what the *account* may do are
  separate documents; the entitlement read is authenticated and
  account-scoped, and its client-side failure posture is fail-closed (a
  deployment-capability read may fail-open for display purposes only).
  *(INV-FE-005.)*
- **Tier ceiling at write time.** Self-serve settings writes cannot exceed
  what the tier grants: an account override may narrow, only sanctioned admin
  paths may widen, and every entitlement change is audited (actor, before,
  after). *(INV-FE-006.)*

## Grounding

The per-field layering *shape* in INV-FE-002 is inherited from the in-repo
[`entitlement-overlay`](../entitlement-overlay) cluster (INV-EO-001..003),
which is mined from a shipped production app's plan resolver — but that mined
form is **raise-only** (`effective = max(base, overlay)`), so the bidirectional
form this pattern requires (an override may also narrow, under INV-FE-006's
ceiling) is design-derived, and the whole cluster (INV-FE-001..006) is marked
**design-derived** accordingly. Each invariant inverts a concrete failure mode
observed while auditing shipped production SaaS (scattered flag systems;
whole-struct override replacement; silent default-tenant fallback; implicit
permissive-default grandfathering; a public unauthenticated capability
endpoint with fail-open client defaults; self-serve writes with no tier
ceiling). Per the registry's provenance policy, private-repo paths are held
out of the public registry. These invariants ground fully when a product
first builds the resolver.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `tier_matrix` | yes | The per-tier capability base (keys → grants/limits). |
| `override_store` | yes | Where per-account overrides live (sanctioned write path). |
| `grandfather_stamp` | yes | The dated per-account legacy pin the resolver honors. |
| `served_endpoint` | yes | The authenticated, account-scoped read the frontend consumes. |
| `audit_sink` | yes | Where entitlement changes are recorded (actor, before, after). |
| `degrade_map` | no | How subscription states (past_due, canceled) narrow the resolved set. |

## Success metrics

`entitlement_check_sources` = 1 (no parallel derivations), `frontend_backend
disagreements` = 0, `unknown_principal_grants` = 0, `overrides_exceeding_tier`
= 0, `unaudited_entitlement_changes` = 0.

## Trust

The resolver, the tier matrix, the override write path, and the degrade map
are billing-adjacent authorization — protected paths. A worker widening a
grant or adding a second resolution path is parked for human review.

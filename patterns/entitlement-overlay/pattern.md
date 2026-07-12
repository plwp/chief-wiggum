# Pattern: Floor-Raising Entitlement Overlay (admin comps)

- **Category:** saas-infra (monetization)
- **Trust class:** the overlay writer + the effective-limits seam are protected paths
- **Status:** specified (spec complete; `scaffold/` not yet built — see [chief-wiggum#135](https://github.com/plwp/chief-wiggum/issues/135))

## What it is

The safe way to grant a customer *more* than their plan pays for — a support comp,
a beta allowance, a bespoke enterprise carve-out — **without** touching the billing
system. An admin-granted **overlay** is stored locally, orthogonal to the payer
tier; effective limits are a per-field `max(payer, overlay)`. It is **grant-only**
(never lowers a payer), and the billing reconcile recomputes the max on every
webhook, so a comp can never be clobbered by a routine subscription sync.

Why a pattern and not "just bump their plan": bumping the plan lies to the billing
system (they're now billed as if they upgraded) and can't express *bespoke* limits
a plan doesn't have. The overlay keeps billing honest — the subscription keeps
charging exactly what it charged — while granting arbitrary per-field caps. The
correctness is a small cluster where one gap silently either overcharges a customer
or lets a comp evaporate on the next webhook.

> **The design choice this encodes** (a deliberate one): a flexible **in-house
> override** that can grant custom limits a third-party coupon can't express, over
> offloading to the billing vendor's discount primitive — even though the vendor
> path is simpler. The overlay is *orthogonal* to billing, so "comped **and** still
> billing" is a normal, expected state.

## When to apply

Any plan-tiered SaaS (sits on [`tiered-subscription`](../tiered-subscription))
where support/sales need to grant individual accounts more capability than their
tier — comps, trials-beyond-the-trial, grandfathering, enterprise one-offs — without
mutating the billing subscription. Skip it if every entitlement change should go
through billing (then just change the plan).

## Mechanism — generic components

- **Overlay stored orthogonally to the payer tier.** The account persists its raw
  payer tier separately (e.g. `stripe_plan`) from its effective tier, plus overlay
  fields: `overlay_plan` + per-field `overlay_limits` + `reason`/`granted_by`/`at`.
- **Effective = per-field floor-raising `max`.** `EffectiveLimits(account)` computes
  each capability as `max(base_plan_limit, overlay_limit)` — the overlay can only
  ever *raise* a cap, never lower one. A downgrade or lapse on the payer side still
  can't drop the account below its comp.
- **Unlimited beats finite in BOTH directions.** The "unlimited" sentinel (`-1`)
  must compare as *greater* than any finite value — a naive numeric `max` reads
  `-1 < finite` and would silently **lower** an unlimited paid plan to a finite
  overlay. This one guard (`maxCap`) is the pattern's sharpest edge.
- **Grant-only writer.** The overlay-granting path accepts only upward grants (paid
  tiers, non-negative caps), **requires a reason**, writes an audit row, and is
  **blocked from impersonated and suspended sessions**. It cannot be used to take
  capability away.
- **Reconcile recomputes the max — comps survive billing syncs.** The billing
  reconcile ([`fetch-on-webhook-reconcile`](../fetch-on-webhook-reconcile)) writes
  `effective = max(payer_tier, overlay)` on every webhook, so a routine
  subscription sync **cannot revert a comp** (no comp-immunity flag needed — the
  max recompute is idempotent and grant-preserving).
- **One enforcement seam reads EFFECTIVE limits.** Every gate/quota check reads
  `EffectiveLimits(account)`, never the raw payer tier — the single integration
  point that makes the overlay real everywhere at once.

## Invariant cluster

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-EO-001` | Floor-raising: `effective(field) = max(base, overlay)` per field; overlay only ever raises. | dogeared-coach `INV-BIL-010`; `services/plan.go:257-305` |
| `INV-EO-002` | Grant-only writer: only upward grants (paid tiers, reason required, audited), blocked from impersonated/suspended sessions. | `INV-BIL-011`; `services/admin_provider.go:222-263` |
| `INV-EO-003` | Unlimited (`-1` sentinel) beats finite in BOTH directions, or a naive `max` silently lowers an unlimited plan. | `services/plan.go:307-320` (`maxCap`) |
| `INV-EO-004` | Orthogonal source of truth: payer tier persisted separately; reconcile recomputes `max(payer, overlay)` so a comp is never reverted. | `INV-BIL-009`; `services/billing_reconcile.go:271-275` |
| `INV-EO-005` | Single enforcement seam reads EFFECTIVE limits, not the raw payer tier. | `INV-ADM-010`; `services/plan.go EffectiveLimits` |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `payer_tier_field` | yes | Where the raw billed tier is persisted, distinct from effective. |
| `overlay_fields` | yes | Per-field caps the overlay can raise (`max_videos`, `minutes`, `clients`, flags…). |
| `unlimited_sentinel` | no (default `-1`) | The value that must compare as greater than any finite cap. |
| `grant_authority` | yes | Who may grant (admin identity); the writer is blocked from impersonated/suspended sessions. |
| `enforcement_seam` | yes | The single function all gates call for effective limits. |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `comp_reverts` | down | comps silently dropped by a billing sync (target 0; INV-EO-004 asserts it). |
| `overlay_downgrade_incidents` | down | an overlay that *lowered* a payer's cap (target 0; the `-1`-beats-finite guard). |
| `unaudited_grants` | down | overlay grants with no reason/audit row (target 0). |

## Relationship to other patterns

- **`tiered-subscription`** — the overlay raises the floor above its plan matrix;
  the effective-limits seam is `tiered-subscription`'s enforcement seam reading
  `max(payer, overlay)`.
- **`fetch-on-webhook-reconcile`** — the reconcile recomputes the max so a webhook
  can't revert a comp; the two share the projected entitlement field.
- **`elevated-access-session`** — the grant writer is blocked from impersonated
  sessions (an operator acting-as a user can't self-grant comps).
- Distinct from a generic **`feature-entitlements`** read-model: this is the
  narrower grant-only, monotonic-floor invariant that survives an orthogonal
  source-of-truth reconcile.

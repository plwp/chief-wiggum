# Pattern: Tiered Subscription

- **Category:** saas-infra (monetization)
- **Trust class:** entitlement changes are protected-path (goalpost-grade)
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The full **subscribe → enforce → lifecycle** machinery for a plan-tiered SaaS:
a single source-of-truth tier matrix, self-serve plan changes, billing-provider
webhooks as the authoritative subscription state, capability/quota enforcement at
every mutating action, and **graceful degradation** when a subscription lapses or
downgrades. Absorbs the earlier `tiered-saas-enforcement` candidate and adds the
lifecycle + degradation half.

## When to apply

Any product with more than one paid tier (or free + paid). The subtle parts —
authoritative state from webhooks not client redirects, never-destructive
downgrade enforcement, restrictive fallback for unknown tiers — are where most
implementations leak revenue or delete customer data, so it's worth stamping as
one coherent shape.

## Mechanism — generic components

Neutral throughout; tiers, provider, and policies are [parameters](#parameters).

- **Tier matrix as single source of truth.** One structure holds every tier's
  capabilities and limits (counts, per-item caps, quotas, capability flags). A
  `-1` **unlimited sentinel**; an **unknown/empty tier falls back to the most
  restrictive tier**, never silently granting a higher one.
- **Enforcement via injected gate seams.** `Allow…` / `Check…` methods exposed as
  injected interfaces into services *and* webhook handlers, so enforcement is
  testable without real infra. Check-before-act at each mutating action; the
  bounded check-before-act TOCTOU is documented and accepted, not papered over.
- **Stateless quota.** Usage is recomputed fresh from the source of truth on every
  check (no stored counters that drift), the **same computation path backs both
  the enforcement gate and the user-facing usage display**, and in-flight items
  are reserved pessimistically to close the sequential-overcommit gap.
- **Billing webhook is the source of truth.** Subscription state
  (`active` / `trialing` / `past_due` / `canceled`) is driven by **signed,
  idempotent provider webhooks**, never by the browser's post-checkout redirect
  (which can die mid-flow). Reuses the webhook-ingestion shape from
  `provider-neutral-adapter` (raw-body verify → replay window → normalize →
  idempotent apply).
- **Lifecycle transitions.** Subscribe / upgrade (effective immediately, proration
  delegated to the provider) / downgrade (effective at period end) / cancel
  (retain access until period end) / reactivate. Entitlement is **recomputed on
  each transition**, never hand-mutated.
- **Graceful, non-destructive degradation.** `past_due` → a **dunning grace
  window**: keep access, notify (see `transactional-email-and-dunning` candidate).
  `canceled` / expired → fall back to the **free tier or read-only**, and **never
  hard-delete data** — grandfather it and offer export. A downgrade that leaves the
  account over the new caps **blocks new creation but keeps existing items** — no
  destructive enforcement, ever.
- **Provider-neutral.** The billing provider sits behind
  `provider-neutral-adapter`, so it is swappable.

## Parameters

| Parameter | What it is |
|--|--|
| `tiers` + `matrix` | the capability/limit matrix per tier (`-1` = unlimited) |
| `billing_provider` | the provider (behind the neutral adapter) |
| `unknown_tier_fallback` | most-restrictive tier used for unknown/empty (default: free) |
| `dunning_grace_window` | how long `past_due` retains access before degrade |
| `downgrade_policy` | `block-new` (keep existing over-cap items) or `read-only` |
| `lapse_policy` | on cancel/expire: `free-tier` or `read-only`; never delete |

## Success metrics

Monetization health — the loop optimizes packaging/thresholds toward these
(admin-gated, since entitlement/pricing are protected-path):

| Metric | Goal | What it measures |
|--|--|--|
| `mrr` | ↑ | monthly recurring revenue / ARPU |
| `upgrade_rate` | ↑ | % of accounts moving to a higher tier |
| `voluntary_churn_rate` | ↓ | % cancelling per period |
| `dunning_recovery_rate` | ↑ | % of `past_due` accounts recovered before degrade |
| `revenue_leak` | ↓ | unpaid-access / wrong-tier-grant incidents (target 0; asserted by the gate) |

## Relationship to other patterns

- **Behind `provider-neutral-adapter`** for the billing vendor + its webhooks.
- **Consumed by `frictionless-onboarding`** — its limit-hit gate (a `409` from the
  enforcement seam) is the friction signal that triggers a contextual upgrade
  prompt.
- **Emits monetization signal for `improvement-loop`.** Limit-hit events,
  upgrade/downgrade transitions, and dunning outcomes are exactly the signal the
  loop can optimize (packaging, thresholds, prompt timing). Because entitlement /
  pricing / paywall definitions are **goalpost-grade** (a wrong change is a direct
  revenue or trust bug) they belong in the **protected pathset** — the loop may
  *propose* changes to them, but they route through park-and-notify or, when the
  proposal is driven by end-user behavioral signal, the **blocking admin-approval**
  quarantine (see the [improvement-loop trust model](../improvement-loop/pattern.md#trust-model)).

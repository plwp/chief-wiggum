# Pattern: Frictionless Onboarding (free → upgrade)

- **Category:** monetization (growth process-loop)
- **Trust class:** conversion changes are end-user-signal-driven → admin-gated in the loop
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

Minimize **time-to-value** on a free tier, then convert to paid at natural
friction points — self-serve, no sales touch. The design goal is that a new user
reaches a real, complete unit of value *before* being asked for money, and the
upgrade ask arrives *at the moment the user feels the ceiling*, not as a blanket
nag.

This is the mini-SaaS pattern **most improved by the [improvement
loop](../improvement-loop/pattern.md)**: activation and conversion rates are its
optimization target. It only works if the funnel is instrumented — so it composes
directly with [`engagement-instrumentation`](../engagement-instrumentation/pattern.md).

## When to apply

Any product-led-growth SaaS with a free (or trial) tier converting to paid. Not
for sales-led / enterprise-only products where onboarding is a human process.

## Mechanism — generic components

- **Value-first free tier.** No credit card to start. The free tier delivers a
  **real, complete unit of value**, not a crippled demo. Time-to-first-value
  (TTFV) is the north-star metric — everything below serves shrinking it.
- **Activation instrumentation.** Define the **"aha" activation event** (the first
  moment of real value) and instrument the funnel `signup → activation → habit →
  upgrade` via `engagement-instrumentation`, each step **trust-tagged**. You cannot
  improve a funnel you can't see.
- **Progressive disclosure.** Ask for the minimum up front; defer account/config
  friction; collect more only when a feature actually needs it. Every required
  field before activation is a drop-off risk.
- **Just-in-time contextual upgrade prompts.** The upgrade ask is triggered by
  **friction signals** — a limit-hit (`409` from the `tiered-subscription`
  enforcement seam), a tap on a locked capability, approaching a quota — **not**
  time-based nags. The prompt is shown *in context* and the upgrade path is
  one-click self-serve into the `tiered-subscription` flow.
- **Reverse trial (optional).** Start new users in a **time-boxed premium**
  experience, then fall back to free — converting on loss-aversion once they've
  felt the ceiling from above. The elevated entitlement is time-boxed with a
  server-enforced TTL (same "independent TTL, fail-closed expiry" discipline as
  the `elevated-access-session` candidate), so the trial can't be extended
  client-side.
- **Recovery loops.** If a user signs up but doesn't activate, a nudge sequence
  fires; failed upgrade payments enter dunning. Both run through
  `transactional-email-and-dunning` (candidate) — idempotent, provider-neutral.

## Parameters

| Parameter | What it is |
|--|--|
| `activation_event` | the instrumented "aha" moment (the funnel's success step) |
| `free_tier_value_unit` | the complete unit of value the free tier delivers |
| `upgrade_triggers` | the friction signals that surface a contextual prompt |
| `reverse_trial_length` | optional time-boxed premium window (omit to disable) |
| `nudge_cadence` | activation/re-engagement email schedule |

## Success metrics

The conversion funnel — this pattern's whole reason to exist, and the loop's
optimization target for it (admin-gated):

| Metric | Goal | What it measures |
|--|--|--|
| `activation_rate` | ↑ | % of signups reaching the aha activation event |
| `time_to_first_value` | ↓ | TTFV: signup → first real value |
| `free_to_paid_conversion` | ↑ | % of free users upgrading to paid |
| `prompt_conversion` | ↑ | contextual upgrade-prompt → upgrade rate |
| `reverse_trial_conversion` | ↑ | % converting after the reverse-trial window (if enabled) |

## Relationship to other patterns

- **Consumes `tiered-subscription`** — the limit-hit gate is the primary upgrade
  trigger; the one-click upgrade drops into its lifecycle.
- **Composes with `engagement-instrumentation`** — the activation/conversion funnel
  *is* engagement signal, captured with the same server-trusted, trust-tagged
  discipline.
- **The through-line to `improvement-loop`.** This pattern is where "re-used **and
  improved on**" is most literal: the loop can propose changes to prompt copy,
  trigger thresholds, nudge timing, and reverse-trial length, scored against the
  conversion funnel. But because those proposals are **driven by end-user
  behavioral signal** (an injection/gaming surface) and touch **paywall/pricing
  entitlements** (goalpost-grade), they are exactly the changes the loop's
  [trust model](../improvement-loop/pattern.md#trust-model) routes to **blocking
  admin approval** — never silent auto-deploy. The pattern is continuously
  optimized, but a human always signs off on how the product asks for money.

# Binding: `tiered-subscription` → Stripe on GCP Serverless SaaS

- **Realizes:** [`tiered-subscription`](../../../tiered-subscription) (vendor-neutral spec)
- **Tier:** T2 · **Vendor:** Stripe (`stripe-go/v82`) · **Source:** `dogeared-coach`

The concrete Stripe wiring for the abstract subscribe → enforce → lifecycle
pattern. The abstract spec says "billing webhook is the source of truth"; this is
*how*, with the sharp edges that actually bit.

## Surface

- **Hosted Checkout Sessions** (subscription mode) for upgrades to paid plans; one
  Stripe **Price** per paid tier (`STRIPE_PRICE_<TIER>`). Checkout in a
  `billing_checkout` service; plan/price config in a `config/stripe` module.
- **Customer Portal** for self-serve plan/payment management (realizes the
  `self-serve-billing-portal` candidate — portal session minted server-side).
- **Stripe Tax** for jurisdiction tax + invoices (optional).

## The non-obvious glue (get these wrong and you leak revenue or corrupt state)

1. **Webhook is the *single entitlement writer*; checkout never writes entitlement.**
   `POST /api/v1/webhooks/stripe` is registered **outside** auth/tenant middleware.
   The handler does **fetch-on-webhook reconcile**: the event is only a *trigger* —
   it re-fetches the live subscription from Stripe and projects entitlement from
   *that*, never from the event payload or the post-checkout redirect (which can die
   mid-flow). One writer = no split-brain between "checkout says paid" and "webhook
   says paid".
2. **Verify against a rotation *list* of secrets.** `ConstructWebhookEvent` checks
   the signature against **`STRIPE_WEBHOOK_SECRETS` (comma-separated)**, not a single
   secret — so you can rotate the signing secret with zero downtime (accept old+new
   during the overlap).
3. **`livemode` guard.** Assert `event.livemode == config.Livemode()` and **400 on
   mismatch**, so a test-mode event can never mutate live subscription state (and
   vice-versa).
4. **Idempotent dedupe.** A claim→processed state machine keyed on `stripe_event_id`
   — Stripe retries deliveries; each event applies at most once.
5. **At-most-one customer per account.** The invariant you actually need is
   **one account → one Stripe customer**, so the durable guard is a **unique
   constraint / CAS on `(provider, account_id)`**, plus `account_id` written into the
   Stripe customer **metadata** for reverse mapping. The Stripe **idempotency key**
   (`provider:{account_id}:customer`) stops a *retry* from duplicating but is not a
   permanent data invariant; a unique index on `stripe_customer_id` only prevents the
   *reverse* (one customer → many accounts) — necessary but **not sufficient**. On
   CAS failure, delete the orphan customer you just minted. Spell out the constraint
   per datastore (Mongo partial unique index vs a Firestore transaction on a
   deterministic doc id).
6. **Test/live safety at boot.** `STRIPE_MODE` defaults to `test`; only exact
   `"live"` enables live. `ValidateStripeConfig` at startup asserts the key segment
   (`_test_` / `_live_`) matches the mode **and** every paid plan has a price, else
   `log.Fatalf`. Fail-fast beats a silently-misconfigured prod.
7. **Degrade, don't crash, when unconfigured.** If Stripe env is absent the client
   is nil, billing routes return **503**, and the server still boots — so a T1
   deploy without billing is a valid state.

## Enforcement + limits

Plan caps live in one `plan.LimitsFor(tier)` function (the abstract spec's "tier
matrix as single source of truth"); a limit-hit returns a **`409`**, which is the
friction signal [`frictionless-onboarding`](../../../frictionless-onboarding)
consumes for a contextual upgrade prompt. Unknown/empty tier → most-restrictive
fallback (free), never a silent grant.

## Trust & protected paths

Entitlement, price, and paywall definitions are **goalpost-grade** — register
`config/stripe`, `plan.go`, and the webhook handler as **protected paths**.

**Signature ≠ full trust.** A verified signature proves the event *came from Stripe*
(transport origin) — it does **not** make every field trustworthy. The **signed
vendor state** (subscription status, price id, current-period-end — which is exactly
why you re-fetch it) is `trusted`; but **customer-controlled fields carried in the
event** (customer `email`, `metadata`, `cancellation_reason`, and — under Connect —
the connected-account context) are **`untrusted`** and must be validated, never used
to decide entitlement or routed to the loop as trusted signal. Concretely: verify
the event's **account/context matches the account you expect**, use
**endpoint-specific signing secrets**, keep Stripe's default timestamp tolerance
(replay window), and optionally IP-allowlist Stripe's ranges alongside the signature.
With that split, loop changes *driven by verified billing state* are park-and-notify;
anything derived from a customer-controlled field is `untrusted` → quarantine — and
changes to the *pricing/matrix* files are always human-gated regardless of provenance.

## Cost

Stripe has **no monthly fee** — pure per-transaction (~2.9%+ + fixed; AU domestic
≈ 1.7% + A$0.30). It is a linear variable cost from transaction #1, never a cliff.
Customer Portal and webhooks are free.

## Stand it up

See [`skills/stripe-subscriptions-setup.md`](../skills/stripe-subscriptions-setup.md).

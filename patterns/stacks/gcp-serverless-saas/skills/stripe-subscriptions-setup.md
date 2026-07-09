# Skill: Stand up Stripe subscriptions (Checkout + Portal + webhook)

Realizes [`tiered-subscription`](../bindings/tiered-subscription.md) on this stack.
Outcome: users upgrade via hosted Checkout, self-serve via the Customer Portal, and
**the webhook is the single entitlement writer** via fetch-on-webhook reconcile.

> Bind first: the tier list + one Stripe **Price** per paid tier; `STRIPE_MODE`
> (`test`|`live`); the datastore for the entitlement projection.

## 1. Products & prices (test mode first)

```bash
stripe products create --name "Coach"
stripe prices create --product <prod_id> --unit-amount 2900 --currency aud \
  --recurring[interval]=month
# repeat per paid tier; store price ids as STRIPE_PRICE_<TIER>
```

## 2. Secrets (never in env/repo — see secret-manager-setup)

Store `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRETS` (comma-separated to allow
rotation) in Secret Manager; grant the **runtime** SA `secretAccessor` (the
**deployer** SA must not have it).

## 3. Checkout (server mints the session)

Create a Checkout Session in `subscription` mode with the tier's price, a
`client_reference_id` = your account id, `success_url`/`cancel_url`. **Do not** write
entitlement on the success redirect — it can die mid-flow.

## 4. Customer Portal

Enable the Portal in the Stripe dashboard; mint a portal session server-side
(`stripe.billingPortal.sessions.create`) so users manage plan/payment without
support. (Realizes the `self-serve-billing-portal` candidate.)

## 5. Webhook — the single entitlement writer

Register `POST /api/v1/webhooks/stripe` **outside** auth/tenant middleware. In the
handler:

```
1. Read the RAW body; verify signature with ConstructWebhookEvent against EACH
   secret in STRIPE_WEBHOOK_SECRETS (rotation list) — accept if any matches.
2. Guard: if event.livemode != config.Livemode() -> 400 (test can't touch live).
3. Idempotency: claim stripe_event_id (claim->processed); if already processed, 200.
4. FETCH-ON-WEBHOOK: ignore the payload's snapshot; re-fetch the live subscription
   from Stripe and project entitlement from THAT. This handler is the ONLY writer
   of entitlement. Trust the re-fetched VENDOR state; treat customer-controlled
   fields (email, metadata, cancellation_reason, Connect account context) as
   UNTRUSTED — validate the account/context matches the account you expect.
5. At-most-one customer: durable unique constraint / CAS on (provider, account_id)
   (+ account_id in Stripe customer metadata); the idempotency key only dedupes
   retries. On CAS loss, delete the orphan customer.
```

Register the webhook endpoint + subscribe to
`checkout.session.completed`, `customer.subscription.*`, `invoice.payment_failed`:

```bash
stripe listen --forward-to localhost:8080/api/v1/webhooks/stripe   # local dev
# prod: add the endpoint in the dashboard; copy its signing secret into the rotation list
```

## 6. Boot-time validation (fail fast)

On startup assert: key segment (`_test_`/`_live_`) matches `STRIPE_MODE`; every paid
tier has a price; if Stripe env absent → billing routes return **503** but the
server still boots (valid T1 state).

## Verify

- `stripe trigger checkout.session.completed` → entitlement appears **only** after
  the webhook, never from the redirect.
- Fire a **test-mode** event at a **live**-configured handler → **400** (livemode
  guard holds).
- Replay the same event twice → applied once (idempotency holds).
- Rotate: add a second secret to the list, flip Stripe's signing secret, confirm no
  missed events, then drop the old one.

## Gotchas

- Writing entitlement in the success_url handler = split-brain when the redirect
  dies. Webhook-only.
- Single webhook secret = downtime on rotation. Use the list.
- Trusting the event payload's subscription snapshot = stale on out-of-order
  delivery. Re-fetch.

# Binding: `transactional-email-and-dunning` → Resend on GCP Serverless SaaS

- **Realizes:** [`transactional-email-and-dunning`](../../registry.json) (candidate; vendor-neutral)
- **Tier:** T1+ · **Vendor:** Resend · **Source:** `booking-forms`, `dogeared-coach`
- **Honesty flag:** the **transactional half is shipped**; the **dunning half is
  aspirational** — no mined app built a failed-payment sequence (Stripe sends the
  billing emails). This binding describes what shipped and marks the gap.

## Transactional email (shipped)

- **Client:** Resend, either **raw HTTPS** (`POST https://api.resend.com/emails`,
  no SDK — dgrd) or the **`resend-go` SDK** (booking-forms). API key from
  `RESEND_API_KEY` in Secret Manager.
- **Emails that shipped:** single-use **invite** links (7-day expiry token) for
  onboarding providers/clients (dgrd); **booking notification + customer
  confirmation** sent synchronously in the request handler (booking-forms).
- **Templating:** hand-rolled Go `fmt.Sprintf` HTML — **no template engine**. Per-
  tenant branding (banners, hours, from-address) from a config map.
- **Injection hygiene (important):** `html.EscapeString` on all interpolated values
  **plus CRLF/header sanitization**, because names/fields can be *provider-* or
  *user-controlled* — an unescaped name is an HTML-injection and header-injection
  vector. Anti-autoreply headers (`Auto-Submitted`, `X-Auto-Response-Suppress`) to
  stop bounce loops.
- **From address:** `RESEND_FROM` (e.g. `no-reply-<tenant>@bookings.<domain>`).
- **Dev fallback:** a `logSender` that **logs the accept URL instead of sending**,
  so local/CI never hits Resend and you can still click the link.

## Dunning (aspirational — the intended shape)

Not yet built in any mined app; specified here so the factory knows the target
rather than inventing wiring. When added, follow the vendor-neutral spec:

- **Idempotent, send-once keys** — a `(account, email_kind, period)` key so retries
  and concurrent workers never double-send.
- **Failed-payment sequence** — driven off the Stripe `past_due` webhook (the
  `tiered-subscription` binding already surfaces it): a bounded retry/notify
  cadence within the `dunning_grace_window` before graceful degrade.
- **Lifecycle nudges** — welcome, activation nudge (fires if a signed-up user hasn't
  hit the activation event), re-engagement. These need a **scheduler**, which this
  stack does not yet provision (see the stack's known gaps) — the natural pairing is
  a Cloud Scheduler → PSK-gated Cloud Run endpoint.

## Trust

Recovery/nudge outcomes are **retention/conversion signal** for the loop. The email
*content templates* that touch conversion (upgrade nudges, dunning copy) are
adjacent to pricing/paywall, so treat copy changes proposed from **end-user signal**
as admin-gated, consistent with the monetization patterns.

## Cost

Resend free tier is **3,000 emails/mo (100/day)** — at ~2 emails per event this is
typically the **first free-tier ceiling** a T1 app hits (~50 events/day). Above it,
~$20/mo flat for 50k. Synchronous send at T1 means email latency is in the request
path (watch the handler timeout); move to async once you provision a queue.

## Stand it up

Resend setup is a subset of [`secret-manager-setup.md`](../skills/secret-manager-setup.md)
(store `RESEND_API_KEY`) + verifying a sending domain (SPF/DKIM) in the Resend
dashboard; no dedicated skill until the dunning scheduler lands.

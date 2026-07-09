# Binding: `provider-neutral-adapter` → Go interface + webhooks (Cloudflare Stream exemplar)

- **Realizes:** [`provider-neutral-adapter`](../../registry.json) (candidate; vendor-neutral)
- **Tier:** T2 · **Vendor:** Cloudflare Stream (exemplar) · **Source:** `dogeared-coach`

The concrete shape of "put a swappable vendor behind a seam". Mined from the video
integration, but the *same shape* is how this stack wraps Stripe, GCS, and any
external service — so it's the meta-binding the others lean on.

## The seam

The vendor lives entirely behind a **Go interface** (`video/` package:
`cloudflare.go`, `jwt.go`, `webhook.go`, `service.go`). The rules that make it
genuinely swappable:

- **Neutral DTOs only** cross the seam; the **concrete vendor types are unexported**
  → the compiler enforces that nothing outside the package depends on the vendor.
- **Vendor states are mapped to internal enums** with safe defaults (an unknown
  vendor status maps to a conservative internal state, never a permissive one).
- Only a **neutral id** (`video_id`) is stored — never a vendor URL or signed asset.

## Three reusable mechanisms behind the seam

1. **Direct browser → CDN upload.** The server mints a **one-time upload URL**; the
   browser uploads **straight to the vendor**, never proxying bytes through Cloud
   Run (which would blow the request timeout and egress budget). Cloud Run only
   brokers the ticket.
2. **Per-request signed access, nothing stored.** Playback and thumbnail access are
   **short-lived signed JWTs minted per request** (time-bound), signed with a key in
   Secret Manager. Only the `video_id` is persisted; a leaked DB row grants nothing.
3. **Webhook as source of truth, idempotent.** The vendor's **signed webhook**
   (`*_WEBHOOK_SECRET`) reconciles upload/processing status, so the app's record
   matches reality even if the browser dies mid-upload — this is what prevents
   **billing-ghost orphans** (assets you pay to store but the app forgot about).

## The same shape, other vendors on this stack

- **Stripe** sits behind the same discipline (config module, webhook-as-truth) —
  see [`tiered-subscription.md`](tiered-subscription.md).
- **GCS thumbnails**: a private bucket (uniform bucket-level access) served via
  **V4 signed URLs** signed by the Cloud Run service account (`signBlob` IAM) — the
  storage equivalent of per-request signed access.

## Trust & meta-disciplines

This binding is where several registry
[meta-disciplines](../../../../docs/patterns-registry.md) become concrete:
*external state is authoritative via signed webhooks, not the client redirect*;
*idempotency + guarded updates for anything touching external state*; *injected
interface seams for every gate*. The webhook is a **trusted, self-describing
signal** (the vendor reporting its own state, signature-verified) — so it stays
`trusted` even in a public app. Register the vendor config + webhook handler as
**protected paths**.

## Cost

The seam is free; the vendor is the cost. For Cloudflare Stream the only
usage-driven cost is **storage ($5/1,000 min stored/mo) + delivery ($1/1,000 min
delivered/mo)** — **delivery is the one uncapped variable cost in the whole stack**,
so bind it to per-plan caps (e.g. storage-minutes + client count per tier) and set a
soft alert. Encoding/ingest is free.

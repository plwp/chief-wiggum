# Pattern: Provider-Neutral Port/Adapter

- **Category:** multi-provider (the swappability seam)
- **Trust class:** the seam boundary is a protected path (a vendor type leaking across it is a design regression)
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The seam that keeps an external vendor (a video CDN, a billing provider, an email
sender) **swappable** — and, just as importantly, keeps its failure modes and
security-sensitive flows contained. Above the seam the app speaks only
vendor-neutral DTOs; below it, exactly one file touches the vendor SDK. It's the
port/adapter shape hardened for the specifics that actually bite in production:
signed-webhook-as-truth, sign-per-request tokens, and direct browser→CDN upload.

Why a pattern and not "just define an interface": the value is in the invariants
that make the seam *real* rather than nominal. An interface with the vendor's
types leaking through its DTOs isn't swappable. A concrete adapter that's exported
lets callers bypass the seam. A webhook that trusts the client redirect instead of
the signed payload isn't a source of truth. Each is a quiet way the seam rots; the
cluster below closes each one structurally.

## When to apply

Any dependency on an external service you might swap, or whose credentials/failure
modes you want contained behind one boundary — billing, video/media, email, SMS,
object storage, search. It's a **dependency of** the money- and media-path patterns
(`fetch-on-webhook-reconcile`, `tiered-subscription`, `elevated-access-session`,
`multi-tenant-isolation` all sit behind or beside it), so it's usually stamped
early.

## Mechanism — generic components

- **Single-file vendor confinement.** The vendor SDK is imported in **exactly one
  file** (plus its tests). No vendor type appears in any other package — verifiable
  by an import lint. The one file is the only thing that changes when the vendor
  does.
- **Neutral DTOs only, unexported concrete.** The port is an interface exposing
  only vendor-agnostic types; the concrete adapter is **unexported** with a
  **compile-time assertion** (`var _ Port = (*concrete)(nil)`) so the compiler
  guarantees the adapter satisfies the port and callers can't reach past it. A
  `Fake` adapter behind the same assertion makes every consumer testable without
  the vendor.
- **Vendor states mapped to internal enums with safe defaults.** External status
  strings are translated to the app's own enum at the seam; an **unknown/new vendor
  state maps to the safe default**, never leaks upward as a raw string.
- **Signed webhook normalized to a neutral internal event — webhook is the source
  of truth.** Inbound vendor webhooks are verified (raw-body signature + replay
  window) and normalized to a minimal neutral event (`{id, type, livemode, raw}`);
  downstream logic reads *that*, never the browser's post-action redirect (which
  can die mid-flow). This is the ingestion half that
  [`fetch-on-webhook-reconcile`](../fetch-on-webhook-reconcile) and
  `tiered-subscription` build on.
- **Sign-per-request short-lived access tokens.** Access to vendor-hosted resources
  (a video stream, a signed URL) is granted by a **per-request** token minted at
  the seam, **never persisted**, with a **TTL clamped to a hard ceiling** (default
  well under the ceiling). No long-lived shared credential reaches the client.
- **Direct browser→CDN upload via a one-time server-minted ticket.** Large uploads
  go **browser → vendor** directly, using a **one-time upload ticket** the server
  mints (e.g. tus `direct_user`); the app's **API credential never reaches the
  browser**. Keeps big payloads off the app's compute and the secret server-side.

## Invariant cluster

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-PNA-001` | Single-file vendor confinement: the vendor SDK is imported in exactly one file (+ tests); no vendor type crosses the seam. | dogeared-coach `billing/stripe.go` (only `stripe-go` importer); `video/cloudflare.go` (package doc: no CF type escapes) |
| `INV-PNA-002` | Neutral DTOs only; concrete adapter unexported + compile-time `var _ Port = (*concrete)(nil)` assertion; a Fake behind the same port. | `video/service.go:91-118`, `video/cloudflare.go:37`; `billing/client.go:39-83`, `billing/stripe.go:24`, `billing/fake.go:52` |
| `INV-PNA-003` | Vendor status mapped to an internal enum with a safe default for unknown states. | billing status→tier + video status mapping at the seam |
| `INV-PNA-004` | Signed webhook normalized to a neutral internal event `{id,type,livemode,raw}`; the webhook, not the client redirect, is source of truth. | `billing/stripe.go:290-318`; `video/webhook.go:118-142` |
| `INV-PNA-005` | Sign-per-request access token: minted per request, never persisted, TTL clamped to a hard ceiling. | `video/jwt.go:22-24,95-144` (`maxTTL=60m`, `defaultTTL=30m`, `clampTTL`) |
| `INV-PNA-006` | Direct browser→CDN upload via a one-time server-minted ticket; the API credential never reaches the browser. | `video/cloudflare.go:62-114` (`CreateDirectUploadURL`, tus `direct_user=true`) |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `vendor` | yes | The external service behind the seam (billing / video / email …). |
| `port` | yes | The neutral interface name callers depend on. |
| `confinement_file` | yes | The single file allowed to import the vendor SDK (import-lint target). |
| `webhook_events` | no | Vendor events normalized to neutral internal events. |
| `token_ttl_ceiling` | no | Hard cap on sign-per-request token TTL. |
| `direct_upload` | no (default `false`) | Whether large uploads go browser→CDN via a one-time ticket. |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `vendor_type_leaks` | down | vendor types appearing outside the confinement file (target 0; import-lint asserted). |
| `swap_blast_radius` | down | files that must change to swap the vendor (target: 1). |
| `credential_client_exposure` | down | flows where a long-lived vendor credential reaches the client (target 0). |

## Relationship to other patterns

- **`fetch-on-webhook-reconcile` / `tiered-subscription`** — their "webhook is
  source of truth" ingestion is this pattern's `INV-PNA-004` applied to the billing
  vendor; the vendor sits behind this seam.
- **`elevated-access-session`** — the session signer / identity provider sit behind
  this seam so the auth vendor is swappable.
- **`multi-tenant-isolation`** — the media/CDN vendor's per-request tokens
  (`INV-PNA-005`) are minted *after* the tenant/authorization guards, so
  swappability doesn't widen the isolation surface.

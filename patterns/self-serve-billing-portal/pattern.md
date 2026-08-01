# Pattern: Self-Serve Billing Portal

- **Category:** saas-infra
- **Trust class:** money-moving surface — minting, the mirror's write path, and webhook intake are protected paths
- **Status:** specified (spec complete; `scaffold/` not yet built)
- **Depends on:** [`fetch-on-webhook-reconcile`](../fetch-on-webhook-reconcile) — billing webhooks re-fetch the authoritative object, never trust the payload; [`feature-entitlements`](../feature-entitlements) — the degrade map (INV-SBP-006) is enforceable only through the entitlement read-model
- **Feeds:** [`feature-entitlements`](../feature-entitlements) — subscription state is an input the entitlement resolver degrades from

## What it is

Users manage plan, payment method, and seats without a support ticket: the
app mints a **provider-hosted portal session server-side** and keeps a **local
mirror of plan/seat state that only billing webhooks write**. The provider's
UI does the PCI-heavy lifting; the app's job is the two disciplines around it —
*who may mint a session for whom*, and *how the mirror stays truthful*.

Why a pattern and not "just call the portal API": audits of a shipped
production billing integration found the exact failure classes this cluster
prevents. The same subscription-status field had **two independent writers**
(a synchronous admin path and the webhook, no ordering guard — last write
wins, so a slow webhook clobbers a newer sync); the billing webhook **trusted
the event payload** while the payments webhook beside it correctly re-fetched;
webhook handler errors returned 200 (so the provider never retried); and the
mirrored status **gated nothing** — a canceled tenant retained full
functionality. Each is an invariant below.

## When to apply

- Plan/payment-method/seat management is a real support-ticket class you want
  off the support queue.
- A payment provider offers a hosted portal (e.g. a billing portal session
  API) and webhooks for subscription lifecycle.
- Subscription state must actually gate capability (through the entitlement
  read-model), not just render a badge.

## Mechanism — generic components

- **Server-minted session.** The portal/checkout session is minted server-side:
  the customer binding comes from the server's own mirror (never a
  client-supplied customer id), the return URL is server-controlled, and a
  missing provider client fails closed. *(INV-SBP-001 — mined.)*
- **Caller-scoped minting.** A self-serve caller may mint only for the account
  they administer — the account-scoping is asserted server-side from the
  authenticated principal, not from a request parameter. Staff minting for
  arbitrary accounts is a separately-authorized, audited path. *(INV-SBP-002 —
  design-derived; the mined implementation was staff-only and unscoped.)*
- **Webhook-authoritative mirror, single writer.** The local plan/seat mirror
  is written through **one** write path, driven by provider webhooks; any
  synchronous admin sync routes through that same writer. No second `$set`
  site, no last-write-wins races between sync and webhook. *(INV-SBP-003 —
  design-derived; declare `controls_field` + `sanctioned_writers` so
  `check_single_writer` can enforce it mechanically.)*
- **Fetch on webhook.** Handlers re-fetch the authoritative object from the
  provider on receipt rather than trusting the event payload. *(INV-SBP-004 ←
  the in-repo `fetch-on-webhook-reconcile` pattern.)*
- **Verified, idempotent, retryable intake.** Signature verification fails
  closed (missing secret refuses intake, never bypasses); events dedupe on
  event id; the dedupe record persists **after** the mutation so a failed
  mutation is retried rather than stranded behind an "applied" record; and a
  handler error returns non-2xx so the provider retries. *(INV-SBP-005 —
  mined from the payments-side intake of the same system.)*
- **Declared degrade.** Subscription states (past_due, canceled) map to a
  declared capability degrade consumed via the entitlement read-model — a
  mirror nobody reads is decoration. *(INV-SBP-006 — design-derived.)*

## Grounding

INV-SBP-001 and INV-SBP-005 are **mined** from a shipped production billing
integration (per the registry's provenance policy, private-repo paths are held
out of the public registry; the manifest describes the realized mechanisms —
server-read customer binding + hardcoded return URL, and the payments-side
event-id dedupe with persist-after-mutate ordering and its documented
stranded-retry rationale). INV-SBP-004 cites the in-repo
[`fetch-on-webhook-reconcile`](../fetch-on-webhook-reconcile) pattern — the
mined system practiced it on the payments side and **omitted it on the billing
side**, which is exactly the gap the dependency closes. INV-SBP-002/003/006
are **design-derived** and marked as such: each inverts an observed failure
(unscoped staff-only minting; a two-writer subscription-status field; a
status that gated nothing). They ground fully when a product first builds the
self-serve surface.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `provider` | yes | The billing provider (portal-session API + webhooks). |
| `mirror_fields` | yes | The mirrored plan/seat fields and their store (the `controls_field` set for single-writer enforcement). |
| `webhook_writer` | yes | The single sanctioned writer of the mirror. |
| `mint_scope` | yes | How the authenticated principal maps to the one account they may mint for. |
| `return_url` | yes | Server-controlled portal return URL. |
| `degrade_map` | yes | Subscription state → entitlement degrade (consumed by feature-entitlements). |
| `staff_mint_audit` | no | Audit sink for the separately-authorized staff minting path. |

## Success metrics

`billing_support_tickets` ↓ (the pattern's reason to exist),
`mirror_provider_divergence` = 0 (webhook-authoritative discipline),
`unsanctioned_mirror_writers` = 0 (single-writer check),
`webhook_replay_side_effects` = 0 (idempotent intake),
`canceled_accounts_with_full_capability` = 0 (declared degrade).

## Trust

Session minting, the mirror's write path, webhook intake, and the degrade map
are money-moving surface — protected paths. A worker adding a mirror writer or
widening mint scope is parked for human review.

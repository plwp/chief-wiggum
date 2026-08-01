# Pattern: Transactional Email & Dunning Lifecycle

- **Category:** process-loop
- **Trust class:** outbound messaging to end users — templates, triggers, and the dunning ladder's economics are protected paths
- **Status:** specified (spec complete; `scaffold/` not yet built; dunning half aspirational — see Grounding)
- **Depends on:** [`provider-neutral-adapter`](../provider-neutral-adapter) — the send seam is an adapter port, not a vendor SDK call site; [`feature-entitlements`](../feature-entitlements) — the dunning ladder's grant-preserving degrade (INV-TED-006) executes through the entitlement read-model
- **Feeds:** [`engagement-instrumentation`](../engagement-instrumentation) / [`improvement-loop`](../improvement-loop) — send outcomes and recovery rates are conversion/retention signal

## What it is

Idempotent, provider-neutral lifecycle messaging: welcome, activation nudge,
re-engagement, and a failed-payment **dunning** sequence with bounded retries
before an explicit degrade. The value is not "send email" — it is the
discipline that makes lifecycle messaging safe to automate: **send-once keys
enforced atomically** (retries, replicas, and double-clicks produce one send),
an **outbox** correlating every attempt with provider evidence, and
**criticality-declared failure handling** (a blocking message aborts the
operation; a courtesy message never does).

Why a pattern and not "call the email API": audits of shipped production
SaaS found the inverse of each invariant below: duplicate sends on retry
because the only dedupe key lived in a *different* service; send-once
implemented as read-then-write (two replicas both select, both send); a
13-method domain-shaped email interface that locks in the vendor; a
lifecycle-stage function that computed `at_risk`/`churned` and then drove
nothing; and a failed-payment webhook that stamped `past_due` and emailed
no one, ever.

## When to apply

- The product sends lifecycle messages triggered by state (signup, inactivity,
  payment failure), not only direct request/response receipts.
- Sends must be exactly-once per (entity, message-type) under retries and
  replicas.
- Failed payments need a bounded recovery sequence that ends in an explicit,
  reversible degrade — not silent cancellation, not infinite grace.

## Mechanism — generic components

- **Transport-shaped provider seam.** All sends go through one adapter port
  (`Send(message)`) so the provider swaps without touching domain call sites;
  domain helpers compose messages, they don't hold SDK types. *(INV-TED-001 ←
  `provider-neutral-adapter`; the mined system's domain-shaped 13-method
  interface is the anti-pattern.)*
- **Atomic send-once.** Every lifecycle send derives a deterministic send key
  `(entity, message_type)` enforced by a unique index or CAS claim — never
  read-then-write — so N replicas and user retries produce exactly one send.
  *(INV-TED-002 — the mined stamped-timestamp mechanism plus its documented
  multi-replica race is the rationale for requiring the atomic form.)*
- **Outbox with provider correlation.** Every attempt writes an outbox row
  (status, error, provider message id); manual resend is permitted only from
  `failed`. *(INV-TED-003 — mined.)*
- **Re-assert state before send.** Async notify paths re-read the entity and
  confirm the triggering state still holds before sending — the world may have
  moved on since the trigger fired. *(INV-TED-004 — mined.)*
- **Criticality-declared failure.** Each message type declares `blocking`
  (send failure fails the surrounding operation — e.g. the business copy of a
  booking) or `best-effort` (failure is logged, never blocks — e.g. the
  customer courtesy copy); missing recipient/config on a blocking message
  fails **before** any send. *(INV-TED-005 — mined.)*
- **Bounded dunning ladder → explicit degrade.** Failed payment starts a
  bounded, spaced retry-and-message ladder; exhaustion ends in an explicit,
  grant-preserving degrade (capability narrows via the entitlement read-model;
  delivered value is never clawed back) — never silence, never infinite
  grace. Ladder timing/copy are admin-gated economics. *(INV-TED-006 —
  design-derived, aspirational.)*
- **Suppression respected.** Bounce/complaint/unsubscribe suppression is
  checked before every send; provider delivery webhooks feed the suppression
  store. *(INV-TED-007 — design-derived.)*

## Grounding

INV-TED-003/004/005 are **mined** from a shipped production SaaS's email
subsystem (per the registry's provenance policy, private-repo paths are held
out of the public registry; the manifest describes the realized mechanisms —
the best-effort outbox with provider message ids and failed-only resend, the
re-read-and-confirm-state notify goroutine, and the asymmetric
business-blocking/customer-best-effort booking mail). INV-TED-001 cites the
in-repo [`provider-neutral-adapter`](../provider-neutral-adapter) pattern;
INV-TED-002 is the **atomic strengthening** of a mined mechanism whose
read-then-write race is real in the source. INV-TED-006 (the dunning half) and
INV-TED-007 are **design-derived and flagged aspirational** exactly as issue
#139 requires: no mined app has built the ladder (the observed failed-payment
path stamps a status and messages no one), so these capture the standard
recovery discipline and ground fully when a product first builds dunning.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `send_adapter` | yes | The transport-shaped port (provider-neutral-adapter instance). |
| `message_types` | yes | Each with trigger, template, and declared criticality (`blocking` / `best-effort`). |
| `send_once_store` | yes | Where `(entity, message_type)` keys are atomically claimed. |
| `outbox` | yes | The attempt ledger (status, error, provider message id). |
| `dunning_ladder` | no | Steps: delay, message, retry; exhaustion → degrade action (aspirational half). |
| `suppression_store` | no | Bounce/complaint/unsubscribe list consulted before send. |

## Success metrics

`duplicate_sends` = 0 (atomic send-once), `blocking_send_failures_surfaced` =
100%, `dunning_recovery_rate` ↑, `silent_churn_after_payment_failure` = 0,
`suppression_violations` = 0. Recovery outcomes are retention signal for the
improvement loop; ladder economics stay admin-gated.

## Trust

Templates, triggers, the send-once store, and the dunning ladder's
timing/copy/degrade are outbound messaging to end users — protected paths. A
worker adding a message type or touching ladder economics is parked for human
review.

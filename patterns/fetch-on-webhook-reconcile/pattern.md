# Pattern: Fetch-on-Webhook Reconcile (non-monotonic external state)

- **Category:** process-loop (external-state consistency)
- **Trust class:** money-path / external-state integrity — the projection writer is a protected path
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The correct idempotency model for a webhook whose external resource has
**non-monotonic** state — a subscription that flips `active ⇄ past_due ⇄ active ⇄
canceled`, an order that can be refunded, anything that can go backwards. The
webhook event is treated as a **trigger only**: the handler never reads mutable
state off the payload, it does a **live fetch** of the external resource and
projects *that* onto local state.

This is the deliberate opposite of the [monotonic-FSM webhook](#sibling-the-monotonic-fsm-webhook)
— and the value of capturing it as a pattern is that most teams reach for the
monotonic model by reflex (copy the payload, guard with a terminal-state check)
and it is silently wrong for money: a stale or out-of-order redelivery copies an
old status and mis-grants or mis-revokes entitlement.

> **The decision this pattern encodes:** *Is the external resource's state
> monotonic?* If it only ever advances to a terminal state (upload → ready), the
> payload **is** the truth — use the FSM guard. If it can move backwards, the
> payload is **only a hint that something changed** — fetch the truth. Pick the
> model from the state's shape, not by habit.

## When to apply

Any signed webhook driving local state off an external system of record whose
state is not monotonic: billing/subscriptions, payments/refunds/disputes,
inventory, external approval workflows. If you find yourself writing "but what if
the events arrive out of order" — you want this pattern, not a bigger idempotency
key.

## Mechanism — generic components

Vendor stays behind [`provider-neutral-adapter`](../provider-neutral-adapter);
the event is decoded only to a neutral envelope (`{id, type, livemode, ...ids}`).

- **Event = trigger, fetch = truth.** On a state-affecting event, extract only the
  ids needed to fetch, then **fetch the live resource** and project its current
  state. The handler must *never* read mutable state (status/plan/amount) from the
  payload. This is what makes it order-immune: a stale/redelivered/out-of-order
  event cannot move local state off live truth.
- **Dedupe is a retry lease, not a drop.** A `claim → processed` lease on the
  event id serialises concurrent deliveries, but on reconcile **failure it releases
  the claim** so the provider redelivers (a dropped-on-first-sight dedupe would
  lose the retry). Only terminal success marks the event processed.
- **Single-writer projection.** Exactly one code path projects external truth onto
  local state; every other reader is downstream of it. (Enforceable by
  `check_single_writer.py` on the projected field.)
- **Unknown identifier is fatal — never a silent floor.** An unrecognised
  price/SKU/plan id **errors, alerts, and writes nothing**; it must never fall back
  to the lowest tier. Silent-downgrade-on-unknown is the classic revenue leak.
- **Non-access states project to the safe floor for enforcement.** A *known* but
  non-paying resource state (`past_due` past grace, `unpaid`) grants no paid
  capability — map it to the restrictive tier for gating, distinct from the
  unknown-id error above.
- **Resource-mismatch guard.** A fetched resource binds to a local record only if
  the record's cached resource-id is empty or equal, so a late event for a
  superseded resource cannot clobber a newer one.
- **Terminal/deletion latch.** Once a local record is latched terminal (being
  deleted, closed), late events are **ignored, not errored** (so the provider
  stops retrying) — no entitlement can be re-raised on a record on its way out.
- **Signature over a rotation list + bounded timestamp window.** Verify the raw
  body against *any* currently-valid signing secret (supports zero-downtime secret
  rotation) inside a replay window.
- **Split non-state signals away from the projector.** Events that don't change the
  projected state (a refund flag, a dispute) run the *same* dedupe lease but a
  *no-projection* effect — they annotate/audit and are forbidden from touching the
  projected field.

## Sibling: the monotonic-FSM webhook

The other model, worth stating so the choice is explicit. When the external state
is monotonic (e.g. video upload `pending → processing → ready|rejected`, terminal
states never resurrect), the payload *is* authoritative and idempotency is a
**terminal-state FSM guard in the write**: advance only from non-terminal states,
exclude the target state from the source set so a replay is a true no-op. No fetch
needed. Both models can coexist in one app (they do in the provenance app below);
the registry keeps them as **one pattern with two branches** so the selection
criterion travels with them.

## Invariant cluster

This pattern *is* the following cluster of invariants (see
[patterns as invariant clusters](../../docs/patterns-registry.md#patterns-as-clusters-of-invariants)).
Binding this pattern into an epic pulls these into `invariants.md` with their
stable ids; the traceability, single-writer, and ratchet gates then hold them.

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-FOWR-001` | Trigger-only: the handler never reads mutable state from the payload; state comes from a live fetch. | dogeared-coach `INV-BIL-001`; `services/billing_reconcile.go:82-94,169-171` |
| `INV-FOWR-002` | Order-immune & idempotent; dedupe is a claim→processed lease that **releases on failure** for retry, not a silent drop. | `INV-BIL-003`; `handlers/stripe_webhook.go:158-165` |
| `INV-FOWR-003` | Single-writer projection of the external-derived state. | `INV-BIL-001` (single-writer) |
| `INV-FOWR-004` | Unknown external id is fatal: no write, no floor fallback, alert. | `INV-BIL-012`; `billing_reconcile.go:37-40,256-267` |
| `INV-FOWR-005` | Known-but-non-access states project to the restrictive floor for enforcement. | `INV-BIL-005`; `billing_reconcile.go:250-257` |
| `INV-FOWR-006` | Signature valid against a rotation **list** of secrets + bounded timestamp window. | `INV-BIL-002` |
| `INV-FOWR-007` | Resource-mismatch guard: bind only if cached id empty or equal. | `billing_reconcile.go:182-213` |
| `INV-FOWR-008` | Terminal/deletion latch: late events ignored (not errored) once the record is latched terminal. | `INV-BIL-013`; `billing_reconcile.go:236-248` |

The **sibling monotonic branch** carries its own smaller cluster (terminal-state
never resurrects; advance-only FSM write; replay is a no-op), realized in the same
app's video webhook (`db/video_webhook.go:54-84`, `handlers/webhook.go:301-308`).

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `resource` | yes | The external resource fetched for truth (e.g. `subscription`). |
| `state_shape` | yes | `monotonic` or `non-monotonic` — selects the branch. |
| `projected_field` | yes | The local field the reconcile is the single writer of. |
| `unknown_id_policy` | no (default `fatal`) | Must be `fatal`; present so a caller can't quietly weaken it. |
| `signing_secrets` | yes | The rotation list the signature is checked against. |
| `non_state_events` | no | Events routed to the no-projection audit-only branch. |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `entitlement_drift_incidents` | down | wrong-state-grant/revoke incidents from out-of-order or stale events (target 0). |
| `silent_downgrade_incidents` | down | unknown-id → floor-fallback events (target 0; the gate asserts this). |
| `reconcile_convergence` | up | % of records whose local state equals a fresh live fetch (drift sweep). |

## Relationship to other patterns

- **`provider-neutral-adapter`** — the vendor + its signed webhook sit behind the
  neutral seam; this pattern is what the seam's ingestion *does* for non-monotonic
  resources.
- **`tiered-subscription`** — its "billing webhook is source of truth" component is
  this pattern applied to the subscription resource.
- **`reconciliation-sweep`** — the *periodic* cousin. This is event-driven
  fetch-on-trigger; the sweep is a scheduled bidirectional pass. Run both: the
  sweep catches events that never arrived.
- **`entitlement-overlay`** — the reconcile recomputes `max(payer, overlay)` so it
  can never revert an admin comp; the two patterns share the `projected_field`.

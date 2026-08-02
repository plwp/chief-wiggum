# Pattern: Platform Cost Observability

- **Category:** monitoring-feedback
- **Trust class:** operator-plane read-only surface over trusted billing telemetry
- **Status:** specified (spec complete; not yet built in any mined app — see Grounding)
- **Depends on:** [`multi-tenant-isolation`](../multi-tenant-isolation) — mounts on the operator plane its INV-MTI-004 establishes
- **Feeds:** [`improvement-loop`](../improvement-loop) — spend actuals are trusted operational signal; they also check the stack cost-tier model and `/business-consultant` unit-economics assumptions against reality

## What it is

Every mined app watches money coming **in** (billing webhooks, revenue boards) but
none watches money going **out**: the platform's own spend is invisible until the
invoices arrive — and they arrive from *many* vendors, not one. A modern SaaS
burns money on the cloud provider **and** on per-call meters outside it: LLM
inference, media delivery, transactional email, a managed database. This pattern
surfaces the **whole bill for this app** — a multi-source spend ledger, read-only
in the SaaS admin panel — as periodically ingested snapshots with per-service
(and, where labels allow, per-tenant) breakdowns, honest staleness semantics, and
budget alerts that fire whether or not anyone is looking at the panel.

Why a pattern and not "just open the cloud console": the console is outside the
product's operator plane, shows **one vendor's slice** of the bill, and — when
the cloud project or vendor account is shared — shows *project-wide* spend
nobody can pin to this app. Naive in-app cost pages fail in recurring ways: a
panel that calls billing APIs on page load is slow, quota-bound, and can make
the monitor itself a meaningful cost; billing data **settles late**, so
yesterday's figure shown as final silently understates spend; breakdowns that
drop unlabeled lines make the parts sum to less than the invoice; attribution
reconstructed by heuristics from a shared bill is fiction; the LLM meter — the
fastest-moving line — is missing entirely because it isn't on the cloud bill;
and a threshold that only renders in a panel alerts nobody at 2am. The
invariant cluster below exists to kill exactly those failure modes.

## When to apply

- The product runs on metered infrastructure and an operator needs to see what
  **this app** costs — across every vendor — without leaving the admin plane.
- Spend actuals should feed decisions — tier graduation, pricing, unit economics
  — rather than arriving once a month as invoice surprises.
- Uncapped meters exist (LLM inference, media egress, logging, serverless
  scale-out) that need budget thresholds alerting out-of-band.

## Mechanism — generic components

- **Operator-plane-only surface.** Platform spend is cross-tenant operator data;
  the cost routes register only on the admin/operator plane, out-of-band from
  tenant routes, and are never reachable by a tenant-scoped principal. *(Reuses
  the `multi-tenant-isolation` operator-plane discipline; INV-PCO-001 ←
  INV-MTI-004.)*
- **Per-source read-only, least-privilege identity.** Each externally-read
  spend source is read with a dedicated credential scoped to viewing
  billing/usage data only — it can
  neither mutate infrastructure nor billing configuration, and its secret
  material lives in the platform's secret seam, never env vars. *(INV-PCO-002 —
  design-derived. It borrows the least-privilege, single-purpose identity-split
  discipline `deployment-release` establishes with INV-DRL-004, but is a
  distinct identity with a distinct scope — the deploy identity provably cannot
  read secrets, while the spend reader must read vendor usage APIs — so the
  cite is a discipline borrowed, not an invariant realized.)*
- **Ingest-then-serve.** A bounded, scheduled ingest queries each source and
  persists normalized snapshots; the panel only ever reads snapshots. No
  request-path call to any billing source exists, so the monitor's own query
  spend is bounded by construction — and its storage too: raw per-day detail
  rolls up beyond a declared retention window, so the cost monitor never
  becomes a cost line. *(INV-PCO-003 — design-derived.)*
- **Staleness + settling-lag honesty.** Every figure carries its as-of timestamp
  and its source's settling window; days still inside the window are marked
  partial, never final — and each ingest **re-reads and idempotently replaces**
  them (keyed on source+day), so late-arriving corrections repair
  already-persisted days instead of being frozen out at first write; estimated
  lines (see whole-bill coverage) are marked estimated, never blended silently
  into actuals; a failed ingest surfaces as visibly stale rather than silently
  serving old numbers as fresh. *(INV-PCO-004 — design-derived; the
  fail-visible sibling of the registry's fail-closed-on-unknown-age
  meta-discipline.)*
- **App-scoped, disjoint, sum-preserving attribution.** Attribution to *this
  app* is established at provision time — a dedicated cloud project per
  app+env, resource labels, per-app vendor keys/subaccounts (that provisioning
  is itself a protected path) — never reconstructed later by heuristics over a
  shared bill. Breakdowns partition each source's reported total: spend that
  can't be pinned to the app lands in an explicit `unattributed`/`shared`
  bucket, credits stay visible as their own line, and per-source lines carry
  their billed currency with any conversion rate recorded. And the source set
  is **disjoint** — every unit of spend counted exactly once: a vendor billed
  *through* another source (marketplace/reseller lines on the cloud bill)
  registers as a declared sub-line of that source, never an independent
  addend, and an app-side estimate acting as a leading indicator is
  **superseded** by its source's reported actual on reconcile, never summed
  with it. At month close the normalized ledger is reconciled per source
  against the settled invoice — the ledger's own truthfulness check.
  *(INV-PCO-005 — design-derived.)*
- **Whole-bill source coverage.** Every metered vendor the product depends on is
  a registered spend source — the cloud provider *and* the per-call meters (LLM
  inference, media delivery, email, managed DB, payment-processor fees). A
  vendor without a fetchable cost API still registers, via app-side metering
  (e.g. token counts × price on the LLM path) or operator **manual entry** —
  manual figures are operator-authored writes stamped with author and entry
  time, marked estimated, scoped to sources that declare a manual mechanism,
  and never overwritten or duplicated by an ingest — so total platform spend is
  never silently understated by an invisible vendor. *(INV-PCO-006 —
  design-derived.)*
- **Out-of-band budget alerts, protected thresholds.** Budget thresholds — on
  the cloud bill *and* the fast-moving vendor meters — alert through a channel
  independent of the panel; a breach never waits for an admin to look. Each
  rung fires exactly once per (source, budget period, rung) via a send-once
  key and re-arms at period rollover — neither spammed into being muted nor
  latched silent after its first rung. And **absence of fresh data is itself
  an alert condition**: a snapshot older than its source's
  `staleness_alert_after` fires through the same out-of-band channel — a
  stalled ingest can never silently mute the meter alerts it computes.
  Threshold config and the ingest jobs are protected paths: an optimization
  loop may propose changes, never auto-apply them. *(INV-PCO-007 —
  design-derived.)*

## Grounding

No mined app has built this surface (the direction is telling: billing patterns
in the registry all watch revenue in, none watch spend out, and the
`gcp-serverless-saas` stack profile lists observability among its known gaps).
The cluster is therefore **design-derived and honestly marked**, with one
exception that is **not re-derived** but cites the in-repo cluster realizing
the same discipline: the operator-plane mount (INV-PCO-001 ←
[`multi-tenant-isolation`](../multi-tenant-isolation) INV-MTI-004). The
least-privilege read-only identity (INV-PCO-002) *borrows* the
[`deployment-release`](../deployment-release) INV-DRL-004 identity-split
discipline but is a distinct identity with a distinct scope, so it stays
design-derived rather than claiming realization. The mined apps also
already contain the structural neighbours the first build will assemble: an
admin-gated read-only financial board, a nightly refresh job, scaling caps used
as cost guards, and — on the LLM path — a fail-closed cost-protection breaker
with per-caller budgets (the *control*-side sibling whose loop this pattern's
observability closes). So the design-derived invariants describe a composition
of proven shapes, not an invention. They ground fully when a product first
builds the surface; the `gcp-serverless-saas` binding names the intended first
adopter.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `spend_sources` | yes | The registered metered vendors and how each is read: fetch mechanism (billing export / usage API / app-side meter / manual), granularity, settling window, billed currency, and — for a vendor billed through another source — the parent source it is a sub-line of (disjointness). The cloud provider's billing export is the mandatory first entry. |
| `app_boundary` | yes | How this app's spend is isolated **at provision time**: dedicated cloud project per app+env, resource labels, per-app vendor keys/subaccounts. A protected path. |
| `ingest_schedule` | yes | Cadence of the snapshot ingests (bounds `cost_visibility_lag`). |
| `staleness_alert_after` | yes | Per-source age beyond which a missing fresh snapshot fires the out-of-band dead-man's-switch alert (e.g. 2× `ingest_schedule`). |
| `snapshot_store` | yes | Where normalized snapshots persist (the app's own DB — the panel never reads a source). |
| `snapshot_retention` | no | How long raw per-day detail is kept and the rollup granularity beyond it (bounds the monitor's own storage). |
| `budget_thresholds` | yes | The threshold ladder per source (cloud budget + vendor meters); send-once per (source, period, rung), re-armed at rollover. |
| `alert_channel` | yes | Out-of-band alert delivery, independent of the panel. |
| `attribution_labels` | no | Resource labels used for breakdown (env / service / tenant), applied at provision time. |
| `reporting_currency` | no | Currency the normalized total is presented in; lines keep their billed currency + recorded conversion rate so the total is reproducible. |

## Success metrics

`api_sourced_spend_share` ↑ (share of spend read from an authoritative
vendor-reported source vs estimated/manual — ledger fidelity, not mere
registration), `cost_visibility_lag` ↓, `unattributed_spend_share` ↓,
`ledger_vs_invoice_variance` ↓ (normalized monthly total vs the settled
invoice per source — the ledger's own truthfulness check),
`time_to_budget_alert` ↓, `monitor_self_cost_share` ↓,
`spend_vs_model_variance` ↓ — the last one is the feedback edge: observed
spend checked against the stack cost-tier model and the unit-economics
assumptions `/business-consultant` derives, so tier graduation is triggered by
actuals, not vibes.

## Trust

The spend signal is operator-trusted infra/vendor telemetry (no end-user input
on this path; the one non-job write is operator manual entry, which is
author-stamped, estimated-marked, and confined to sources declaring a manual
mechanism — the surface stays read-only for everyone else). The surface itself
is operator-plane-only (INV-PCO-001). The ingest jobs, the snapshot write path
(manual entries included), each source credential's scope, the `app_boundary`
provisioning, and the budget threshold config (dead-man's-switch included) are
all protected paths — a worker touching them is parked for human review,
exactly as with any goalpost.

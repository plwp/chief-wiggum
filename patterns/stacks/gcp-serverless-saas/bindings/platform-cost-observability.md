# Binding: `platform-cost-observability` → Cloud Billing budgets + BigQuery billing export + per-vendor usage APIs

- **Realizes:** [`platform-cost-observability`](../../../platform-cost-observability) (vendor-neutral spec)
- **Tier:** T1+ · **Vendor:** GCP Cloud Billing + BigQuery export, plus each metered vendor's usage/billing API · **Source:** **aspirational** — no mined app surfaces its own platform spend yet; intended first adopter is the stack's T2 exemplar (private)

The whole-bill spend ledger for this stack. The T1 floor is alert-only (no
panel); the T2 shape is the full pattern: multi-source ingest → normalized
snapshots in the app DB → admin-panel page on the operator plane. The stack's
cost doctrine already says *"put a project budget alert on everything"* — this
binding is that doctrine grown into an observable surface.

## The app boundary on this stack (INV-PCO-005)

On this stack a **GCP project is dedicated to one app + one environment**
(separate staging and prod projects), so **the project boundary IS the app
boundary** — project-scoped cost *is* app-scoped cost by construction, not by
filtering a shared bill. Keep it that way: if projects are ever consolidated,
terraform-applied resource labels (`app`, `env`, `service`) must take over as
the attribution boundary *before* consolidation, or attribution degrades to
heuristics — exactly what INV-PCO-005 forbids.

The same principle covers the off-GCP meters: **one vendor key/subaccount per
app+env** (LLM router key, email key, media account, DB project). A per-app key
makes the vendor's own usage reporting app-attributable by construction.

## T1 floor — budgets + alerts, no panel yet (realizes INV-PCO-007 only)

- `google_billing_budget` scoped to the app's project(s), with a
  `threshold_rules` ladder (e.g. 50% / 80% / 100% / 120% of the tier's expected
  monthly) and email notification to the operators. Terraform-managed, so the
  thresholds live in the human-run infra path — protected by construction.
- Vendor-side hard ceilings double as the alert floor where APIs are thin: the
  LLM router key's spend limit, Cloud Run `max-instances` (the stack's existing
  throughput-ceiling-as-cost-cap), per-plan media caps.

## T2 — the full surface

### Cloud side: BigQuery detailed-usage billing export

- Enable the **detailed usage cost export** at the billing account into a
  dedicated BigQuery dataset. Backfill on first enablement is
  **dataset-location-specific**: a US/EU multi-region dataset retroactively
  receives current-month (and some previous-month) data; a supported regional
  dataset gets nothing before enablement; and a moved or re-enabled export is
  never backfilled. The safe rule is unchanged either way: **enable at project
  creation**, tiers before you need the panel — the retroactive window is
  limited and not guaranteed.
- **Ingest** rides the stack's established job shape (there is no standing
  job/queue infra): a **PSK-gated pull endpoint poked by Cloud Scheduler**,
  nightly. It queries the date partitions covering **the trailing settling
  window plus any days since the last snapshot** — never "only what's new":
  export rows for already-snapshotted days keep arriving and adjusting, so the
  ingest **idempotently replaces** each re-read day (upsert keyed on
  source+day), letting late corrections repair earlier days instead of
  freezing them at first write. A day is only marked final once it exits the
  window; at month close, reconcile against `invoice.month` rather than usage
  timestamps. The query stays partition-pruned to that bounded window, so
  bytes scanned — and query cost — stay ≈ 0 (INV-PCO-003). Aggregate by
  `service.description` + labels and write one normalized snapshot row-set per
  day into the app DB (INV-PCO-003's snapshot_store).
- **Identity (INV-PCO-002):** a dedicated ingest service account holding
  `roles/bigquery.dataViewer` **scoped to the export dataset** plus
  `roles/bigquery.jobUser` **on the query project** (`jobUser` cannot be
  dataset-scoped — it is a project-level grant, so keep the export in a
  dedicated project if you want the blast radius minimal) — never any
  `billing.*` admin role. On Cloud Run it authenticates via ADC; no key file,
  same keyless discipline as the `deployment-release` binding.
- **Settling window (INV-PCO-004):** export rows keep arriving and adjusting
  for roughly 1–2 days (with month-end credits/true-ups later still). Mark the
  trailing `settling_window` days **partial**; stamp every snapshot with its
  as-of; the panel shows a stale badge whenever the latest snapshot is older
  than the schedule expects.
- **Attribution (INV-PCO-005):** breakdown by service + terraform-applied
  labels (`env`, `service`, `tenant` where per-tenant resources exist).
  Unlabeled/shared lines land in the explicit `unattributed` bucket; credits
  and promotions stay visible as their own lines, never netted invisibly. The
  buckets must sum to the export's total for the period.

### Vendor side: the meters that never hit the GCP bill (INV-PCO-006)

Each metered vendor in the stack registers as a spend source, read with a
**read-only, per-app credential** from Secret Manager (never env vars):

| Meter | Source | Mechanism | Notes |
|--|--|--|--|
| LLM inference | router usage API (e.g. OpenRouter credits/usage per key) | usage API | the fastest-moving line; per-app key ⇒ attributable by construction. App-side token metering (the app already holds request/response token counts × price) runs as a **leading indicator marked `estimated`**, reconciled against the router-reported actuals each ingest. |
| Media (stream/egress) | media vendor account usage | usage API | subscription base + delivery meter; per-plan caps are the control-side bound. |
| Email | email vendor plan + send counts | usage API / plan tier | free-tier ceiling is the first alert line. |
| Managed DB | DB vendor billing/invoice API per project | billing API | per-app project ⇒ attributable by construction. |
| Processor fees | payment processor's fee lines (balance transactions the app already mirrors) | in-app data | money-out hiding inside money-in; surfaced as its own spend line, not netted against revenue. |

A vendor with no fetchable cost API still registers with a manual/estimated
entry so the ledger's `spend_source_coverage` metric is honest — an invisible
vendor is a silent understatement, which INV-PCO-006 forbids.

### Panel + alerts

- The panel page registers via the operator-plane route group (the
  `multi-tenant-isolation` binding's admin plane) — tenant principals can never
  reach it (INV-PCO-001). It renders snapshots only: current month-to-date per
  source, trailing 90 days, per-service/per-label breakdown, `unattributed` and
  `estimated` called out, partial days visually distinct.
- **Alerts stay out-of-band (INV-PCO-007):** cloud side via
  `google_billing_budget` notifications (email, or Pub/Sub into the app's
  existing email seam at T2); vendor side via snapshot-derived threshold checks
  in the same nightly ingest, delivered through the email seam — the alert path
  shares nothing with the panel, so a breach never waits for a page view.
- **Currency:** the GCP export bills in the account currency; several vendor
  meters bill in USD. Lines keep their billed currency; the normalized total
  uses `reporting_currency` with the conversion rate recorded per ingest, so
  the number on the panel is reproducible (INV-PCO-005).

## Control-side siblings already in the stack

The T2 exemplar already ships the *control* half of cost discipline on its LLM
path — a fail-closed cost-protection breaker and per-caller request budgets —
and the stack carries `max-instances` caps and per-plan media caps. This
binding adds the *observability* half those controls currently lack: you can
cap a meter you can't see, but you can't tune it, price it, or catch the
vendor line you forgot to cap.

## Monitor self-cost (INV-PCO-003)

Budgets are free; billing-export storage is cents; partition-pruned nightly
queries sit inside the BigQuery free tier; vendor usage APIs are free calls.
Record the total as `monitor_self_cost_share` — it should stay ≈ 0 relative to
monitored spend, and the metric existing keeps it honest.

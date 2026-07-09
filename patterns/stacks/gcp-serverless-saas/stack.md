# Stack Profile: GCP Serverless SaaS (the house stack)

- **Status:** specified (bindings + skills below; `scaffold/` not yet built)
- **Provenance:** mined from three shipped apps — `plwp.net` (T0), `booking-forms`
  (T1), `dogeared-coach` (T2). The vendor choices are consistent across all three;
  this profile is that consistency written down.

## What a stack profile is (and why it's separate from a pattern)

The [patterns registry](../../../docs/patterns-registry.md) is deliberately
**vendor-neutral** — `provider-neutral-adapter` (candidate)
is the whole seam, and a pattern like [`tiered-subscription`](../../tiered-subscription)
names *no* vendor. That abstraction is correct, but a catalog of seams is not a
factory: to actually *stamp a working product* you need an **opinionated, real-
vendor default** for each seam, wired the way that has already shipped.

A **stack profile** is that default. It **binds** the abstract patterns to a
concrete infrastructure stack, ships the **runnable skills** to stand each piece
up, and carries a **cost model** so you know the zero-cost PoC footprint and where
money starts. The abstract pattern stays neutral (swap Cloud Run for Fly, Stripe
for Paddle); the profile is the bound default the factory reaches for first.

```
  ABSTRACT (patterns/)            CONCRETE (this profile)
  vendor-neutral seam        ×    named vendor + wiring    →   runnable recipe + cost
  ─────────────────────           ─────────────────────        ───────────────────────
  tiered-subscription        ×    Stripe Checkout+Portal   →   bindings/tiered-subscription.md
  engagement-instrumentation ×    DIY Firestore beacon     →   bindings/engagement-instrumentation.md
  multi-tenant-isolation     ×    server-derived scoping   →   bindings/multi-tenant-isolation.md
  provider-neutral-adapter   ×    Go interface + webhooks  →   bindings/provider-neutral-adapter.md
  transactional-email        ×    Resend + token links     →   bindings/transactional-email.md
  deployment-release         ×    keyless WIF + promote    →   bindings/deployment-release.md
```

## The house stack

| Seam | Vendor | Notes |
|--|--|--|
| **Static hosting / frontend** | **Firebase Hosting** | multi-site (one site per env/tenant); SPA rewrite; immutable-hashed assets, `no-cache` HTML |
| **Backend / API** | **Cloud Run** (Go) | region `us-central1`; scale-to-zero by default; public ingress, app does its own auth |
| **Database** | **Firestore** (T0/T1) → **MongoDB Atlas** (T2) | Firestore for config/append-only/simple; Atlas when the query model needs joins/txns/aggregations |
| **Auth** | **Firebase Auth / Identity Platform** | email/password + passwordless magic-link; verified server-side via Admin SDK; RBAC (e.g. Casbin) on top |
| **Payments** | **Stripe** | Checkout + Customer Portal for **subscriptions**; **Connect** for per-tenant marketplace/deposits |
| **Transactional email** | **Resend** | raw HTTPS or SDK; hand-rolled sanitized HTML; single-use token links |
| **Media / large files** | provider-neutral (**Cloudflare Stream** exemplar) | behind a Go interface: direct-to-CDN upload, per-request signed playback, webhook reconcile |
| **Secrets** | **GCP Secret Manager** | IaC creates containers + grants runtime read; values added out-of-band |
| **Deploy identity** | **Workload Identity Federation** (GitHub OIDC) | keyless, repo-scoped; least-privilege deployer (roll releases, not mutate infra) |
| **Observability** | **Cloud Logging** (structured stdout) + append-only audit collections | no paid APM by default; `/healthz` smoke; DIY Firestore analytics for product signal |
| **IaC** | **Terraform** | provisions services, secret containers, IAM, WIF; infra changes are a human-run privileged path |

Backend language is **Go** (Gin at T2, stdlib `net/http` at T1); frontend is a
**Vite/React SPA** (T2) or **Next.js static export** (T1), TypeScript throughout.

## Cost tiers — the ladder from $0 to production

The same stack, subsetted into three coherent tiers. You start at the lowest tier
that does the job and **graduate one seam at a time** — most seams add cleanly
(add Cloud Run, add Auth, flip `min-instances`). **One seam is not a knob but a
migration: Firestore → Atlas** changes query semantics, indexes, transactions,
backups, and local tooling — it needs a dual-write/backfill/cutover plan, not a
config flip. Design the T1 data access behind a repository interface *from the
start* so that migration is contained.

### T0 — Static + DIY (~$0/mo) · exemplar `plwp.net`

- **Firebase Hosting** serving a static site directly (no build step), manual
  `firebase deploy`.
- **DIY Firestore analytics**: a cookieless beacon POSTs directly to the Firestore
  **REST API**; writes locked **append-only** by security rules (`hasOnly` key
  whitelist, `read/update/delete: if false`); read offline by a stdlib CLI. No SDK,
  no Cloud Function, no analytics vendor. See
  [`bindings/engagement-instrumentation.md`](bindings/engagement-instrumentation.md).
- **No auth, no CI, no server.**
- **Cost:** genuinely ~$0 — Firestore free tier (~20k writes/day), Hosting free
  tier (10 GB storage, 360 MB/day egress), heavy libs from third-party CDNs. The
  cost lever is *no always-on server, no vendor*.
- **Use for:** landing pages, marketing sites, tiny tools, demand validation.

### T1 — Thin serverless (~$0–5/mo) · exemplar `booking-forms`

- **+ Cloud Run** (Go, stdlib `net/http`), `min-instances=0` (scale-to-zero),
  `max-instances` capped for cost — the cap doubles as a throughput ceiling.
- **+ Firestore** for per-tenant config: **layered defaults ← Firestore override**,
  merged behind a **60-second TTL cache**, with a **parity audit** (what the form
  presented vs what the server recomputed) logged as structured JSON.
- **+ Resend** for transactional email (sent synchronously in the request handler
  at this tier).
- **+ Stripe** — at this tier typically **Connect** (per-tenant connected accounts,
  pay-per-booking deposits) rather than subscriptions.
- **+ per-tenant static bearer token** for `/admin` (constant-time compare, magic-
  link `?token=` → `sessionStorage`), instead of a full identity provider.
- **+ keyless WIF deploy**, staging auto on push, prod manual.
- **Cost:** ~$0–5/mo — everything scale-to-zero or free-tier. **First ceiling hit
  is usually Resend** (3k mail/mo, 100/day free).
- **Use for:** real transactional micro-SaaS, per-tenant config, marketplace/deposit
  collection.

### T2 — Full production (~$60–140/mo) · exemplar `dogeared-coach`

- **+ Cloud Run prod `min-instances=1`** to kill cold-start latency on auth/media —
  typically **the biggest fixed-cost jump**. *~$45–65/mo is an illustrative figure*
  (1 always-allocated instance, ~1 vCPU / 512Mi–1Gi, `us-central1`, instance-based
  billing); the real number depends on region, CPU/memory, concurrency, and whether
  you use request-based vs instance-based billing — treat it as an example, not a
  stack invariant.
- **+ MongoDB Atlas** (Flex ~$8–30/mo → M10 ~$57/mo at hundreds of tenants) when
  the query model outgrows Firestore. **This is a data migration** (see the tier-
  ladder note), not a drop-in — budget for a dual-read/backfill/cutover.
- **+ Firebase Auth + RBAC** (Casbin) for real user accounts and roles.
- **+ Stripe Checkout + Customer Portal + webhooks** for subscriptions: the webhook
  is the **single entitlement writer** via **fetch-on-webhook reconcile**, verified
  against a **rotation list** of signing secrets with a **`livemode` guard**. See
  [`bindings/tiered-subscription.md`](bindings/tiered-subscription.md).
- **+ provider-neutral media** (Cloudflare Stream exemplar): direct-to-CDN upload,
  per-request signed playback JWTs (only the media id is stored), webhook reconcile
  for billing-ghosts.
- **+ promote-identical-image** deploys with a **bundle-guard** config-leak fence;
  **cross-tenant isolation proof tests** that must fail on leak.
- **Cost:** ~$60–140/mo. **Media delivery is usually the largest uncapped product
  meter** — bound it with per-plan caps + a soft alert. It is *not* the only
  usage-metered line, though: Firestore reads/writes/storage, Cloud Run
  requests/CPU/**egress**, Firebase Hosting transfer, Cloud Logging ingestion,
  Artifact Registry storage, Resend overages, and Stripe per-txn fees all scale with
  use. Put a **budget alert** on the project and cap/alert each meter. Break-even ≈ a
  handful of paying customers.
- **Use for:** subscription SaaS with accounts, media, and compliance needs.

### Graduation triggers (the cost-scaling knobs)

| From → To | Trigger | Add |
|--|--|--|
| T0 → T1 | need server logic / write-side validation / secrets | Cloud Run |
| T1 → T2 (db) | Firestore query model doesn't fit (joins, cross-entity txns, aggregations) | MongoDB Atlas |
| T1 → T2 (auth) | real user accounts + role-based access | Firebase Auth + RBAC |
| T1 → T2 (latency) | cold starts hurt auth/media UX | prod `min-instances=1` ← **biggest fixed jump** |
| usage | Resend > 3k mail/mo | ~$20 flat |
| usage | Stripe | linear per-txn from #1 (no free tier); Connect adds per-account fees |
| usage | media delivery | the **largest uncapped meter** (but not the only one — see cost note) — bound with plan caps + a soft alert |

## Pattern bindings (concrete recipes)

Each file below is the concrete realization of one vendor-neutral pattern **on this
stack** — the wiring, the non-obvious glue, the gotchas mined from the real apps.

| Binding | Realizes | Tier | Source |
|--|--|--|--|
| [`tiered-subscription.md`](bindings/tiered-subscription.md) | `tiered-subscription` | T2 | dgrd |
| [`engagement-instrumentation.md`](bindings/engagement-instrumentation.md) | `engagement-instrumentation` | T0+ | plwp.net |
| [`multi-tenant-isolation.md`](bindings/multi-tenant-isolation.md) | `multi-tenant-isolation` (two variants) | T1/T2 | booking-forms, dgrd |
| [`provider-neutral-adapter.md`](bindings/provider-neutral-adapter.md) | `provider-neutral-adapter` | T2 | dgrd (Cloudflare) |
| [`transactional-email.md`](bindings/transactional-email.md) | `transactional-email-and-dunning` | T1+ | booking-forms, dgrd |
| [`deployment-release.md`](bindings/deployment-release.md) | `deployment-release` | T1+ | booking-forms, dgrd |

## Stand-up skills (runnable playbooks)

The "skills to use them" — step-by-step stand-up procedures for the vendor pieces:

| Skill | Stands up |
|--|--|
| [`deploy-gcp-wif.md`](skills/deploy-gcp-wif.md) | keyless WIF deploy pipeline (staging-auto / prod-promote-identical) |
| [`stripe-subscriptions-setup.md`](skills/stripe-subscriptions-setup.md) | Stripe Checkout + Portal + webhook (fetch-on-webhook reconcile) |
| [`firestore-diy-analytics-setup.md`](skills/firestore-diy-analytics-setup.md) | the zero-cost DIY Firestore analytics beacon + CLI |
| [`secret-manager-setup.md`](skills/secret-manager-setup.md) | Secret Manager containers + least-privilege runtime access |

## Known gaps (honest, mined-from-real)

- **Dunning is unbuilt** in every mined app — Stripe sends billing emails; no
  in-app failed-payment sequence exists yet. The
  [`transactional-email.md`](bindings/transactional-email.md) binding marks the
  dunning half **aspirational** rather than inventing a recipe that never shipped.
- **No standing job/queue infra** — reconciliation is a PSK-gated pull endpoint
  poked by an external scheduler; no Cloud Scheduler/Tasks resource is provisioned.
  A `reconciliation-sweep` binding is a natural next addition.
- **Observability is thin** — structured logs + audit collections only; no APM/error
  tracker wired. Fine at these tiers; name it when a product needs SLOs.

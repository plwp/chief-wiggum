# Binding: `multi-tenant-isolation` → two variants on GCP Serverless SaaS

- **Realizes:** [`multi-tenant-isolation`](../../registry.json) (candidate; vendor-neutral)
- **Tier:** T1 (variant A) / T2 (variant B) · **Source:** `booking-forms`, `dogeared-coach`

Two genuinely different concrete realizations mined from two shipped apps. They are
**not** better/worse — they sit at different points on the cost/isolation curve.
Pick by whether tenants share a running process.

## Variant A — build-time isolation (T1, cheapest) · `booking-forms`

**One deployment artifact per tenant.** Tenants never share a running frontend.

- **Frontend resolution:** the shipped app substring-matches the host to a tenant,
  with a **`NEXT_PUBLIC_SITE_ID` baked at build time** as the fallback. Because CI
  fans out a **separate static build + separate Firebase Hosting site per tenant**,
  isolation is a *deploy artifact* boundary, not runtime routing — so a
  mis-resolution here is a UX bug, not (at this tier) a data leak.
  **⚠ Do not carry substring host-matching into any tier where the host decides data
  access.** Substring matching is a classic **tenant-confusion** bug —
  `foo.attacker.com` and `notfoo.example` both contain `foo`. The correct form is an
  **exact, normalized (lower-case / punycode) hostname allowlist that fails closed**
  on no match. At T1 lean on the baked `SITE_ID` as the source of truth and treat
  the host only as a hint.
- **Backend resolution:** tenant is a **path segment** (`/api/bookings/{tenant}`),
  and every handler runs `isValidSlug` (lowercase/digit/hyphen, ≤100 chars,
  path-traversal-safe) **and** checks membership in a hardcoded allowlist map —
  unknown tenant → **404**.
- **Per-tenant scoping** is by **separate secrets** (`ADMIN_TOKEN_<TENANT>`),
  **separate config docs** (Firestore `pricing_configs/{tenant}`), and **separate
  builds** — not a database tenant column.
- **Admin auth:** per-tenant **static bearer token**, constant-time compared
  (`crypto/subtle.ConstantTimeCompare`), delivered via a `?token=` magic-link that's
  stripped from the URL into `sessionStorage`.
  **⚠ Understand the trade-off:** a static bearer token **in a URL leaks** via
  browser history, server/proxy logs, screenshots, and referrers, and `sessionStorage`
  is **readable by any XSS** on the page. This is acceptable only as a **T1
  low-value-admin** shortcut with a **short-lived, rotatable** token. The hardened
  form: make the link a **one-time, expiring redeem token** that the server exchanges
  for an **HttpOnly, SameSite** session cookie (so the durable credential is never in
  the URL or JS-readable), with rotation + an audit trail. Move to real auth (Variant
  B / Firebase Auth) before this admin controls anything valuable.

**Trade-off:** dead simple, near-$0, no shared-process leak surface at all — but
doesn't scale past a handful of known tenants (every tenant is a build + a secret +
a hosting site) and has no self-serve tenant creation.

## Variant B — runtime server-derived scoping (T2, scalable) · `dogeared-coach`

**Tenants share a running process; isolation is enforced in every query.**

- **Server-derived tenant identity only.** Tenant is resolved from the
  **authenticated session**, a **custom-domain/host mapping**, a **path**, or a
  **signed invite token** — and a client-supplied **`X-Tenant-*` header is *never*
  trusted** as identity (both architecture-review AIs flagged raw-header trust as a
  cross-tenant escalation; ADR-3 / INV-006).
- **Forced tenant-scoped repository layer.** Handlers **cannot touch raw
  collections** — they go through a repo layer that injects `tenant_id` into every
  read/write. This is the fail-closed core: you can't *forget* to scope a query.
- **Schema-level enforcement.** Compound unique indexes **include `tenant_id`**;
  identity is global (`user_identity` = Firebase UID) with **per-tenant membership**
  records (one dog owner using two vets = one identity, two client records).
- **Standing isolation proof.** Integration tests **attempt cross-tenant access and
  must fail** — the cross-tenant proof is an executable gate (register it as a
  protected path so the loop can't weaken it).

**Trade-off:** self-serve tenant creation, one deploy, scales to many tenants — at
the cost of a real shared-process leak surface that the scoped-repo + proof-tests
exist to close.

## Choosing

| If… | Use |
|--|--|
| few known tenants, static frontend, no self-serve signup | **Variant A** (build-time) |
| many/self-serve tenants, shared backend, per-user accounts | **Variant B** (runtime scoped repo) |

Both are **fail-closed on unknown tenant** (404 / reject), both resolve tenancy
**server-side**, both keep tenant config out of client-trusted inputs — that
invariant is the pattern; the mechanism differs by tier.

## Cost

Variant A adds ~$0 (more builds, same free tiers). Variant B adds the cost of the
shared backend running (T2 Cloud Run) but **one** backend for all tenants — cheaper
per-tenant at scale.

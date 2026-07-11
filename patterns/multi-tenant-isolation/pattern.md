# Pattern: Multi-Tenant Isolation Stack

- **Category:** saas-infra (the isolation floor)
- **Trust class:** the tenant resolver, the scoped repository, and the proof gate are all protected paths
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The **floor** every multi-tenant SaaS stands on: one tenant's data can never be
read, written, or even *named* by another, and that guarantee is **proven by a
standing test**, not asserted in a code review. It's the layered blueprint —
server-only tenant resolution → a fail-closed tenant-scoped repository → a
cross-tenant isolation proof gate → a cancel-latched cascade erasure — that makes
everything above it (mining per-tenant signal, operator act-as, per-tenant billing)
safe to build.

The reason it's a pattern and not "add `provider_id` to your queries": correctness
here is a **cluster of invariants where a single gap is a full data breach**. A
handler that forgets to scope, a caller that's allowed to pass its own
`provider_id`, a `$rename` that smuggles the tenant key into an update, an admin
route accidentally *inside* tenant middleware — any one of those is the whole game.
The pattern makes each of those *structurally impossible* rather than
remembered.

## When to apply

Any product where more than one customer/organization shares a datastore — i.e.
almost every B2B SaaS. Stamp it **first**, before the features that depend on it;
retrofitting isolation onto a codebase that assumed a single tenant is where
breaches come from. Skip it only for genuinely single-tenant or per-tenant-DB
deployments.

## Mechanism — generic components

Neutral throughout; the tenant key (`provider_id` / `org_id` / `account_id`), the
store, and the routes are parameters.

- **Server-only tenant resolution — reject client hints.** The tenant is resolved
  from the authenticated principal (a verified claim / a membership lookup),
  **never** from a header, body field, or query param the client controls. A
  request that tries to *supply* a tenant is **rejected (400)**, not silently
  honored or overridden. Wrong-actor-class requests get an **anti-probe 404** (an
  admin on a provider route, a provider on `/admin/*`) so the surface can't be
  enumerated.
- **Fail-closed tenant-scoped repository.** A generic scoped repo where *every*
  read/write resolves the tenant from context **first** and returns
  `ErrMissingTenant` if it's absent — **there is no unscoped code path to forget to
  scope**. Filters are AND-wrapped with the tenant key; the tenant key is
  **auto-injected** on insert.
- **Reject-don't-override, recursively.** A caller-supplied tenant key in *any*
  filter/update — including deep in nested arrays, a replacement document, an
  aggregation pipeline stage, or as a **`$rename` string *value*** (`{$rename:
  {x: "provider_id"}}`) — is **rejected** (`ErrTenantOverride`), never quietly
  replaced with the "right" value. Silent-override hides the bug; rejection
  surfaces it. The scan is recursive over both keys and string values because the
  tenant key must stay immutable.
- **Unexported store handles.** The raw collection/handle is unexported so callers
  *cannot* bypass the scoped repo to issue an unscoped query — the only door is the
  fail-closed one.
- **The operator plane sits OUTSIDE tenancy.** Admin/operator routes register
  *outside* the tenant-resolution middleware and carry a system-level identity
  (a claim, not a tenant membership). A tenant identity is **orthogonal to** the
  operator identity graph, not a super-role within it — so a tenant can never
  escalate into cross-tenant reach, and an operator's cross-tenant reads happen on
  an explicitly separate, audited plane. (Shared seam with
  [`elevated-access-session`](../elevated-access-session).)
- **Standing cross-tenant isolation proof gate.** An executable test that is a
  permanent CI citizen: positive controls (a tenant *can* reach its own data) + a
  negative matrix across every vector (read, write, token, header, id-in-URL) each
  asserting **both** that the cross-tenant attempt is denied **and** that it caused
  **no mutation**, and that the denial response **leaks no foreign id**. This is
  the invariant made continuously true, not one-time reviewed.
- **Cancel-latched, resumable, transactional cascade erasure.** Deleting a tenant
  is a monotonic state machine: any **external** obligations (a live billing
  subscription) are settled **outside and before** the datastore wipe, then all
  tenant collections are erased in **one transaction**. It's **resumable**
  (fail-closed, re-entrant on a crash mid-way) and carries a **documented tombstone
  exception** — e.g. immutable tax/invoice snapshots are deliberately *not* wiped
  for legal retention. (This is a *cancel-latched transactional cascade*, not
  quarantine-then-erase.)

## Invariant cluster

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-MTI-001` | Fail-closed scoping: every repo method resolves tenant from context first, errors if absent; no unscoped path exists. | dogeared-coach `db/tenant.go:16` (`ErrMissingTenant`), `db/tenant_repo.go:44-135` |
| `INV-MTI-002` | Reject-don't-override: a caller-supplied tenant key anywhere (incl. `$rename` string values, nested arrays) is rejected; key auto-injected on insert. | `db/tenant.go:20,81-142` (`ErrTenantOverride`, recursive `containsProviderID` + `…StringValue`) |
| `INV-MTI-003` | Server-only resolution: client hints rejected (400); anti-probe 404 for wrong actor class. | `middleware/tenant.go:130-191,278-285` |
| `INV-MTI-004` | Operator plane out-of-band: admin routes register OUTSIDE tenant middleware; provider token can't reach `/admin/*`, admin token can't ride provider routes. | `INV-ADM-001/002/020`; `handlers/routes.go` (`RegisterAdminRoutes`), `middleware/admin_only.go` |
| `INV-MTI-005` | Standing cross-tenant proof gate: asserts denial AND no-mutation AND no-foreign-id-in-denial across every vector. | `integration/cross_tenant_test.go` (+ `_clients`, `_client_assignments`) |
| `INV-MTI-006` | Cancel-latched resumable transactional cascade erasure with a documented tombstone exception (billing cancelled outside+before the wipe; tax snapshots survive). | `db/erasure.go:83-…` (`DeleteTenantAll`), `services/admin_offboarding.go:14-21` |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `tenant_key` | yes | The scoping field (`provider_id` / `org_id` / …). |
| `resolver` | yes | How the tenant is derived from the authenticated principal (claim vs membership lookup). |
| `store` | yes | The datastore the scoped repo wraps (handles kept unexported). |
| `operator_routes` | yes | Route prefix(es) that register outside tenant middleware. |
| `erasure_external_obligations` | no | External systems (billing) settled before the wipe. |
| `tombstone_collections` | no | Collections deliberately retained through erasure (tax/legal). |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `cross_tenant_leak_incidents` | down | any read/write reaching another tenant's data (target 0; the proof gate asserts it). |
| `unscoped_query_paths` | down | code paths that touch the store without the fail-closed scoped repo (target 0). |
| `isolation_proof_coverage` | up | fraction of tenant-scoped collections/vectors covered by the standing proof gate. |

## Relationship to other patterns

- **`elevated-access-session`** — sits *on* this floor; the `/admin/*` operator
  plane it's denied from is this pattern's out-of-band admin plane.
- **`engagement-instrumentation` / improvement-loop** — this is the floor that makes
  mining per-tenant behavioral signal safe: the loop can read across tenants for
  analysis *only* atop fail-closed, server-derived scoping.
- **`tiered-subscription` / `fetch-on-webhook-reconcile`** — per-tenant billing
  state is projected onto the tenant record; the erasure's "settle billing before
  wipe" is where this pattern and the billing patterns meet.

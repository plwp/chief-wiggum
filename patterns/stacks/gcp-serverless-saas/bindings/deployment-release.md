# Binding: `deployment-release` → keyless WIF on GCP + GitHub Actions

- **Realizes:** [`deployment-release`](../../../deployment-release) (vendor-neutral spec)
- **Tier:** T1+ · **Vendor:** GitHub Actions + GCP Workload Identity Federation · **Source:** `booking-forms`, `dogeared-coach`

The concrete pipeline for the abstract build → stage → promote pattern. This is the
most consistent shape across the mined apps and the reference for the whole stack.

## Keyless identity (no stored cloud keys)

- **Workload Identity Federation** binds **GitHub's OIDC** to a single
  `github-deployer` service account, locked by
  `attribute.repository == '<owner>/<repo>'` (`google-github-actions/auth@v2`). No
  long-lived JSON SA keys exist anywhere — a leaked log or fork PR has nothing to
  steal.
- All WIF/CI infra is gated behind an `enable_cicd` flag, applied once from an
  operator workspace (infra is a human-run path, not something CI can grant itself).

## Environment split

- **Staging = fully automatic on push to the main branch**, after the floor:
  `pre-merge-check.sh` (Go build/test/lint + Node lint/vitest/build) gates → build &
  push the backend image to **Artifact Registry tagged with the short SHA** →
  `gcloud run deploy` (an **image-only roll**, *not* a full `terraform apply`) →
  `firebase deploy` for the frontend → **smoke-test `/healthz`** (asserts
  `"db":"ok"`).
- **Production = manual `workflow_dispatch`**, gated by a GitHub **`production`
  environment with required reviewers**. It **copies the byte-identical staging
  image** to the prod registry (docker pull → tag → push, **no rebuild**), rolls
  prod Cloud Run, and **rebuilds only the frontend** with prod Vite env.

## The two safety fences

1. **Promote the identical *backend* image — by digest.** Prod runs the exact bits
   staging validated; the backend is never rebuilt. Promote by **immutable digest**,
   not the mutable short-SHA tag (a tag can be repointed after validation), and
   record the digest as deploy provenance. The **frontend is explicitly *not*
   byte-identical** — it's rebuilt with prod env — so the honest guarantee is
   **"backend digest-identical, frontend rebuild-guarded"**, not "the whole release
   is identical".
2. **Bundle guard.** A step greps the built frontend JS and fails if staging/demo
   config leaked into the prod bundle. Treat this as a **denylist backstop, not a
   complete fence** — a grep catches the known-bad strings it's given and misses
   novel leaks. The stronger form is an **env allowlist + an artifact manifest +
   a source-map/secret scan**; keep the grep as the cheap first line.

## Least-privilege deployer

The `github-deployer` SA can **roll images and deploy hosting** but **deliberately
lacks secret-accessor and serviceusage permissions** — so it cannot call Secret
Manager directly or mutate infra; infra stays a separate human `terraform apply`.
**Caveat that matters:** a deployer that can `run deploy` can still ship an image
that executes **as the runtime SA** and reads whatever *that* SA can — so the
deployer's true privilege ceiling is the runtime SA's. Bound it from both sides:
keep the **runtime SA's secret grants minimal and per-service**, and gate **which
image** prod accepts (digest-allowlisted promote-only path), not just who may
deploy. (`ci.yml` is `workflow_dispatch`-only on private repos to save Actions
minutes.)

## Migrations

No migration tool — the stack's datastores are **additive-schema** (Firestore
schemaless; Mongo indexes created in code). Schema changes are forward-compatible by
convention. **This is under-powered for T2** and is a known sharp edge: an index
build, a backfill, or an incompatible data-shape change can break prod
*independently of a green code smoke-test*. Minimum discipline when you outgrow
"additive-only": follow **expand → migrate → contract** (add the new shape, backfill
+ dual-read, then remove the old) and add an **index-readiness check** to the smoke
step so a deploy waits on its indexes.

## Trust & protected paths

Per the [`deployment-release` trust model](../../../deployment-release/pattern.md#trust-model),
register the **workflow files, the `production` environment/reviewer config, the WIF
bindings, and the bundle-guard script** as **protected paths**. A loop may propose
pipeline improvements, but every change here is human-gated — this is the path to
prod, the highest-value goalpost in the product.

## Cost

The pipeline is **free** — GitHub Actions free minutes, Artifact Registry (0.5 GB
free), Cloud Build (120 build-min/day free) for source deploys. The only real lever
is keeping `ci.yml` manual-dispatch on private repos to conserve Actions minutes.

## Stand it up

See [`skills/deploy-gcp-wif.md`](../skills/deploy-gcp-wif.md).

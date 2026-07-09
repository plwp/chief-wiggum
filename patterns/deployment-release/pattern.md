# Pattern: Deployment & Release

- **Category:** gate (delivery)
- **Trust class:** the deploy pipeline and prod-promotion authority are protected-path (goalpost-grade)
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The **build → stage → promote** machinery that gets a change from a merged commit
to production **safely and provably**: keyless CI identity, an environment split
with a human-gated production promotion, promotion of the **byte-identical
artifact** that passed staging (so "tested" and "shipped" are the same bits), a
mechanical config-leak fence, a least-privilege deploy identity that can roll
releases but not mutate infrastructure, and post-deploy smoke verification.

It is the delivery counterpart to `build-test-floor`
(candidate): the floor answers *"does this build and pass?"*; this pattern answers
*"how does a passing build reach production without drift, leaked config, or an
over-privileged robot?"*. Most teams re-derive this per product and get one of the
subtle parts wrong (rebuilding for prod → staging/prod drift; long-lived CI keys →
credential blast radius; a deployer that can also read secrets → lateral movement).

## When to apply

Any product shipped to a hosted environment on a cadence — i.e. essentially every
SaaS. The value compounds with the number of deploys and the sensitivity of the
target; a weekend project deploying by hand can skip it, a product taking money
cannot.

## Mechanism — generic components

Neutral throughout; the CI system, cloud, and identity provider are
[parameters](#parameters).

- **Keyless federated CI identity.** CI authenticates to the cloud with a
  **short-lived token minted from the CI provider's OIDC**, scoped to the *exact*
  source repository — **no long-lived stored cloud keys**. A leaked CI log or fork
  PR cannot exfiltrate a credential that does not exist at rest.
- **Environment split with a promotion gate.** At least `staging` and `production`.
  Merge to the main branch **auto-deploys to staging** (after the floor passes);
  production is a **manual, human-gated promotion** (protected environment /
  required reviewers), never an automatic consequence of a merge. The prod trigger
  is explicit (`workflow_dispatch` / tag / signed promotion), so "we merged" and
  "we shipped" are decoupled decisions.
- **Promote the identical artifact — by digest.** Production ships the
  **byte-identical build** that passed staging, referenced by its **immutable content
  digest** (not a mutable tag, which can be repointed after validation); pull the
  staged image, retag, push, deploy the digest — **do not rebuild**. A
  rebuild can pick up a different dependency, a different base image, or different
  build-time config, silently breaking the "what we tested is what we shipped"
  guarantee. Where a per-environment rebuild is unavoidable (e.g. a frontend bundle
  that inlines the environment's public config), it is the **documented exception**
  and it runs behind the config-leak fence below.
- **Config-leak fence (build guard).** Before an artifact can ship, a mechanical
  check asserts it contains **no lower-environment config or secrets** (staging
  URLs, demo keys, test-mode flags). Fail-closed: a match blocks the release. This
  is what makes a per-environment rebuild safe when it can't be avoided.
- **Least-privilege deploy identity.** The deployer identity can **roll a release**
  (deploy an image, publish hosting) but **cannot mutate infrastructure or read
  secret values**. Infra changes (new secrets, IAM, service topology) are a
  **separate, privileged, human-run path** (e.g. `terraform apply` from an operator
  workspace), so a compromised or misbehaving deploy job has a bounded blast radius.
- **Floor-gated.** No deploy without the `build-test-floor` (candidate) gate
  passing first — build + test + lint, mirrored locally by a pre-merge script so
  the gate is reproducible off-CI.
- **Post-deploy smoke verification.** After each roll, assert a **liveness + core-
  dependency health signal** (e.g. a `/healthz` that checks the datastore) before
  the deploy is considered live; a failed smoke fails the release.
- **Secret containers, out-of-band values.** Infra provisioning creates the secret
  *containers* and grants the runtime identity read access to exactly those; the
  **values are added out-of-band** and never live in the IaC state or the repo.

## Parameters

| Parameter | What it is |
|--|--|
| `ci_provider` | the CI system minting the OIDC identity |
| `cloud` + `federated_identity` | target cloud and the keyless workload-identity binding (scoped to the repo) |
| `environments[]` | ordered env list (`staging`, `production`, …), each with its deploy trigger + gate |
| `promote_strategy` | `identical-artifact` (default) or `rebuild-guarded` (per-env rebuild behind the fence) |
| `floor_cmd` | build/test/lint floor, mirrored locally |
| `build_guard_cmd` | the config-leak fence assertion |
| `smoke_cmd` | post-deploy liveness + dependency-health check |
| `deployer_permissions` | least-privilege set: roll-release yes, mutate-infra / read-secrets no |
| `prod_promotion_authority` | how a human is required to approve prod (protected env / required reviewers / signed promotion) |

## Success metrics

Delivery health (DORA-flavored); the loop can tune cadence/gating toward these,
but the **pipeline definition itself is protected-path**, so changes to *how*
deploys happen are always human-reviewed:

| Metric | Goal | What it measures |
|--|--|--|
| `deploy_frequency` | ↑ | releases reaching prod per unit time |
| `lead_time_to_prod` | ↓ | merged-commit → live-in-prod latency |
| `change_failure_rate` | ↓ | % of prod deploys causing an incident / rollback |
| `staging_prod_drift` | ↓ | build/config divergence between staging and prod (target 0; asserted by identical-artifact + the fence) |
| `hotfix_rate` | ↓ | out-of-band emergency deploys (a proxy for gate escapes) |

## Trust model

The **deploy pipeline definition** (workflow files, promotion gate, the identity
bindings, the build guard) and **who is allowed to promote to production** are
**goalpost-grade**: a wrong change here is a direct route to shipping unreviewed or
malicious code. They belong in the [improvement-loop](../improvement-loop/pattern.md#trust-model)
**protected pathset** — the loop may *propose* pipeline improvements, but every
such change routes through **park-and-notify** (trusted signal) or the
**blocking admin-approval quarantine** (any untrusted signal). Production-promotion
authority is verified against a real authority (protected environment / CODEOWNERS
/ signed promotion), **never self-asserted** by an automated actor.

## Relationship to other patterns

- **Sits above `build-test-floor`** (candidate) — the floor
  is the pre-condition gate this pattern enforces before any deploy.
- **Emits the deploy signal `improvement-loop` needs** — deploy frequency, change-
  failure, and smoke outcomes are exactly the operational signal a post-ship loop
  consumes; the loop's own `deploy_cmd` parameter is realized by this pattern.
- **Its smoke + health surface is a [monitoring](../../docs/patterns-registry.md#monitoring--signal-is-a-pattern-group)
  signal source** — `/healthz` and post-deploy verification feed the same
  observability substrate the other patterns' metrics ride on.
- **Provider-neutral by construction** — cloud, CI, and identity provider are
  parameters, so the same pattern binds to any keyless-OIDC-capable stack.

# Patterns Registry — reusable product patterns Chief Wiggum stamps into apps

> **Status: design proposal.** This document defines the patterns registry and
> captures the first pattern (the improvement lifecycle loop). The registry entry
> lives at `patterns/improvement-loop/`. Nothing here is wired into a workflow yet
> — see [Rollout](#rollout).

## What this is (and what it is not)

Chief Wiggum already installs *artifacts* into the apps it builds: `/design`
stamps `docs/design/`, `/architect` stamps `docs/epics/*/contracts.md`. Those
are **per-product** artifacts, generated fresh each time.

A **pattern** is one level up: a proven, opinionated, reusable *architecture +
process* that CW knows how to stamp into **the app it is building** — the same
way a senior engineer carries a handful of battle-tested shapes ("put a guarded
query layer here", "gate deploys on a benchmark ratchet") from project to
project. The registry is the curated library of those shapes.

The distinction that matters:

| | Home | Scope | Trust model |
|--|--|--|--|
| **CW workflows** (`/architect`, `/implement`) | the CW checkout | operate *on* a target repo | the operator (developer at the prompt) is trusted |
| **A registry pattern** | defined in CW, **installed into the target repo** | becomes part of the *product's* own machinery, runs after CW walks away | the product's **end users may be untrusted** — this is the new axis |

The registry is **not** about Chief Wiggum's own internals. CW itself is improved
**directly, human-in-the-loop, through Claude Code** — the developer at the
prompt *is* the admin, so every CW change is already admin-gated by construction.
CW does not need an autonomous self-improvement loop bolted onto it. (The
improvement-loop pattern is nonetheless *available* to point at CW or a CW
subsystem later, if a case appears where an autonomous nightly pass earns its
keep — see [Applying a pattern to CW itself](#applying-a-pattern-to-cw-itself).)

## Why a registry

Right now the valuable, reusable shapes CW could install are trapped inside the
one-off apps that happen to implement them (a self-improvement loop is one). When
CW builds the next analytics agent, nothing carries that loop forward — it would
be re-derived, or forgotten. A registry:

1. **Captures** each shape once, generically, with its parameters made explicit.
2. **Catalogs** them so `/seed` and `/architect` can *select* patterns as part
   of designing a product ("this is an agent over a warehouse → apply the
   guarded-query + improvement-loop patterns").
3. **Stamps** the pattern's scaffold into the target repo with its parameters
   bound, then registers its protected paths with the product's ratchet.
4. **Governs** the trust boundary — the piece a trusted-insider loop can skip but
   a public-facing app cannot (see [Trust model](#trust-model-the-core-generalization)).

## Registry layout

```
patterns/
├── registry.json                 # the index: specified patterns + mined candidates + meta-disciplines + stacks pointer
├── improvement-loop/             # specified pattern
│   ├── pattern.md                # the spec: what / when-to-apply / mechanism / parameters
│   ├── manifest.json             # machine-readable: params, installs[], protected_paths, trust
│   └── scaffold/                 # (future) the templated files stamped into the target app
├── engagement-instrumentation/   # specified pattern — the signal tier that feeds the loop
│   ├── pattern.md
│   └── manifest.json
├── <candidate patterns>/         # mined, catalogued in registry.json, not yet fully specified
└── stacks/                       # CONCRETE layer: vendor-bound profiles (the factory) — see #stack-profiles
    ├── registry.json             # index of stack profiles + the cost-tier ladder
    └── gcp-serverless-saas/
        ├── stack.md              # the house stack: topology, cost tiers, graduation triggers, provenance
        ├── manifest.json         # machine-readable: vendors, tiers, bindings, cost model, skills
        ├── bindings/             # pattern × this-stack → concrete recipe (one .md per bound pattern)
        └── skills/               # runnable stand-up playbooks ("the skills to use them")
```

Each specified pattern is a directory. Two files are the contract:

- **`pattern.md`** — the human-facing spec. What the pattern is, when to apply it,
  the mechanism (as a set of generic components), its parameters, its
  **success metrics**, and its trust requirements.
- **`manifest.json`** — the machine-readable surface a future `apply-pattern`
  script consumes: parameter schema, the files/scaffold it installs, the paths it
  adds to the product's protected pathset, its **`success_metrics`**, and its
  trust class.

Declaring **success metrics is mandatory**, not decorative — see
[Success metrics make patterns improvable](#success-metrics-make-patterns-improvable).

`registry.json` is a thin index over the directories so a workflow can list and
filter patterns without opening each manifest. It also carries **candidates**
(patterns mined but not yet fully specified) and **meta-disciplines** (recurring
cross-pattern rules — fail-closed, idempotency, injected seams, documented TOCTOU).

## How a pattern gets applied (`/apply-pattern`)

A pattern is installed into a target repo by a thin workflow
(`.claude/commands/apply-pattern.md` → `scripts/apply_pattern.py`):

```
/apply-pattern owner/repo --pattern fetch-on-webhook-reconcile --param resource=subscription
```

Mechanically — the **contract-pack** steps (1, 2, 4, 5) are **built**; scaffold
stamping (3) is deferred until patterns ship a `scaffold/`:

1. **Resolve** the target repo (`scripts/repo.py`, as every workflow does). ✅
2. **Bind parameters** — read `manifest.json`'s parameter schema and resolve each
   from the target app's context (its signal sources, its build/test commands,
   its model family, its protected paths). Unresolved **required** params become
   `TBD:` markers in the installed contract pack, gated exactly like every other
   CW artifact (`scripts/check_unresolved.py`). ✅
3. **Stamp** the scaffold into the target repo with parameters bound. ⏳ *(deferred
   — no pattern ships a `scaffold/` yet.)*
4. **Register protected paths** — add `docs/patterns/**` (the installed contract
   pack) to the product's `docs/quality/ratchet.json`, so the adopted cluster
   becomes a goalpost the app's autonomous machinery cannot move (reuses the
   existing [ratchet](ratchet.md)). The manifest's prose `protected_paths` are
   recorded in the adoption record as *intents* for a human to map to real code
   paths. ✅
5. **Record adoption** — write `docs/patterns/adopted.json` in the target repo
   with the pattern id, version, bound parameters, cluster ids, and provenance,
   plus `docs/patterns/<id>/invariants.md` — the invariant cluster as a stable-id
   contract pack `/architect` folds into the epic's `invariants.md`. ✅

Patterns are **project-agnostic** and installed by *value* into the target — the
same principle as every other CW skill. CW never hardcodes the target's
warehouse, model, or channel; those are parameters.

## How patterns thread through the existing workflow

Applying a pattern is not a one-shot stamp — it threads through the existing
build loop. The division of labor: **`/seed` selects, `/architect` binds,
`/implement` builds, `/close-epic` verifies.** A registry pattern is best thought
of as a **contract pack** — it ships stable-ID contracts, invariants, integration
tests, protected paths, and parameters, and the workflow stages consume them
rather than re-deriving.

| Stage | Role with patterns |
|--|--|
| **`/seed`** (or the product-architecture step) | **Selects.** Proposes applicable patterns from the registry given the product's shape ("multi-tenant SaaS with untrusted users" → `multi-tenant-isolation` + `engagement-instrumentation` + `improvement-loop`), records the choice and the per-app trust bindings — the "chosen, not converged" moment, like `/design`. |
| **`/architect`** | **Binds.** For each selected pattern this epic realizes: folds the pattern's contracts/invariants into `contracts.md` / `invariants.md` **with their stable IDs, pulled from the manifest** (not re-derived); threads its integration tests into `integration-tests.md`; registers its `protected_paths` into `docs/quality/ratchet.json`; and emits `TBD:` markers for any unbound parameter (gated by `check_unresolved.py`) so dependent tickets can't build on a guess. |
| **`/implement` / `/implement-wave`** | **Builds.** Implements against the pattern-supplied contracts; the `scaffold/` may already be stamped by `/apply-pattern`, and tickets fill in the app-specific parameterization. Workers touching the pattern's protected paths are parked, exactly as with any goalpost. |
| **`/close-epic`** | **Verifies.** Confirms the pattern's invariants and gates held across the epic (e.g. the cross-tenant isolation proof passes, the ratchet held, the trust gate is wired). |

So the answer to "does `/architect` need to reference the patterns?" is **yes** —
`/architect` is where a pattern stops being a catalog entry and becomes this
epic's contracts, gates, and protected paths. Concretely: `multi-tenant-isolation`
contributes the tenant-scoping invariant + the cross-tenant-proof integration
test; `engagement-instrumentation` contributes the trusted-denominator +
monotonic-latch contracts; `improvement-loop` contributes the protected-pathset +
ratchet-gate + trust-model contracts.

## Patterns as clusters of invariants

The organizing principle underneath everything above: **a pattern *is* a cluster
of invariants that must hold together, plus the playbook to satisfy them.** The
prose, the scaffold, and the gates are all in service of keeping that cluster
true. This is what turns the registry from a snippet library into a way to **not
re-derive connected requirements every time**.

The failure this prevents is concrete. Someone builds a billing webhook and copies
the idempotency shape from a video-upload webhook — a terminal-state FSM guard.
It's silently wrong, because subscription state is non-monotonic: a stale
redelivery mis-grants entitlement. The fix isn't one line — it's a *cluster* of
invariants that only make sense together: *event-is-trigger-fetch-is-truth* +
*unknown-id-is-fatal-never-a-silent-floor* + *retry-lease-not-drop* +
*terminal-deletion-latch*. Miss any one and the money path leaks. A senior
engineer carries that whole cluster in their head; a snippet doesn't. The registry
carries it as [`fetch-on-webhook-reconcile`](../patterns/fetch-on-webhook-reconcile/pattern.md)
— the worked example of this section.

Mechanically, every pattern (specified or candidate) carries an **`invariants`**
array. Each entry is:

```json
{
  "id": "INV-FOWR-004",
  "statement": "Unknown external id is fatal: no write, no floor fallback, alert.",
  "realized_as": {"app": "dogeared-coach", "id": "INV-BIL-012",
                  "code": "services/billing_reconcile.go:37-40,256-267"}
}
```

- **`id`** is the pattern's *own* generic stable id (`INV-<ABBR>-NNN`) — the pattern
  is vendor- and product-neutral, so it owns neutral invariant ids.
- **`statement`** is the invariant in one line: the thing that must stay true.
- **`realized_as`** is **provenance** — the app-specific id (`INV-BIL-012`) and the
  real code that proves the invariant is buildable, not aspirational. Mining an app
  is exactly *"which generic invariant did this app-specific `INV-` realize, and
  where."*

### Why this pays off

1. **`/architect` pulls the cluster by id, doesn't re-derive it.** Binding a
   pattern into an epic means copying its `invariants` cluster into the epic's
   `invariants.md` with the stable ids — the [existing threading](#how-patterns-thread-through-the-existing-workflow)
   already says patterns ship stable-ID invariants; this makes the invariant set
   the *primary* thing a pattern ships. The connected requirements arrive as a set,
   so you can't adopt half of them by accident.
2. **The gates already operate on invariants.** `check_traceability.py`,
   `check_single_writer.py`, and the ratchet all key off `INV-`/`CTR-` stable ids.
   A pattern expressed as an invariant cluster drops straight into the machinery
   that already holds invariants monotonic — no new enforcement layer.
3. **The playbook travels with the cluster.** The [stack profile](#stack-profiles--the-concrete-layer-the-factory)
   bindings/skills are *how to satisfy this cluster on a concrete stack*. Cluster =
   "what must stay true"; binding = "how to make it true here." Selecting a pattern
   gets you both, so the playbook isn't rediscovered either.
4. **Mining becomes mechanical.** "Extract patterns from app X" = "group X's
   `INV-`/`CTR-` ids into clusters, name the generic invariant each realizes, cite
   the code." An `INV-` that belongs to no pattern cluster is either a candidate for
   a new pattern or genuinely app-specific.

### Composition

Clusters compose the way patterns do. `entitlement-overlay`'s "reconcile recomputes
`max(payer, overlay)`" invariant (`INV-EO-004`) only closes because
`fetch-on-webhook-reconcile`'s single-writer projection (`INV-FOWR-003`) is what
does the recompute — they share the same `projected_field`. Reading two patterns'
clusters side by side surfaces these seams, which is precisely the "connected
requirements" a copy-paste loses.

> A pattern that can't state its invariant cluster isn't specified yet — it's a
> vibe. Declaring the cluster is the bar for `status: specified`, the same way
> [declaring a success metric](#success-metrics-make-patterns-improvable) is.

This bar is **mechanically enforced**, not trusted: `scripts/check_patterns.py`
(wired into `make lint`, so it runs in CI) fails the build if any `specified`
pattern lacks a non-empty invariant cluster, if an invariant id is malformed or
duplicated within a pattern, if a `realized_as` provenance block is present but
ill-formed, or if a `depends_on`/`feeds` reference dangles. A specified pattern
that depends on a not-yet-specified candidate is reported as a *warning* (tracked
debt), not an error — consistent with the smallest-first rollout.

## Success metrics make patterns improvable

Every pattern **must declare its own success metrics** (`success_metrics` in the
manifest, a table in `pattern.md`). This is the hinge that turns a pattern from a
static scaffold into a **self-improving unit**, and it's what makes the whole
registry more than a snippet library:

> **A pattern's metric is its objective.** The metric defines "is this working";
> the [monitoring group](#monitoring--signal-is-a-pattern-group) captures it as
> trust-tagged signal; the [improvement loop](../patterns/improvement-loop/pattern.md)
> optimizes the pattern toward it; the ratchet holds it monotonic so it only ever
> improves. Without a declared metric, the loop has nothing to optimize and the
> pattern can only ever be as good as the day it was stamped.

Each metric carries a **goal direction** (`up` / `down` / `context`), so a
downstream loop knows which way is better without a human re-explaining it every
iteration. Examples already specified:

- `frictionless-onboarding` → `activation_rate ↑`, `time_to_first_value ↓`,
  `free_to_paid_conversion ↑`.
- `tiered-subscription` → `mrr ↑`, `dunning_recovery_rate ↑`, `revenue_leak ↓`.
- `engagement-instrumentation` → `signal_coverage ↑`, `latch_integrity ↑` (a
  monitoring pattern's metrics are about the *quality of the instrument*).
- `improvement-loop` → `highwater_trend ↑`, `escaped_defect_rate ↓`.

Because monetization and conversion metrics are optimized from **end-user
behavioral signal** and touch **pricing/paywall goalposts**, loop-proposed changes
against them are exactly what the [trust model](#trust-model-the-core-generalization)
routes to **blocking admin approval** — the pattern iterates continuously, but a
human signs off on changes to how the product makes money.

## Monitoring & signal is a pattern group

Patterns fall into groups, and **monitoring / signal** is a first-class one — the
substrate the rest depend on, because *every* pattern's success metrics have to be
captured by *something*. It's the supply side of the "declare a metric" contract
above.

| Group | Members (specified + candidate) |
|--|--|
| **monitoring & signal** | `engagement-instrumentation` (flagship); candidates: dual-scope `audit-log`, error-signature ingestion + `signal-ingestion + findings` (from the loop), funnel/activation capture |
| **process loops** | `improvement-loop`, `reconciliation-sweep`, `transactional-email-and-dunning`, `referral-invite-loop` |
| **monetization / growth** | `tiered-subscription`, `frictionless-onboarding`, `feature-entitlements`, `self-serve-billing-portal` |
| **saas-infra** | `multi-tenant-isolation`, `provider-neutral-adapter`, `elevated-access-session` |
| **gates** | `build-test-floor`, `protected-pathset + ratchet`, `decorrelated-judge`, `gate-only-holdout` |

The three groups compose into one loop: **monitoring** captures each pattern's
declared metric → the **improvement loop** (a process loop) optimizes toward it →
**gates** keep it from sliding back → and the **monetization/saas-infra** patterns
are the surfaces being improved. Grouping also guides `/seed`'s selection: pick the
saas-infra floor, the monetization surface you're monetizing, and always the
monitoring group underneath so the rest are measurable.

## Trust model — the core generalization

This is the piece a **trusted-insider** loop can skip, and the reason a registry
(rather than a copy-paste) is worth building.

A baseline autonomous improvement loop runs fully autonomous to production. Its
*one* human touchpoint is **non-blocking park-and-notify**: when a change touches
the protected pathset, or needs domain truth the agent can't verify, it posts one
message to a human channel and **parks** the item — it never waits for a reply.
That is safe **only because its signal sources are trusted insiders**: the people
whose feedback and conversations drive the loop are authenticated employees.

Generalize the loop to an app with **untrusted end users** and those same signal
inputs — user feedback text, conversation transcripts — become a **prompt-injection
surface**. A malicious user can craft feedback engineered to get the loop to
diagnose a "fix" that weakens a guardrail, rewrites a business rule, or plants a
backdoor. Park-and-notify is not enough: a parked-but-eventually-auto-applied
change is still an attacker-influenced change reaching production.

The registry closes this with two additions to the pattern-as-installed:

### 1. Signal sources carry a trust level

Every signal source declared in the pattern's parameters is tagged
`trusted` or `untrusted`:

- `trusted` — internal/authenticated-admin origin (named insiders,
  operator-authored benchmark cases, runtime error logs the app emits about
  itself).
- `untrusted` — any end-user-supplied content (public feedback, conversation
  text from unauthenticated or low-privilege users).

Trust flows through the whole chain: every `Finding` inherits `signal_trust`
from its source, and every proposed change inherits the *lowest* trust of the
findings in its cluster. (Runtime error logs the app emits about itself stay
`trusted` even in a public app — the app is describing its own failure, not
relaying user words.)

### 2. Untrusted-derived changes require blocking admin approval

The human gate becomes **trust-conditional**:

| Change provenance | Gate |
|--|--|
| Derived **only** from `trusted` signals, touches no protected path | Autonomous fix-forward (the baseline model): floor + ratchet, auto-deploy |
| Touches a **protected path** (any provenance) | Park-and-notify (the baseline model) — goalpost changes always human-reviewed |
| Derived from **any** `untrusted` signal | **Quarantine → blocking admin approval** before it can enter the deployable set |

The third row is the new one. An untrusted-derived change:

1. Is written to a **quarantine** (`docs/patterns/pending-approval/<id>/` in the
   target repo): the proposed diff + its full provenance chain (which findings,
   which signals, verbatim source text) + the trust classification.
2. **Cannot deploy.** It is not a parked-but-eventual change; it is inert until
   an admin acts.
3. Is released only by an **admin-authenticated approval** —
   `patterns approve <id> --admin <identity>` — where admin identity is verified
   against a real authority (CODEOWNERS / a signed approval commit / an
   operator allowlist), **not** self-asserted by the loop.
4. On approval, the change (and *who* approved it) is appended to the existing
   **tamper-evident hash-chained journal** (the ratchet journal), so approvals
   are auditable and un-forgeable — you can always prove an untrusted-derived
   change reached prod *only* through a named admin.

The result: a malicious end-user's feedback can, at worst, produce a **proposal
in quarantine**. It can never silently become a deployed guardrail change or a
business rule. Business rules derived from untrusted feedback are likewise marked
`unverified` until an admin confirms them — a "domain truth needs a human ruling"
idea, but promoted from a *domain-uncertainty* trigger to a *trust* trigger and
hardened from park-notify into a blocking gate.

This trust axis is **declared per pattern** in `manifest.json` and **bound per
app** at apply time: an internal-only tool binds every source `trusted` and gets
the frictionless autonomous loop; a public app binds its user-feedback source
`untrusted` and automatically gets the quarantine gate. Same pattern,
trust-appropriate behavior, no fork.

## Candidate patterns (the backlog the registry seeds)

Candidates come from two sources: mechanisms the improvement loop decomposes into,
and patterns **mined from shipped apps** CW has built.

### From the improvement loop

The loop distills into a set of mechanisms
(`patterns/improvement-loop/pattern.md` catalogs them), several strong enough to
stand alone:

| Candidate | One-liner | Notes |
|--|--|--|
| **protected-pathset + ratchet** | Monotonic quality high-water mark + fenced goalposts, embedded in the product | CW already runs this on *its own* work; the pattern is embedding it *in the built app* |
| **decorrelated-judge / shadow-audit** | Referee model family ≠ player model family; blind re-generation to catch shared blind spots | For any product with an LLM-judge or generative success path |
| **gate-only-holdout** | Train/holdout eval split; holdout withheld from the fixer's context | Anti-overfit for any self-modifying, test-graded product |
| **signal-ingestion + findings** | Heterogeneous signals → one uniform, pointer-only, idempotent `Finding` shape | The abstraction that makes the loop source-agnostic; carries the trust tag |
| **business-rules-registry** | Human corrections captured as provenance-bearing, version-controlled rules that outrank inference | The admin-approval trust gate rides on this |

### Mined from a shipped multi-tenant SaaS app

Mining an existing production app (genericized — no app, domain, or vendor names)
surfaced a coherent set of reusable shapes. Two observations shaped how they land:

- The **feedback/instrumentation stack** was promoted to its own specified pattern
  ([`engagement-instrumentation`](../patterns/engagement-instrumentation/pattern.md))
  because it is precisely the signal supply the improvement loop's *enabling
  condition* (strong monitoring + feedback) calls for — and one of its
  sub-patterns, a documented per-metric **trust-boundary "honesty note"**,
  directly reinforces the [trust model](#trust-model-the-core-generalization):
  a loop that ingests a signal must know that signal's trust class or it will
  optimize against a gameable proxy.
- The **multi-tenant isolation stack** is the *floor* that makes mining per-tenant
  behavioral data safe — the loop can read across tenants for analysis without
  leaking between them only atop a fail-closed, server-derived scoping layer.

| Candidate | Category | One-liner |
|--|--|--|
| **multi-tenant-isolation** | saas-infra | Server-only tenant resolution → fail-closed tenant-scoped repository → standing cross-tenant isolation proof gate → quarantined cascade erasure. One multi-tenancy blueprint |
| **provider-neutral-adapter** | multi-provider | Vendor-agnostic seam (neutral DTOs, unexported concrete types = compile-time swappability); behind it: signed-webhook-as-source-of-truth, sign-per-request access tokens, direct browser→CDN upload |
| **reconciliation-sweep** | process-loop | Periodic bidirectional local↔external drift repair, fail-closed on unknown state, re-confirm before destroy, per-run counts report |
| **immutable-assignment-snapshot** | data-structure | Version-pin assigned composite items so template edits never mutate outstanding assignments; keeps completion analysis drift-free |
| **elevated-access-session** | saas-infra | Revocable, time-boxed support impersonation with independent TTL, future-skew guard, fail-closed revocation, full audit |
| **build-test-floor** | gate | Language-agnostic auto-detecting build+test+lint gate with local mirror; optionally zero-cost-until-opt-in |

Plus **meta-disciplines** recurring across the mined patterns — fail-closed on
unknown input, idempotency + guarded conditional updates, injected interface seams
for every gate, documented-not-hidden TOCTOU limitations, external state
authoritative via signed webhooks not client redirects — catalogued in
`registry.json` as rules rather than installable patterns.

### Mini-SaaS growth & monetization

The reusable building blocks every product-led SaaS re-implements. Two were
promoted to **specified** patterns (they carry the clearest "re-used *and improved
on*" story — the loop optimizes them over time, admin-gated because they touch how
the product asks for money):

- [`tiered-subscription`](../patterns/tiered-subscription/pattern.md) — subscribe →
  enforce → lifecycle, with billing webhooks as source of truth and **non-destructive**
  degradation on downgrade/lapse. Absorbs the mined `tiered-saas-enforcement`.
- [`frictionless-onboarding`](../patterns/frictionless-onboarding/pattern.md) —
  value-first free tier, contextual upgrade prompts at limit-hit friction points,
  optional reverse trial. Consumes `tiered-subscription` + `engagement-instrumentation`.

The **through-line** the loop makes real: a limit-hit `409` from tiered-subscription
is the upgrade trigger for onboarding; the resulting conversion funnel is captured
by engagement-instrumentation as trust-tagged signal; the improvement loop optimizes
prompt copy / thresholds / trial length against that funnel — but every such change
is **admin-gated**, because it's driven by end-user behavior (an injection surface)
and touches pricing/paywall (a goalpost). Continuously improved, human always signs
off on the ask for money.

Supporting monetization candidates in `registry.json`:

| Candidate | Category | One-liner |
|--|--|--|
| **transactional-email-and-dunning** | process-loop | Idempotent, provider-neutral lifecycle messaging: welcome, activation nudge, re-engagement, failed-payment dunning with bounded retries + send-once keys. Recovery outcomes are retention signal |
| **referral-invite-loop** | process-loop | Invite → signed single-use expiring token → attribution → two-sided reward. Reuses the signed-token discipline; a self-serve growth loop |
| **feature-entitlements** | saas-infra | One resolver: capability flags from tier + per-account overrides + grandfathering, queried identically by backend gates and frontend UI |
| **self-serve-billing-portal** | saas-infra | User-managed plan / payment / seats; provider-hosted portal session + a webhook-authoritative local mirror. Kills the #1 support-ticket class |

Fully specifying the remaining candidates (a `pattern.md` + `manifest.json` each)
is future work; the registry structure is built to hold them.

## Stack profiles — the concrete layer (the factory)

Everything above is deliberately **vendor-neutral**: `provider-neutral-adapter` is
the seam, `tiered-subscription` names no billing vendor. That abstraction is
correct — but a catalog of seams is not yet a **factory**. To actually *stamp a
working product* you need an opinionated, real-vendor **default** for each seam,
wired the way that has already shipped, with the runnable steps to stand it up and a
cost model so you know the zero-cost PoC footprint and where money starts.

A **stack profile** (`patterns/stacks/<id>/`) is that default. It **binds** the
abstract patterns to a concrete infrastructure stack, ships the **skills** to stand
each piece up, and carries a **cost model**. The abstract pattern stays neutral (you
can swap Cloud Run for Fly, Stripe for Paddle); the profile is the bound default the
factory reaches for first.

```
  ABSTRACT (patterns/<id>)         CONCRETE (patterns/stacks/<id>)
  vendor-neutral seam         ×    named vendor + wiring    →   runnable recipe + cost
```

Each profile is four things:

- **`stack.md`** — the house stack: vendor table, the **cost-tier ladder**, the
  **graduation triggers** between tiers, provenance (which shipped apps it was mined
  from), and honest known-gaps.
- **`manifest.json`** — machine-readable: `vendors`, `cost_tiers`,
  `graduation_triggers`, `bindings` (pattern → recipe → source app), `skills`.
- **`bindings/*.md`** — one per bound pattern: the concrete realization *on this
  stack*, including the non-obvious glue and the gotchas mined from real code.
- **`skills/*.md`** — runnable stand-up playbooks with real commands (the *"skills
  to use them"*).

### The cost axis is first-class

A micro/mini-SaaS factory lives or dies on **zero-cost PoC deployments and
predictable cost scaling**, so every profile declares a **tier ladder**: the lowest
tier is a genuine ~$0 footprint (scale-to-zero compute, free-tier datastore, no
vendor), and each higher tier adds one seam with its cost and a **graduation
trigger** (the concrete signal that says "now add this"). The single most important
number a profile surfaces is the **first real fixed-cost jump** and the **only
uncapped variable cost** — so a builder knows exactly what they're signing up for.

### First profile: `gcp-serverless-saas`

The house stack behind every CW-built app so far — **Firebase Hosting + Cloud Run
(Go) + Firestore/Atlas + Firebase Auth + Stripe + Resend + Secret Manager + keyless
WIF deploys** — presented as a **T0 → T1 → T2 cost ladder** ($0 static/DIY →
$0–5 thin-serverless → $60–140 full-production). It was mined from three shipped
apps (`plwp.net`, `booking-forms`, `dogeared-coach`) and binds
`tiered-subscription`, `engagement-instrumentation`, `multi-tenant-isolation`
(two variants), `provider-neutral-adapter`, `transactional-email-and-dunning`
(dunning half flagged aspirational — honestly unbuilt in the mined apps), and
`deployment-release`. See [`patterns/stacks/gcp-serverless-saas/stack.md`](../patterns/stacks/gcp-serverless-saas/stack.md).

### How a stack profile threads through the workflow

A profile extends, not replaces, the [pattern threading](#how-patterns-thread-through-the-existing-workflow):
`/seed` selects the patterns *and* a stack profile for the product's cost tier;
`/architect` binds the profile's concrete contracts (still with the abstract
patterns' stable IDs); `/apply-pattern --stack` stamps the profile's scaffold and
runs the stand-up skills; the cost tier chosen becomes an explicit product decision
(like a chosen design direction). Every vendor stays swappable because the binding
sits *below* the abstract pattern's neutral seam.

## Applying a pattern to CW itself

Because CW is also just a repo, the improvement-loop pattern *can* target it.
That is **not the default** and not needed for normal CW development (you improve
CW directly through Claude Code, and you are the trusted admin). But if a bounded,
well-benchmarked CW subsystem emerges where an autonomous nightly pass earns its
keep — say, a suite whose failures are mechanical and gradeable — the pattern is
available to point at it. If that ever happens, every CW signal source is
`trusted` (there are no untrusted end users at the CW prompt), so it runs in the
frictionless autonomous mode with no quarantine gate.

## Rollout

Smallest-first. Progress so far:

1. ✅ **Registry structure** + the invariant-cluster model, with 9 specified
   patterns (`pattern.md` + `manifest.json` each) mined + grounded, and
   `check_patterns.py` enforcing the model in CI.
2. 🟡 **`/apply-pattern` skill** — the thin **contract-pack** installer is built
   (`scripts/apply_pattern.py` + `.claude/commands/apply-pattern.md`): binds
   params, stamps the invariant cluster + adoption record, registers protected
   paths. The pattern `scaffold/` (stamping code, not just contracts) is still to
   come.
3. ⏳ **`/seed` + `/architect` integration** — pattern *selection* becomes part of
   product design: the architecture stage proposes applicable patterns and records
   the choice, the way `/design` records a chosen design direction; `/architect`
   folds an adopted cluster into the epic's `invariants.md` by stable id.
4. ⏳ **Fill the backlog** — capture the remaining candidate patterns as their own
   entries.

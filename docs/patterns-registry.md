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
├── registry.json                 # the index: id, category, status, trust-class, one-liner
├── improvement-loop/             # pattern #1 (this proposal)
│   ├── pattern.md                # the spec: what / when-to-apply / mechanism / parameters
│   ├── manifest.json             # machine-readable: params, installs[], protected_paths, trust
│   └── scaffold/                 # (future) the templated files stamped into the target app
└── <future patterns>/
```

Each pattern is a directory. Two files are the contract:

- **`pattern.md`** — the human-facing spec. What the pattern is, when to apply it,
  the mechanism (as a set of generic components), its parameters, the reference
  implementation it was distilled from, and its trust requirements.
- **`manifest.json`** — the machine-readable surface a future `apply-pattern`
  script consumes: parameter schema, the files/scaffold it installs, the paths it
  adds to the product's protected pathset, and its trust class.

`registry.json` is a thin index over the directories so a workflow can list and
filter patterns without opening each manifest.

## How a pattern gets applied (proposed `/apply-pattern`)

A pattern is installed into a target repo by a new thin workflow (or a mode of
`/architect`):

```
/apply-pattern owner/repo --pattern improvement-loop
```

Mechanically:

1. **Resolve** the target repo (`scripts/repo.py`, as every workflow does).
2. **Bind parameters** — read `manifest.json`'s parameter schema and resolve each
   from the target app's context (its signal sources, its build/test commands,
   its model family, its protected paths). Unresolved facts become `TBD:`
   markers, gated exactly like every other CW artifact
   (`scripts/check_unresolved.py`).
3. **Stamp** the scaffold into the target repo with parameters bound.
4. **Register protected paths** — add the pattern's `protected_paths` to the
   product's `docs/quality/ratchet.json`, so the pattern's own guards/objective
   become goalposts the app's autonomous machinery cannot move
   (reuses the existing [ratchet](ratchet.md)).
5. **Record adoption** — write `docs/patterns/adopted.json` in the target repo
   with the pattern id, version, bound parameters, and provenance. This is the
   product's manifest of "which CW patterns am I running, and how were they
   configured".

Patterns are **project-agnostic** and installed by *value* into the target — the
same principle as every other CW skill. CW never hardcodes the target's
warehouse, model, or channel; those are parameters.

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

The improvement loop distills into a set of mechanisms
(`patterns/improvement-loop/pattern.md` catalogs them). Several are strong enough
to stand alone as their own registry entries CW could apply independently:

| Candidate pattern | One-liner | Notes |
|--|--|--|
| **improvement-loop** | Autonomous, signal-driven, ratchet-gated fix-forward refinement | Pattern #1, this proposal |
| **protected-pathset + ratchet** | Monotonic quality high-water mark + fenced goalposts, embedded in the product | CW already runs this on *its own* work; the pattern is embedding it *in the built app* |
| **decorrelated-judge / shadow-audit** | Referee model family ≠ player model family; blind re-generation to catch shared blind spots | For any product with an LLM-judge or generative success path |
| **gate-only-holdout** | Train/holdout eval split; holdout withheld from the fixer's context | Anti-overfit for any self-modifying, test-graded product |
| **signal-ingestion + findings** | Heterogeneous signals → one uniform, pointer-only, idempotent `Finding` shape | The abstraction that makes the loop source-agnostic; carries the trust tag |
| **business-rules-registry** | Human corrections captured as provenance-bearing, version-controlled rules that outrank inference | The admin-approval trust gate rides on this |
| **build-test-floor** | Language-agnostic auto-detecting build+test gate | A generic pre-merge check the loop's floor reuses |

Capturing these is future work; the registry structure is built to hold them.

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

Proposed, smallest-first:

1. **This PR** — the registry structure + pattern #1 captured as a spec
   (`pattern.md` + `manifest.json`) + this design doc. No workflow wiring yet.
2. **`/apply-pattern` skill** — the thin installer described above, plus the
   `scaffold/` for pattern #1 (the generic loop skill + scripts, with the
   trust/quarantine gate added).
3. **`/seed` + `/architect` integration** — pattern *selection* becomes part of
   product design: the architecture stage proposes applicable patterns and
   records the choice, the way `/design` records a chosen design direction.
4. **Fill the backlog** — capture the standalone candidate patterns above as
   their own entries.

Steps 2–4 are deliberately out of scope here — this PR is about *starting to
capture*, with one real, fully-specified entry to prove the shape.

# Pattern: Improvement Lifecycle Loop

- **Category:** process-loop
- **Trust class:** trust-aware (behavior depends on per-app signal-trust binding)
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

A self-improvement loop installed **into the product** that acts as the product's
**error function**: after CW ships the app, the loop runs on a schedule, consumes
the app's runtime signals (conversations, user feedback, error logs, real
requests), diagnoses each into a finding, clusters findings into structural
issues, fixes them forward across any product surface (prompts, contracts, config,
code), gates the change on a deterministic ratchet, and — depending on the trust
of the signals that drove it — either auto-deploys or quarantines for admin
approval.

It is the **operate/refine** counterpart to CW's **build** loops. CW's existing
loops take an app from nothing to shipped (`/seed → /architect → /implement-wave
→ /close-epic`). This pattern is what the app runs *itself*, afterward, to keep
improving without a human writing every ticket.

## When to apply

The one enabling condition is **strong monitoring and feedback**. Wherever a
product emits rich signal about how it's actually doing — quality-gradeable
outcomes, explicit user corrections, structured error telemetry — the loop has
something real to act on. This is not specific to conversational or analytics
agents; any system with good observability + feedback is a candidate. Conversely,
a product with weak monitoring has no error signal, and the loop has nothing to
drive it — don't bother.

Signal strength gets the loop *value*. Two further properties govern *how
autonomous* it's allowed to be — this is a spectrum, not a yes/no:

| Monitoring / feedback | Deterministic gate (benchmark + trust) | Loop runs as |
|--|--|--|
| Strong | Strong — golden benchmark to ratchet against, and trusted signals | Fully autonomous fix-forward, auto-deploy |
| Strong | Weak / none, **or** any untrusted signal | Loop still surfaces, diagnoses, and *proposes* — but **every change is human-gated** (quarantine → approval; no auto-deploy) |
| Weak | (any) | Not a candidate — no error signal to drive it |

So a **deterministic benchmark** and a **safe blast radius** (editable surfaces
separable from the goalposts the loop must not move) aren't preconditions for
running the loop — they're what unlocks the *autonomous* column. Without them, or
with untrusted signals, the loop is still valuable as a human-gated proposer. The
[trust model](#trust-model) is the mechanism that places a given change in the
right row.

## Mechanism — generic components

Every component is generic and technology-agnostic. Concrete choices
(warehouse/datastore, operational vs judge model, notification channel, deploy
target, ticketing system) are **parameters** — see [Parameters](#parameters).

### Loop shape

- **Inner/outer split** — cheap continuous per-item diagnosis (inner) feeds a
  periodic batch improvement (outer). Per-item granularity is what makes later
  clustering possible.
- **One skill invocation = one iteration**, scheduled via cron/`/loop` (e.g.
  nightly), **not** CI-triggered. Cloud automation (a scheduler invoking the skill
  headless) is a deferred deployment shape, not a requirement.

### Signals in (the error-function inputs)

- **Uniform Finding abstraction** — every heterogeneous source normalizes to one
  `Finding` shape: pointer-only evidence (trace/conversation/execution id, never
  inlined PII), a stable idempotency key of `(source_id, signals_digest)` that
  deliberately excludes agent version so the same problem dedupes across builds,
  and a **`signal_trust` tag**.
- **Conversation + feedback ingestion** — real transcripts and explicit user
  corrections. Feedback outranks inference: the user's text becomes both
  `root_cause` and a `proposed_fix`, with provenance.
- **Runtime error ingestion** — operational failures (panics, guardrail trips,
  5xx) grouped by a **normalized signature** (volatile tokens stripped) with a
  `sufficient|INSUFFICIENT` context flag. Diagnosed *without* the judge (objective
  evidence).
- **Real-backlog ingestion** — resolved real requests with known-good answers
  become benchmark cases extending the same schema; unanswered ones are
  judge-graded and kept out of the strict ratchet.

### Diagnose (inner loop)

- **Decorrelated, stake-free judge** — when quality is LLM-judged, the judge is a
  **different model family** from the operational model AND sees only the rubric +
  transcript (never the fix/diff). Judge output is **advisory forever**; only
  deterministic cases are *trusted* by the ratchet. `judge_family` is recorded so
  judge drift is observable.
- **Shadow / differential re-generation** — for *successful* outputs, blindly
  re-generate twice with fresh decorrelated agents and semantically diff against
  production, splitting "generator wrong" from "grounding wrong". Dry-run
  validated, never executed.

### Improve (outer loop)

- **Deterministic clustering → ranked queue** — group findings on
  (surface × failure_mode), rank by severity × mass, **pin ratchet regressions
  first**. Deterministic so priority is un-game-able. Cluster mass is a
  corroboration threshold: a lone low-confidence finding can't solo-trigger a fix.
- **Fix forward** across any product surface; **no rollback** (the next
  iteration's signals catch slips).
- **Amnesia context** — inject the last N iteration records so the fixer doesn't
  re-try a fix it already regressed. The gate-only holdout is withheld from this
  same context.
- **Observability ratchet** — when an error can't be root-caused from its log
  (INSUFFICIENT), the fix is to *enrich the log site*, not guess the bug; a stable
  site tag links old→new signature and a follow-up tracks it until it recurs
  (root-cause then) or goes quiet. Each iteration reduces next-run ambiguity.

### Gates (deterministic; the loop cannot move these)

- **Build/test floor** — language-agnostic auto-detecting `does it build + pass`.
- **Deterministic ratchet** — the strict benchmark pass-set is a monotonic
  high-water mark **derived from the journal of deployed iterations**, not a
  separately-writable file. A deploy is blocked unless the current pass-set is a
  superset. Plus a **definition-hash** guard: a case that keeps its id but weakens
  its assertion does not count as passing. **→ this is CW's [ratchet](../../docs/ratchet.md), used as a hard pre-deploy gate.**
- **Tamper-evident hash-chained journal** — iteration outcomes form an append-only
  hash chain; the high-water mark is derived from the verified chain, so lowering
  the bar by editing state is detectable and fails closed. **→ identical to CW's ratchet journal.**
- **Gate-only holdout** — a benchmark split withheld from the fixer's context and
  run only at the gate, so the fixer can't overfit-and-ship.
- **Protected pathset** — a git-diff fence over the files that *define correct*,
  *enforce safe*, or *are the gate*. **Fail-closed**: unresolvable base → unsafe;
  protected touched → park; clean → proceed. **→ this is CW's [protected pathset](../../docs/ratchet.md).**

### Human touchpoint

- **Baseline behavior (trusted signals): park-and-notify, non-blocking.** A
  protected-path touch or an unverifiable domain-truth question posts one deduped
  message to a human channel and **parks** the item; the loop never waits — a
  future iteration consumes the human's out-of-band reply. This is safe only when
  the signal sources are trusted.
- **Untrusted signals: quarantine + blocking admin approval.** See
  [Trust model](#trust-model).

### Governance

- **Authority split (code as black box)** — business rules = human authority;
  code = agent authority. The loop may re-architect the product but never move its
  own goalposts (benchmark, guardrails, gate scripts, its own spec/state) — those
  are the protected pathset.
- **Self-reducing-friction lane** — the loop may improve *its own procedure*
  (helpers, skill body) but not its own *guards* (ratchet/guard/state/judge), which
  stay protected. Guard-adjacent self-edits auto-park.

## Trust model

The core design decision. A baseline loop assumes **trusted signal sources** (the
people whose feedback and conversations drive it are authenticated insiders), so
its only human gate is non-blocking park-and-notify. Apps with **untrusted end
users** turn feedback/conversation signals into a **prompt-injection surface** — a
malicious user can craft feedback engineered to get the loop to diagnose a "fix"
that weakens a guardrail or plants a backdoor. So the pattern adds a trust axis.
Full rationale in [`docs/patterns-registry.md`](../../docs/patterns-registry.md#trust-model-the-core-generalization).

1. **Signal sources are tagged `trusted` or `untrusted`** at apply time. Every
   `Finding` inherits `signal_trust`; every proposed change inherits the **lowest**
   trust of the findings in its cluster. (An app's own runtime error logs stay
   `trusted` — the app describes its own failure, not user words.)

2. **The human gate is trust-conditional:**

   | Change provenance | Gate |
   |--|--|
   | Only `trusted` signals, no protected path | Autonomous fix-forward → auto-deploy |
   | Touches a protected path (any provenance) | Park-and-notify (goalpost review) |
   | **Any `untrusted` signal** | **Quarantine → blocking admin approval** |

3. **Quarantine (`docs/patterns/pending-approval/<id>/` in the target app):** the
   proposed diff + full provenance chain (findings, signals, verbatim source text)
   + trust classification. The change is **inert** — not parked-but-eventual;
   it cannot deploy.

4. **Release requires admin-authenticated approval** —
   `patterns approve <id> --admin <identity>`, where admin identity is verified
   against a real authority (CODEOWNERS / signed approval commit / operator
   allowlist), never self-asserted. On approval the change **and its approver** are
   appended to the tamper-evident journal, so you can always prove an
   untrusted-derived change reached prod only through a named admin.

An internal-only app binds every source `trusted` and gets the frictionless
autonomous loop. A public app binds its user-feedback source `untrusted` and
automatically gets the quarantine gate. **Same pattern, no fork.**

## Parameters

Bound per app at apply time (schema in [`manifest.json`](./manifest.json)):

| Parameter | What it is |
|--|--|
| `signal_sources[]` | each `{kind, locator, trust}` — conversations / feedback / errors / backlog, with its trust level |
| `benchmark.strict_cmd` | deterministic per-case pass/fail extractor |
| `benchmark.golden_dir` / `holdout_dir` | fix-visible vs gate-only eval |
| `floor_cmd` | build/test floor |
| `product_model_family` | the operational model (judge must differ) |
| `judge_model_family` | decorrelated judge family |
| `protected_paths[]` | goalposts the loop can't move |
| `human_channel` | park-and-notify + approval destination |
| `admin_authority` | how admin identity is verified for approvals |
| `deploy_cmd` | fix-forward deploy |
| `schedule` | iteration cadence |

## Relationship to existing CW machinery

This pattern is **mostly a repackaging of machinery CW already owns**, pointed at
the *product* instead of at CW's build loop:

- Ratchet, high-water mark, definition-hash, tamper-evident journal → CW's
  [`docs/ratchet.md`](../../docs/ratchet.md) / `scripts/ratchet.py`, essentially verbatim.
- Protected pathset → CW's protected pathset, same concept.
- Human-parks-goalpost-changes → CW's "workers can't move their own goalposts".
- Traceability of findings → CW's `@cw-trace` / [`docs/traceability.md`](../../docs/traceability.md) spirit.
- `TBD:`/`UNRESOLVED:` gating of unbound parameters → `scripts/check_unresolved.py`.

The genuinely **new** parts are: (a) the **operate/refine loop shape** itself
(CW has no post-ship loop), (b) the **signal abstraction** (uniform Finding over
heterogeneous sources), and (c) the **trust model** (quarantine + blocking admin
approval for untrusted-derived changes).

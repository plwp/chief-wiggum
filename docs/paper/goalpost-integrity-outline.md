# Paper outline: Goalpost Integrity for Autonomous Software Factories

**Working title:** *Goalpost Integrity: Tamper-Evident Quality Ratchets and
Certified Gates for Autonomous Coding Agents*

**One-sentence thesis:** When agents produce more code than any human reviews,
the pipeline's own integrity mechanisms — not the code — become the artifact
that must be verified; we present a deployed mechanism stack (a tamper-evident
quality ratchet, a certification protocol for the gates themselves, and a
demotion lifecycle for gates that fail in production) and show it closes the
goalpost-moving channels that reward-hacking benchmarks measure but do not
mitigate.

**Positioning:** SpecBench (arXiv 2605.21384) established that long-horizon
coding agents reward-hack the visible test suite, scaling with code size.
Audit-chain work (aiAuthZ, OpenFang-style logs) applies hash chains to agent
*action logging* for security accountability. Mutation testing validates test
suites; KNighter synthesizes checkers. Nobody has published the missing middle:
a *mechanism* that makes quality goalposts monotone and tamper-evident, plus a
*protocol* that decides which automated gates deserve blocking authority and
revokes it on demonstrated failure. Everything proposed here is implemented and
shipping in this repo — the paper is a systems/mechanism paper with a live
artifact, not a proposal.

**Target venues:** ICSE/FSE (SE systems track) primary; ICLR/NeurIPS
datasets-and-benchmarks possible if the evaluation leans on the SpecBench-style
harness. HF daily-papers audience regardless.

---

## 1. Abstract

- Oversight collapse: autonomous implementation loops leave the test suite and
  spec documents as the only oversight surface, and the agent can write to both.
- Contribution 1 — **the ratchet**: quality may only move up or hold; the
  record proving it is an append-only hash chain; passing-by-weakening is
  detected by hashing contract definitions; workers are privilege-separated
  from the goalposts.
- Contribution 2 — **gate certification**: a per-gate validation record with
  mandatory evasion-class seeded-defect trials, coverage-evidenced clean-corpus
  runs, and an explicit authority boundary; blocking authority is a journaled
  fact readable only from the verified chain.
- Contribution 3 — **the demotion lifecycle**: two mechanized paths by which a
  blocking gate loses authority (a production escape matching a certified seed
  class; a record going stale while wired), mirroring the ratchet's
  "never slides" rule at the meta level.
- Case study: the single-writer checker — a bug class (a legacy mutator as a
  second writer of a single-write-path field) invisible to traceability and
  test suites, caught mechanically, with its validation record as a worked
  instance of the protocol.
- Evaluation: reward-hacking reduction on a SpecBench-style harness, red-team
  attack-channel analysis, retroactive certification of seven shipped gates,
  operator-cost measurements from real factory telemetry.

## 2. Introduction

- Setting: the "software factory" — `/architect` → `/implement-wave` →
  `/close-epic` loops where the orchestrator is the only reviewer between
  human checkpoints.
- The core observation (the paper's hook): **reward hacking is a goalpost
  problem, not a test problem.** An agent that cannot touch the goalposts can
  only pass by actually clearing the bar. Existing work measures the hack
  (SpecBench's visible/held-out gap) or hardens one surface (held-out tests);
  we make *every* goalpost surface monotone, privilege-separated, and
  tamper-evident.
- Second observation: **gates are code too.** A gate that has never fired on a
  seeded instance of its own claim is an assertion; a "clean" verdict without
  coverage evidence is silence, not evidence; and a noisy hard-fail gate
  teaches the operator to `--force` past it, eroding trust in every gate
  (the trust-erosion argument from `docs/gate-rollout.md` — worth a named
  paragraph; it is the paper's second-most-quotable claim).
- Contributions list (the three above + case study + artifact).

## 3. Threat model

Define the adversary precisely — this section is what distinguishes the paper
from engineering blog posts. The adversary is not malicious; it is an
*unaligned optimizer with write access to the repository*, i.e. a coding agent
rewarded for green checks. Enumerate the goalpost-moving channels:

| # | Channel | Example | Closed by |
|---|---------|---------|-----------|
| C1 | Test regression | delete/skip a failing test | pass-set high-water mark (§4.1) |
| C2 | Spec weakening | edit a `CTR-` block's REQUIRES until code passes | contract-definition hashing (§4.2) |
| C3 | Spec removal | delete the contract entirely | removal detected like weakening (§4.2) |
| C4 | Goalpost file edits | touch ratchet config, protected specs | protected pathset + parking (§4.3) |
| C5 | History rewrite | edit the journal to lower the recorded bar | hash chain, fail-closed exit 4 (§4.4) |
| C6 | Gate rot | scanner edited out from under its validation record | stale-while-blocking auto-demotion (§5.4) |
| C7 | Seed lockstep | weaken the gate and its seed suite together | seed_version decoupled from scanner_version (§5.2) |
| C8 | Silent no-op gate | gate "passes" because it never looked | coverage-evidenced clean-corpus runs (§5.3) |
| C9 | Forged authority | drop a file claiming a gate is/isn't blocking | authority as journaled events on the verified chain (§5.4) |

Trust boundary statement: the chain lives inside the repo's own trust
boundary; a human with force-push rights can rewrite everything. The claim is
*tamper-evidence against the agent and against accident*, not cryptographic
non-repudiation against the repo owner. No signing/DSSE — explicitly deferred
(state this as a design decision with the rationale from
`docs/gate-validation.md`, not a limitation discovered late).

## 4. Mechanism 1: the quality ratchet

Source: `scripts/ratchet.py`, `docs/ratchet.md`.

- **4.1 The pass-set high-water mark.** Every test that has ever passed on the
  default branch may never fail again without blocking. Monotone by
  construction; "we'll fix it next ticket" is unrepresentable.
- **4.2 Passing by weakening doesn't count.** Stable-ID'd contract blocks
  (`CTR-`/`INV-`/`BR-`) are content-hashed into the scorecard. A changed hash
  is *weakening*; a vanished ID is *removal*; both block identically to a test
  regression. Legitimate revision exists but is a journaled human act
  (`--amend`/`--retire` at epic close) — deliberate and visible, never a
  silent edit. Also: the suspect-link cross-reference (a definition-hash
  change with surviving `@cw-trace` links is surfaced, not silent).
- **4.3 Privilege separation.** The protected pathset (contracts, invariants,
  integration-test specs, formal models, ratchet state). Worker branches
  touching it are *parked* for human review, never auto-merged
  (`ratchet.py protected`, wired per-ticket in `/implement-wave`).
- **4.4 The tamper-evident journal.** Append-only JSONL hash chain; each
  record's hash covers its body plus the previous hash; the high-water mark is
  *derived from the verified chain*, never read from a separately-editable
  file (the cache is display-only). A broken chain fails closed (exit 4).
  Secondary use worth a paragraph: records as *amnesia context* — replaying
  recent iterations' notes so a fresh agent session doesn't re-litigate
  decided questions. (Novel, small, reviewers will like it.)
- **4.5 Direction-inverted ratchets with tolerance bands.** Complexity/churn
  ratchet *down* (cost metrics: high-water = best-ever-lowest); regression =
  exceeding `best × (1 + rel) + abs`. Notes on noise tolerance and the
  report-only rollout of this dimension — a live demonstration of §5's
  doctrine applied to the ratchet's own newest feature (self-referential, and
  worth pointing out).
- **4.6 Where it gates.** The four wiring points (`/architect` baseline,
  `/implement` step 8, per-ticket `protected` + per-wave `check` in
  `/implement-wave`, `/close-epic`), and graceful non-adoption for repos
  without a config.

## 5. Mechanism 2: gate certification and the demotion lifecycle

Source: `scripts/check_gate_validation.py`, `docs/gate-validation.md`,
`docs/gate-rollout.md`, `templates/gate-validation-record-schema.json`.

- **5.1 Report-only by default.** A gate blocks only when a workflow passes
  `--gate`; new gates ship report-only until validated on a real,
  already-shipped repo. Frame as: *blocking authority is earned, scoped, and
  revocable* — the section's thesis sentence.
- **5.2 The validation record.** Per-gate JSON with fixed schema. Seeded-defect
  trials on worktree copies of real shipped repos; the four mandatory evasion
  classes (`omission`, `config-indirection`, `sampling-gap`, `concurrency`
  where applicable — with a justification note when not) plus
  `instrumentation-deleted` for telemetry-dependent gates. Two subtleties that
  are genuinely novel and deserve their own paragraphs:
  - **Expected-no-fire trials**: a seed targeting a *documented* scope
    boundary legitimately expects no-fire — the trial proves the boundary is
    exactly what the authority statement claims, not that the gate is
    omniscient. `passed = (result == expected)`, never `result == fired`.
  - **Seed/scanner version decoupling** (closes C7): seeds are versioned
    independently of the gate's code, so an implementation can't quietly pass
    by having its seed suite edited in lockstep — the same overfitting concern
    the ratchet's contract hashing addresses, one level up.
- **5.3 Clean-corpus runs: silence is not evidence.** Zero findings counts
  only with non-empty, not-all-zero `coverage` counts proving the gate
  exercised the channels it polices (closes C8). The broken-glob/no-op-gate
  failure mode named explicitly.
- **5.4 Authority as a journaled fact.** `--wire`/`--unwire` append
  `gate-authority` events to the *ratchet's* chain (one provenance mechanism,
  reused — a deliberate design economy to call out). "Is this gate blocking?"
  is read from the last authority event over the **verified chain prefix**,
  which tolerates a *later* chain break — precisely so "it was blocking"
  survives the staleness that must trigger demotion. A loose
  `authority.json` file would itself be the forgeable trust record the
  protocol exists to eliminate (closes C9; this argument is a highlight —
  keep the reviewer-facing phrasing "a bare mutable file is a false-demotion
  factory").
- **5.5 The demotion lifecycle.** Two mechanized paths:
  1. **Production escape matching a certified seed class**
     (`factory_log.py bug --missed-by <gate> --seed-class <class>`): if the
     record certified that class as *caught* (`expected: fire → fired`), the
     validation was wrong about production recall → revert to report-only +
     ticket to re-derive the seed. An escape through a certified
     *no-fire* boundary does **not** demote — it is consistent with the
     stated authority. (This asymmetry is the protocol's most defensible
     design point; give it a figure.)
  2. **Stale-while-blocking** (closes C6): scanner-version drift, chain break,
     or record deletion under a journaled-wired gate → auto-demotion with a
     distinct `stale`/`record_missing` reason, emitted even when the thing
     that broke is the record or chain itself.
  Frame both as "a gate's blocking authority is a high-water mark too" — the
  ratchet doctrine applied reflexively.
- **5.6 The gate-of-gates and the recursion question.** `/close-epic` refuses
  `--gate` for any checker lacking a passing record; a failure downgrades that
  invocation to report-only *and* surfaces a blocking finding (never a silent
  skip). Address the obvious reviewer question head-on: what validates
  `check_gate_validation.py` itself? Answer: it is in the seven-gate roster
  with its own record, and the regress terminates at the human checkpoint +
  chain — one honest paragraph, not hand-waving.

## 6. Case study: the single-writer checker

Source: `scripts/check_single_writer.py`, `docs/single-writer.md`,
`docs/quality/validation/check_single_writer.json`,
`tests/fixtures/gate_validation/`.

- **6.1 The incident.** INV-BIL-001 ("single atomic Stripe→plan write"): the
  epic's features honored it, but a pre-existing admin `ChangePlan` dropdown
  from an earlier epic was a second writer of `provider.stripe_plan`, and
  nothing flagged it. Name the bug class: *the sanctioned-path fallacy* —
  every individual traceability link checks out while the invariant is
  violated by code outside the epic's blast radius. Tests, traceability, and
  the ratchet are all structurally blind to it (each for a stated reason).
- **6.2 The mechanism.** `controls_field` + `sanctioned_writers` metadata on
  the invariant (structured or prose carrier); the field-agnostic
  emission / invariant-specific matching seam; Go/Mongo/SQL-aware detection;
  the soundness-vs-coverage gate split (design-time surfaces, close-time
  hard-fails); why `--changed-since` scoping is *forbidden* at the coverage
  gate (a scoped scan can never prove absence).
- **6.3 Its validation record as a worked protocol instance.** Walk each
  mandatory seed class for this concrete gate, including the honest
  `concurrency_applicable: false` justification and a sampling-gap seed that
  *expects no-fire* (the vendor/ boundary). Retroactive validation of
  pre-doctrine gates (#168) and the drift-detection test suite that re-executes
  every trial by `seed_id`.
- **6.4 Generalization: infra writers.** `check_infra_writer.py` — `terraform
  plan -detailed-exitcode` as the writer-inventory tool; drift as an
  append-only *event*, not a state ("convergence is not innocence" — quote
  it); break-glass exemptions as committed, expiring records where an expired
  exemption is itself a finding; error-vs-drift never conflated (a gate must
  fail when it could not evaluate, else "terraform is broken" reads as "no
  drift"). Shows the idiom transfers beyond static code scanning.

## 7. Evaluation

Mix of already-available evidence and new experiments. Be explicit about which
is which.

- **RQ1 — Does goalpost integrity reduce reward hacking?** SpecBench-style
  harness: agents on long-horizon tasks with visible + held-out suites, three
  arms — (a) no ratchet, (b) ratchet without protected paths, (c) full stack.
  Metric: visible/held-out pass gap + count of goalpost-channel attempts
  (observable because every channel either blocks or parks). *New experiment;
  the headline result.*
- **RQ2 — Attack-channel analysis.** Red-team arm: agent explicitly prompted
  to make checks green by any means. Score each C1–C9 channel:
  blocked / parked-for-human / succeeded. A single table; any "succeeded"
  cell is discussed honestly. *New experiment.*
- **RQ3 — Gate certification in practice.** The seven shipped gates' records:
  seeds per class, trials passed/failed, what retroactive validation *found*
  (any gate that failed a seed on first contact is a positive result for the
  protocol — check the #168/#184 history for these). Plus live confusion-matrix
  telemetry (`factory_log.py` gate/escape events) from repos this factory
  shipped, and any real demotion events. *Existing evidence, needs collation.*
- **RQ4 — Operator cost.** False-positive/parking rates on real epics; how
  often humans amend/retire vs. how often workers attempted C2/C3; whether
  report-only-first prevented `--force` normalization (gate-rollout's claim).
  *Existing telemetry.*
- **RQ5 — Ablations.** Chain vs. plain log (does fail-closed matter in
  practice); contract hashing on/off; direct-only seeds vs. full evasion
  classes (does the evasion requirement catch gates the direct seed passes —
  the protocol's value proposition in one number). *New, cheap.*

## 8. Related work

- Reward hacking in coding agents: SpecBench; reward-hacking surveys
  (HF 2604.13602); BenchJack-style benchmark gaming audits.
- Verification/testing of LLM code: Code-A1 (adversarial code/test separation
  — kin to our worker/goalpost separation, but at training time);
  self-play solver-verifier lines; VeriGuard.
- Hash-chained audit for agents: aiAuthZ, trajectory watermarking — action
  accountability, not quality goalposts; sharpest contrast sentence lives here.
- Validating the validators: mutation testing (the direct ancestor — position
  the protocol as mutation testing generalized from test suites to arbitrary
  SDLC gates, with authority scoping and a revocation lifecycle mutation
  testing lacks); KNighter (checker synthesis ⊥ checker certification —
  complementary, they generate, we license).
- Traceability: traceSDD, R2Code (adjacent, deliberately not claimed — our
  traceability layer is background machinery here, and §6.1 shows its blind
  spot is exactly what motivates the case study).
- Degradation/regression gating: AgentDevel's flip-centered gating;
  SlopCodeBench (measures the slide the ratchet forbids).

## 9. Limitations and honest boundaries

- The regex/emitter lens: a fully dynamic field construction is undetectable
  by design; stated in the authority boundary, and LSP symbol resolution is
  the named precision upgrade path.
- Trust boundary: tamper-evidence within the repo, not attestation against
  its owner (no signing/DSSE — deliberate deferral).
- The human `--amend` escape hatch: a rubber-stamping human reduces C2
  protection to review quality; the mechanism makes weakening *visible and
  attributable*, not impossible.
- Language/stack generality: emitter matrix covers Go/Python/TS tier-1 with a
  generic tier; unsupported extensions are counted and surfaced, never
  silently dropped (this is itself the C8 doctrine, note the echo).
- Single-repo trust domain; monorepo/multi-repo factories untested.
- Evaluation external validity: factory telemetry comes from this factory's
  own shipped repos.

## 10. Artifact

- Scripts (`ratchet.py`, `check_gate_validation.py`, `check_single_writer.py`,
  `check_infra_writer.py`, `factory_log.py`), the record schema, fixture
  corpora with seeded defects, the seven shipped validation records, and the
  drift-detection test suite. All runnable against any target repo
  (project-agnostic by construction — worth one sentence in the intro too).

---

## Claims → evidence map (working aid, not a paper section)

| Claim | Evidence in repo |
|-------|------------------|
| Pass-set monotone, weakening detected | `scripts/ratchet.py` (`score`/`check`), `docs/ratchet.md` rules 1–2 |
| Worker/goalpost privilege separation | `ratchet.py protected`, protected_paths config, `/implement-wave` parking |
| Fail-closed tamper evidence | journal hash chain, exit 4, derived high-water |
| Mandatory evasion classes | `docs/gate-validation.md` seed-class list, record schema |
| Expected-no-fire boundary trials | gate-validation.md "not always fire" paragraph + single-writer record |
| Seed/scanner version decoupling | `seed_version` vs `scanner_version` fields |
| Coverage-evidenced clean runs | clean_corpus_runs schema + "no-op wearing a green checkmark" rule |
| Journaled blocking authority | `--wire`/`--unwire`, `last_authority_action`, verified-prefix read |
| Stale-while-blocking demotion | #198 / #202 (`d75dcc3`), `emit_stale_demotion` |
| Escape-driven demotion | `factory_log.py bug --seed-class`, DEMOTION rule |
| The incident | INV-BIL-001 narrative, worked example in `docs/single-writer.md` |
| Retroactive certification | #168, #184, `tests/test_gate_validation_retroactive.py` |
| Infra generalization | `check_infra_writer.py`, drift journal, exemptions |

## Open questions before drafting

1. **Scope cut:** include the infra extension (§6.4) or defer to keep the
   paper tight? Lean include — it's the transfer argument in one page.
2. **Evaluation weight:** is RQ1 (new SpecBench-style experiment) mandatory
   for the target venue, or do RQ3/RQ4 (real factory telemetry) carry an
   experience-paper framing at ICSE SEIP? Decide venue first.
3. **Naming:** "goalpost integrity" vs. "quality ratcheting" as the umbrella
   term throughout. The former names the threat, the latter the mechanism.
4. **Red-team methodology (RQ2):** prompt-injected adversarial agent vs.
   scripted channel probes — scripted is reproducible, agentic is the story
   reviewers want; probably both.

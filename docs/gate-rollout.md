# Gate rollout: report-only before blocking

A **gate** is a script that can hard-fail a workflow (`/architect`, `/implement`,
`/implement-wave`, `/close-epic`) — traceability, single-writer, unresolved-markers,
ratchet, the SaaS NFR gate. A gate that is *noisy on real code is worse than no gate*:
it hard-fails a workflow on false positives, and the operator learns to `--force` past
it — which erodes trust in **every** gate.

This is not hypothetical. `check_single_writer.py` (the single-writer gate) shipped wired
as a blocker and false-positived heavily on the first real polyglot repo (comments, Go
`:=`, in-memory assignments, TS interface fields — see the precision fix in the git
history for `#93`). The lesson generalizes.

The mirror-image failure — a gate that is *silent when it is most broken* — is
governed by [fail-closed.md](fail-closed.md): the four-state
`pass | findings | inapplicable | error` outcome, the measured denominator every
gate prints, and the per-gate audit of which checkers still render "failed to
run" as "passed" (chief-wiggum#289).

## The rule

**Every new hard-fail gate ships report-only first, and is validated on a real,
already-shipped repo before it is wired as a blocker.**

Concretely:

1. **Report-only is the default; blocking is an explicit opt-in.** The gate scripts
   already enforce this shape: they print their findings and **exit 0** unless you pass
   `--gate` (`check_single_writer.py`, `check_traceability.py`, `ratchet.py`) or the
   equivalent blocking flag (`saas_gate.py --gate`). Running a gate **without** its
   `--gate` flag is report-only mode — use it during bring-up.

2. **Prove precision on real code before promoting to blocking.** Run the new gate
   report-only against at least one real, already-shipped target repo and inspect the
   finding set. An acceptable false-positive rate is the bar for promotion — attach the
   dry-run findings to the change that wires the gate into a workflow with `--gate`.

3. **Log what a gate does NOT cover.** If a gate has known blind spots or residual false
   positives (e.g. the single-writer gate cannot, by regex alone, tell an audit-log
   `bson.M{"field": …}` from a provider `$set`), document them next to the gate and in
   its `--help`, and prefer precise metadata (`sanctioned_writers`, `--exclude`) over
   silently accepting noise.

## Why report-only is already the mechanism

None of the gate scripts fail on their own — a workflow *chooses* to make a gate
blocking by passing `--gate`. So "report-only" needs no new flag: it is what a gate does
when a workflow (or a human, during validation) runs it without `--gate`. When adding a
gate to a workflow, land the report-only invocation first (surface findings in the run
output), and only switch it to `--gate` once step 2 is satisfied.

## Checklist for adding a gate

- [ ] Gate exits 0 in its default (no-`--gate`) mode and prints findings.
- [ ] Dry-run against a real shipped repo; false-positive rate is acceptable.
- [ ] Known limitations documented in the script docstring/`--help` and its `docs/` page.
- [ ] Wired into the workflow report-only first; promoted to `--gate` in a follow-up that
      cites the dry-run.

## Gate ledger

| Gate | Script | Blocking flag | Status |
| --- | --- | --- | --- |
| Traceability | `check_traceability.py` | `--gate` | blocking (`/architect`, `/close-epic`); **gate-validation record: passed** (#168, retroactive); widened to a three-state `applicable\|inapplicable\|error` outcome by chief-wiggum#281/#289 — two new blocking soundness finding classes, `malformed_ids` (declaration-position near-miss stable IDs, e.g. the two-segment `INV-001` shape) and `unparsed_artifacts` (an ID-bearing artifact present with content but zero parseable IDs); `error` fails BOTH `--gate soundness` and `--gate coverage` and is never `--write-links`-eligible. Precision proven report-only across this repo's own real `docs/epics/*/`, `templates/formal-models/examples/`, and `patterns/*/` corpus (zero false positives) before folding into `soundness_ok` — see the dry-run evidence cited in the #281 PR |
| Single-writer | `check_single_writer.py` | `--gate` | blocking (precision fix in #93); **gate-validation record: passed** (#168, retroactive) |
| Unresolved markers | `check_unresolved.py` | (blocks by default) | blocking (`/implement-wave`); **no `--gate` flag exists** — the ledger claimed one until chief-wiggum#289 corrected it. A workflow runs this to decide whether dependent work may proceed, so a marker blocks wherever it is run. Widened to the four-state outcome by #289: an artifact present that the scanner could NOT read is `error` (exit 1), never the "OK: no unresolved markers found" it printed before |
| Ratchet: pass-set + contract hashes | `ratchet.py check` | (blocks by default) | blocking (`/implement`, waves, `/close-epic`); **gate-validation record: passed** (#184, retroactive); a pass-set case may be quarantined out of the high-water via the journaled `record --retire-case` waiver (#278) — the quarantine LISTING is report-only, and an expired quarantine blocks through the existing `missing_tests` class, so no new blocking finding class was introduced and the record's eight trials still cover it |
| **Ratchet: complexity + relative churn** | `ratchet.py check --gate-quality` | `--gate-quality` | **NEW — report-only** (#110); validate on a shipped repo before wiring as a blocker |
| SaaS NFR | `saas_gate.py --gate` | `--gate` | blocking (`/saas-gate`); **gate-validation record: passed** (#184, fixture-served local-server target per CTR-fh-044) |
| **Minimal-CI** | `ci_scaffold.py` | `--gate` | **report-only** (#111); wired into `/close-epic` report-only; **gate-validation record: passed** (#184) — `--gate` may now be wired per INV-fh-003 |
| **AI-slop signals (code survival + duplication)** | `quality_slop_gate.py` | `--gate` | **report-only** (#113); `/close-epic`; **gate-validation record: passed** (#184, fixture band-file target per CTR-fh-044 — validates the banding/verdict logic, not the external engines) |
| Architecture model consistency | `check_architecture.py` | `--gate` | report-only in `/architect` (#174); **gate-validation record: passed** (#184, one seed per frozen `CHECKS` entry per ADR-fh-06) |
| **Traceability: suspect-link propagation** | `check_traceability.py` (`suspect_links`) | none yet | **NEW — report-only** (#169); does not affect `coverage_ok`/the `--gate coverage` exit code; `ratchet.py check`/`regressed` surface the same sidecar cross-reference visibly. Promote once a dry-run across a real epic's link churn shows an acceptable false-positive rate (see docs/traceability.md) |
| **Bet-ledger gates (states-and-dates soundness, tranche cap, dated-criterion evaluation, bets-in-flight cap, bet-selection lint, goalpost integrity)** | `bet.py` | `--gate` | **NEW — report-only** (#235); no workflow passes `--gate` until a `validation/bet-gates.json` record exists (dogfood one real bet end-to-end first — docs/business-factory.md §8). Journal tamper (exit 4) and `killed`-without-retrospective are hard state-machine guards, not gated findings |
| **Channel-engine gates (experiment completeness incl. referral baseline-flow, exactly-one-focused-channel, channel-CAC ≤ target join, ≤3 concurrent testing, zero-headcount sales-led filter)** | `channel.py` | `--gate` | **NEW — report-only** (#241); same dogfood bar as the bet-ledger gates — one real Bullseye pass before any workflow passes `--gate`. CAC join skips (never fires) when the bet declares no `target_cac_usd` |
| **Rep cadence + *Traction* 50% rule** | `bet.py evaluate` / `channel.py status` | `--gate` | **NEW — report-only** (#241); cadence counts trailing-week rep entries while probing\|validating (default 3, per-bet `--cadence`, journaled); 50% rule reads `--tag product\|traction` hours and is silent when no tagged hours exist — a finding must come from data, never its absence |
| **Validation-experiment gates (XYZ falsifiability lint, vanity-metric lint, pre-registration hash enforcement incl. never-journaled cards + comparator-contradicting verdicts, ASM↔card traceability, interview-strength cap)** | `assumption.py` | `--gate` | **NEW — report-only** (#236); same dogfood bar as the bet-ledger gates — one real ASM through card → run → verdict before any workflow passes `--gate`. A verdict with NO card and a decided card's re-verdict are hard usage errors (exit 2), not gated findings; journal tamper is exit 4 |
| **Evidence-strength floor on `building`** | `bet.py transition <id> building` (computed by `assumption.py`) | `--gate` | **NEW — report-only** (#236); ≥1 validated ASM at strength ≥4 (reputation/money); interview-only evidence capped at 1 regardless of count; no assumptions.json → `skipped`, never a silent block |
| **Kill-review verdict findings (malformed provider output, hold-without-disconfirming-test, missing optional voice)** | `bet.py kill-review` | `--gate` | **NEW — report-only** (#237); same dogfood bar as the bet-ledger gates. The brief-purity lint is NOT here: it is a generator self-check that refuses an impure brief unconditionally (exit 1, like the retrospective hard guard), never an operator gate. The fairness downgrade (demand-criterion kill → recycle while distribution unattempted) is a mechanical verdict transform, not a finding |
| **Business-model authoring gates (premortem coverage, pain↔reliever bipartite completeness, e3-value per-actor viability >2 actors, canvas/VPC field status↔evidence consistency, ASM↔canvas join, goalpost integrity)** | `plan_bet.py` | `--gate` | **NEW — report-only** (#238); same dogfood bar as the other bet-stage gates — grounded against a real portfolio bet re-authored through the stage (`tests/test_plan_bet_accrualflow.py`) before any workflow passes `--gate`. Schema validation failure is `error` (never gated as an ordinary finding, blocks under `--gate` the same way `check_traceability.py`'s broken-instrument case does) |
| **Fermi viability gate (arithmetic impossibility: MSC/price/churn/funnel vs. TAM)** | `plan_bet.py author\|rebaseline\|check` | none (unconditional) | **NEW — hard block, NOT report-only** (#238, Decision 3 — a deliberate, stated exception to this document's own rule): a DECLARED, arithmetically-impossible `fermi_inputs` block blocks `author`/`rebaseline`/`check` regardless of `--gate`, the same posture as `bet.py`'s two state-machine hard guards. No `fermi_inputs` declared at all is an ordinary `skipped:` finding, never a block — only a declared impossibility blocks. Precision risk is a non-issue here (either the bet's own numbers reach its own declared TAM or they don't), which is why this is exempted from the bring-up ramp rather than promoted after a dry-run |
| **EU AI Act (Art. 5 prohibited practices, Art. 6(4) derogation documentation, Art. 50 transparency — in-force layer only)** | `check_ai_act.py` | `--gate` | **NEW — report-only** (#316); wired report-only into `/architect` (Step 4j, classification soundness) and `/close-epic` (Step 2j2, Art. 50/5/6(4) coverage); no workflow passes `--gate` until a dry-run against a real shipped target and a `docs/quality/validation/check_ai_act.json` record exist. Absence of `docs/compliance/ai-act.json` is itself a `findings` outcome (never a silent pass) — Art. 6(4) makes the classification's non-existence distinguishable from a recorded "not high risk". The Chapter III high-risk conformity pack is deliberately out of scope (Digital Omnibus deferral; harmonised standards don't exist yet) |
| **External-interface provenance** | `check_interface_provenance.py` | `--gate` | **NEW — report-only** (#350); wired report-only into `/architect` (Step 6). An operation declared `"external": true` must cite `observed_fact` or `api_doc` provenance, or carry an unresolved marker. No workflow passes `--gate` until a `docs/quality/validation/check_interface_provenance.json` record exists. `external` is deliberately NOT schema-required — the omission evasion (guess a vendor route, never declare it) is not mechanically closable, so the gate reports a named **declaration gap** whenever operations exist and none are external — outcome `inapplicable`, never `pass`, since zero of zero verified is not evidence (the first cut of this gate returned `pass` there, and the 22-epic dry-run caught it). #350 also proposed a hedge-prose lint ("best-effort", "approximation", "assumed"); it was measured against 314 shipped epic artifacts in 9 repos and REJECTED — `best-effort` alone fired 84 times, every hit a genuine design property ("a best-effort emitter"). Presence of a cited source is decidable; intent in prose is not |
| **External-integration smoke** | `check_external_smoke.py` | `--gate` | **NEW — report-only** (#353); wired report-only into `/close-epic` (Step 2j3). Every system declared `"external": true` with an `external_system` name (#350) needs one real round-trip, marked `@cw-smoke <system> [case=...]` — the third tag in the `@cw-*` family after `@cw-writes`/`@cw-emits`. Deliberately does NOT reuse `ratchet.parse_junit_xml`, which returns passing case ids only and collapses `skipped` in with `failure`/`error`: that collapse is the very conflation this gate exists to stop, so it parses the three states itself. A SKIPPED smoke is `unverified` and blocking-eligible, never a pass. An unpinned annotation cannot reach `verified` (file-fallback matching would let an unrelated passing test in the same file award the pass) but can still reach `unverified`/`failed` — weak evidence may raise an alarm, never grant one. No workflow passes `--gate` until a `docs/quality/validation/check_external_smoke.json` record exists |

## From convention to protocol: `docs/gate-validation.md` (#168)

The rule above ("validated on a real, already-shipped repo") was, until #168,
a human judgment call checked by convention — a dry-run's findings attached to
a PR, reviewed by eye. `docs/gate-validation.md` makes it a **mechanical
protocol**: a per-gate `validation/<gate>.json` record (seeded-defect trials —
including mandatory evasion classes — plus clean-corpus runs with coverage
evidence and an authority-boundary statement), enforced by
`scripts/check_gate_validation.py`. `/close-epic` refuses to pass `--gate` to
a checker lacking a passing record. Traceability and single-writer (both
predating the doctrine) carry retroactively-completed records as of #168; #184
extended the protocol to the five remaining blocking-capable gates — ratchet,
SaaS-NFR, minimal-CI, AI-slop-signals, and architecture-model consistency —
each with a hash-derived `--scanner-version`, live-verified seeded trials
(re-executed by `tests/test_gate_validation_retroactive.py` and the per-gate
suites), and a journaled record under `docs/quality/validation/`. The
non-deterministic targets (SaaS-NFR's live URL, AI-slop's tool bands) are
pinned to fixture harnesses — a scripted local HTTP server and recorded band
files — per CTR-fh-044, so their clean-corpus runs are reproducible in CI.

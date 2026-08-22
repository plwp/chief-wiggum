# Pre-registration: dynamic-DAG open/micro-model ablation

Tracking issue: chief-wiggum#391. Parent epic: chief-wiggum#383.

**Status: registered, no arm has run.**

This document is binding. Arms, metrics, the gap-closure formula, the
non-inferiority margin and the stopping rules are fixed here, before the first
arm runs. A margin chosen after seeing results is not a margin, and a
degenerate case discovered during analysis is an invitation to pick the
flattering reading. Both are settled below instead.

Changing anything in this document after an arm has run invalidates that arm.
The change must be recorded as an amendment with its date and reason, and the
affected arms re-run on a fresh split.

## The question

How much of the open-model versus frontier-model gap does a better process
close?

Every figure CW publishes today confounds "which model" with "which workflow".
E2EDev reports 67.1% test accuracy across 46 tasks; SWE-bench Verified reports
15/20 on a seeded random subset, with the README already carrying the caveat
that N=20 means roughly plus or minus nineteen points. Neither isolates process
from model, so "the factory closes the capability gap" is currently
unfalsifiable. This experiment factorises it.

This is the ticket the other seven exist to answer. It is also the one allowed
to conclude they were not worth building.

## Arms

Arms are specified by capability tier, never by model name, so the experiment
survives the roster changing under it (`models.md` is refreshed often). Tiers
resolve through `config/providers.json` at run time and the resolved roster is
recorded in each run manifest.

| Arm | Model tier | Process | Tests |
|-----|-----------|---------|-------|
| 1 | frontier-tier | static factory (`plan_waves.py`, static roles) | upper end of the gap |
| 2 | open-tier | static factory | lower end of the gap |
| 3 | open-tier | dynamic DAG (#387) | decomposition |
| 4 | open-tier | dynamic DAG + selective candidates (#389) | candidates |
| 5 | adaptive hybrid routing (#388) | dynamic DAG | routing economics |

Arm 2 is the baseline against which negative findings are judged.

## Corpus and strata

Stratified across task class (feature, bugfix, refactor, test-only, frontend,
backend), risk class, and size. Historical CW tickets plus live tasks. Existing
harnesses (`swebench_harness.py`, `e2edev_harness.py`) are reused where the
corpus overlaps; no third harness is built.

Inclusion rules, fixed here:

- A historical task is excluded if its solution is reachable from the repo
  state given to the arm. The cutoff and the exclusion count are recorded.
- Public-benchmark instances that may be in model pretraining are stated
  plainly. Cross-arm comparison under fixed conditions is the readable signal;
  absolute score is not.
- Minimum 20 tasks per stratum for a stratum-level number to be reported at
  all. Below that the stratum is reported as underpowered, with its N.

## Fixed conditions

Everything except the varied factor is identical across arms: task corpus,
environment, tool availability, budget ceilings, verifier, and held-out
grading. The verifier is frozen and content-hashed before any arm runs, the
same discipline as #389's candidate verifier.

Deviations are recorded as protocol violations, never silently absorbed.
`experiment.protocol_violations` compares two run manifests and names what
moved.

## Metrics

Per arm, and sliced by task class and risk:

- accepted-patch rate (the headline quality number)
- contract, test, and gate conformance
- escaped defects, meaning those found after acceptance
- operator intervention minutes
- model cost and normalised compute cost
- wall-clock latency
- retries, graph mutations, escalation rate

Every headline number carries its N and a 95% Wilson interval. The Wilson
interval is used rather than the normal approximation because the strata are
small, and the normal approximation misbehaves exactly there: it produces
bounds outside [0, 1] and a zero-width interval at 0 or 100 percent.

Quality is never reported without cost. The cost/quality frontier across all
five arms is part of the result, not an appendix.

## Gap closure

```
gap_closure = (adaptive_open_quality - static_open_quality)
            / (frontier_quality  - static_open_quality)
```

Degenerate cases, decided now:

| Case | Handling |
|------|----------|
| denominator within 1e-9 of zero | **Undefined.** The corpus was not discriminative. No ratio is published. |
| denominator negative (open beats frontier under the static process) | **Meaningless.** Report the raw arm differences and investigate the corpus. |
| numerator negative (the dynamic process made the open model worse) | **Negative result.** Reported as such, never clipped to zero. |

Only a `DEFINED` status may be quoted as a gap-closure percentage.
`experiment.gap_closure` returns the status alongside the value, and
`GapClosure.publishable` is false for every other case.

## Non-inferiority margin

Claim under test: **arm 5 is not worse than arm 1 on accepted-patch rate.**

**Margin: 5 percentage points, absolute.**

Justification, recorded before any arm runs: CW's own acceptance bar is that a
change leaves CI green and passes the gates, so a difference smaller than the
run-to-run noise of the existing suite is not a quality difference anyone would
act on. Five points is also below the roughly nineteen-point interval the
README already reports at N=20, which means the margin is only meaningful at
the corpus sizes this pre-registration requires. Choosing a wider margin would
let arm 5 lose materially and still be called non-inferior; choosing a narrower
one would demand a corpus larger than the budget supports.

Assessment is conservative: the worst case for the treatment against the best
case for the control. Non-inferiority holds when the lower bound of arm 5's
interval minus the upper bound of arm 1's interval clears the negative margin.

## Stopping rules

- No arm stops early because it is winning. Interim looks do not change the
  registered N.
- An arm stops immediately on a protocol violation. The violation is recorded
  and the arm re-runs from a clean manifest.
- The experiment stops and reports if budget is exhausted before the registered
  N. A partial result is reported as partial, with its N, and does not produce
  a gap-closure ratio.
- Any tuning of the DAG, routing, or candidate policy against the evaluation
  corpus invalidates the arm and requires a re-run on a fresh split.

## Reporting rules

- Raw per-task results are published and immutable, journaled through the
  existing ratchet hash chain. No signing or DSSE, consistent with how
  gate-validation records are handled.
- Negative results are retained and reported. A report with no negative
  findings across five arms is treated as a reporting failure and reviewed
  before publication. `experiment.reporting_failures` flags this mechanically.
- No README or public claim is updated before this ticket's evidence exists.
  Any claim that does land carries its N, its interval, and its caveats in the
  same sentence. `experiment.readme_claim_allowed` encodes the bar: gap closure
  must be `DEFINED`, the smallest arm must reach the registered minimum N, and
  no interval may be wider than 20 points.

## Rollout ladder

Each rung has entry and exit criteria, and the previous rung must pass before
the next begins.

1. **Historical replay.** Entry: corpus frozen, verifier hashed. Exit: all five
   arms complete with no protocol violations.
2. **Live shadow.** The dynamic path computes decisions without dispatching
   (#387's shadow mode, #388's shadow routing). Exit: shadow decisions logged
   for a full epic with no crash and no divergence the operator judges unsafe.
3. **Advisory.** Decisions surfaced to the operator, who acts or declines.
   Exit: operator agrees with the recommendation on a majority of nodes.
4. **Bounded autonomy.** Dynamic path runs with budget ceilings and the
   approval queue for privileged operations. Exit: one epic completed with no
   escaped defect attributable to the scheduler.
5. **Default.** Static path retained as `--static` fallback.

## Rollback

Conditions for reverting to `plan_waves.py` plus static roles:

- quality regression beyond the non-inferiority margin at any rung
- an escaped defect attributable to the scheduler, the router, or promotion
- operator intervention minutes exceeding the static baseline
- any unrecoverable graph state in production use

The fallback is exercised at least once at the end of the experiment to prove
it still works, not assumed. The static projection shares the same graph
(#385's `project --waves`), so the fallback is real code exercised by tests.

## What this document does not do

It does not run the arms. Execution needs real budget, real wall-clock, and a
human decision at each rung of the ladder. The go / hold / rollback decision is
a human decision by design, and nothing here pre-empts it.

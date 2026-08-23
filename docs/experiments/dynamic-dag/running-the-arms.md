# Running the arms

Operator runbook for the dynamic-DAG ablation (chief-wiggum#391).

The pre-registration (`pre-registration.md`) is the binding document and it
does not change. This file is the machinery underneath it: what to run, in
what order, and which step refuses to continue.

Everything here is `scripts/dag_experiment.py`. Nothing here spends model
budget. The harness freezes, pins, records, and analyses. Executing the arms
themselves, and the go / hold / rollback decision at each rung of the ladder,
stays a human decision by design.

## Order of operations

```
freeze-corpus  ->  hash-verifier  ->  manifest (x5)  ->  [run the arm]
                                          |
                                   check-protocol
                                          |
                                       record (x5)  ->  report
```

`check-protocol` deliberately sits before `report`. The pre-registration stops
an arm on a protocol violation and re-runs it from a clean manifest, and that
decision has to be reachable without anyone having looked at a score yet.

## 1. Freeze the corpus

```bash
python3 scripts/dag_experiment.py freeze-corpus \
  --candidates corpus/candidates.json \
  --repo ~/.chief-wiggum/repos/owner/repo \
  --out corpus/corpus.json
```

A candidate record:

```json
{
  "task_id": "cw-352",
  "source": "cw-history",
  "task_class": "bugfix",
  "risk": "low",
  "size": "small",
  "base_commit": "b6a4e...",
  "base_date": "2026-05-01",
  "solution_commit": "179181c...",
  "solution_date": "2026-08-23",
  "public_benchmark": false
}
```

`task_class`, `risk` and `size` must come from the pre-registered
vocabularies (`corpus.TASK_CLASSES`, `RISK_CLASSES`, `SIZE_CLASSES`).
Anything else excludes the task rather than inventing a stratum. A typo would
otherwise produce a stratum of one, and a stratum of one gets reported as a
stratum.

### Contamination

A task is excluded when its solution is reachable from the repo state the arm
is given. `--repo` is what makes that question answerable: the harness asks git
whether `solution_commit` is an ancestor of `base_commit`.

**Without `--repo`, reachability is `UNKNOWN`, not clean.** The task then falls
back to its dates, and a task whose dates cannot settle it is excluded as
`UNVERIFIABLE_BASE`. That direction is deliberate. An unverifiable task
admitted is a silently contaminated corpus, and contamination does not show up
as an anomaly in the results. It just raises every arm that can read the
answer, which looks exactly like a real difference.

Exclusion reasons, all counted and published with their denominator:

| Reason | Meaning |
|---|---|
| `SOLUTION_IN_BASE` | git says the solution is an ancestor of the base |
| `SOLUTION_PREDATES_BASE` | git could not say; the dates put the solution first |
| `UNVERIFIABLE_BASE` | neither git nor the dates could settle it |
| `DUPLICATE_ID` | the same task id appeared twice |
| `INVALID_STRATUM` | a class, risk or size outside the registered vocabulary |

Public-benchmark instances are **annotated, not excluded**. They are listed
under `pretraining_risk` so the reader knows the absolute score may be
inflated; the readable signal is the cross-arm difference under fixed
conditions.

### Underpowered strata

The pre-registration sets a floor of 20 tasks per stratum for a stratum-level
number to be quoted at all. Strata below it are labelled `UNDERPOWERED` with
their N and still counted in the corpus, never dropped.

`--gate` turns the floor into a blocking check (exit 1). Without it the command
reports and exits 0, per `docs/gate-rollout.md`.

## 2. Hash the verifier

```bash
python3 scripts/dag_experiment.py hash-verifier \
  --path tests/verify_patch.py --path tests/conftest.py \
  --command "pytest tests/" \
  --out corpus/verifier.json
```

Frozen before any arm runs, identical across arms. A path that does not exist
raises: "the verifier file was missing so we hashed the rest" is how two arms
end up sharing a hash while running different verifiers.

## 3. Pin each arm's conditions

```bash
python3 scripts/dag_experiment.py manifest \
  --arm arm-3 \
  --corpus corpus/corpus.json --verifier corpus/verifier.json \
  --roster implementer=open-tier-a --roster reviewer=open-tier-b \
  --seed task_order=7 \
  --budget usd_cents=50000 --budget wall_seconds=7200 \
  --env python=3.11 --env os=darwin-25.1 \
  --out runs/manifest-arm-3.json
```

The registered arms are `arm-1` through `arm-5`; the tier and process for each
come from `report.ARM_SPECS`, copied from the pre-registration, so an arm
cannot be relabelled at run time.

Budgets and seeds are whole integers. Manifests are hashed, and the canonical
encoding rejects floats so hashes stay replay stable, so express money in cents
and time in whole seconds.

## 4. Run the arm

This is the part that costs money and wall-clock, and the part this repo does
not automate. Produce one outcome record per corpus task:

```json
{"task_id": "cw-352", "accepted": true, "gate_conformant": true,
 "escaped_defect": false, "operator_seconds": 240,
 "model_cost_cents": 812, "wall_clock_seconds": 1830,
 "retries": 1, "graph_mutations": 4, "escalations": 0}
```

`accepted` has no default. A record without it is refused rather than read as
a rejection.

## 5. Check protocol before looking at anything

```bash
python3 scripts/dag_experiment.py check-protocol --gate \
  --manifest runs/manifest-arm-1.json ... --manifest runs/manifest-arm-5.json
```

Compares every arm against `arm-1` and names what moved: verifier, corpus,
budgets, environment. Any finding means those arms re-run from a clean
manifest.

## 6. Record the raw results

```bash
python3 scripts/dag_experiment.py record \
  --arm arm-3 \
  --corpus corpus/corpus.json \
  --manifest runs/manifest-arm-3.json \
  --outcomes runs/outcomes-arm-3.json \
  --journal docs/quality/ratchet-journal.jsonl \
  --out runs/run-arm-3.json
```

This is what makes the raw results immutable in the sense the repo can
actually enforce. The per-task records live in `runs/run-arm-3.json`; their
digest, the manifest digest, the corpus version and the verifier hash go into
the ratchet hash chain as an `experiment-record` event. Edit any one of them
afterwards and the digest recorded at the time no longer matches.

The event is never `merged`, so it does not touch the ratchet's pass-set
high-water mark. Appending onto a broken chain fails closed, and the reader
(`chief_wiggum.experiment_journal.experiment_records`) reads only the verified
prefix. A record past a torn tail is not evidence of anything.

The append lives in `scripts/chief_wiggum/experiment_journal.py` rather than in
`ratchet.py`. The ratchet gate's `--scanner-version` is the hash of
`ratchet.py` and its finding-affecting dependencies (INV-fh-005), so editing
that file to add an unrelated event type would stale the ratchet gate's
validation record and demand a re-run of its seeded-defect trials, paid for a
change that touches none of the gate's finding logic.

Coverage is reported here, not inferred:

- **missing**: a corpus task with no outcome. Not scored as a rejection; an
  arm that died halfway must read as incomplete, not merely bad.
- **unknown**: an outcome for a task outside the corpus. Not counted as an
  attempt.
- **duplicates**: the same task twice. First record wins, second is counted.

## 7. Report, or refuse to

```bash
python3 scripts/dag_experiment.py report \
  --corpus corpus/corpus.json \
  --run runs/run-arm-1.json ... --run runs/run-arm-5.json \
  --out-json results/report.json --out-md results/report.md
```

The report refuses to produce a gap-closure ratio at all when the registered
stopping rules say it should not exist:

| Suppression | Cause |
|---|---|
| `PROTOCOL_VIOLATION` | arms ran under different conditions |
| `PARTIAL_RUN` | some arm did not attempt the whole corpus |
| `MISSING_ARM` | arm 1, arm 2 or arm 5 is absent |

Suppression is kept distinct from a degenerate ratio on purpose. "We never ran
enough tasks" must not read like "the corpus was not discriminative". Only
one of those is a finding about the corpus.

When nothing is suppressed, gap closure is reported for **arms 3, 4 and 5
against the same denominator**. Arm 5 is the headline because the registered
non-inferiority claim is about arm 5; showing all three is what stops the
flattering one being quietly promoted.

Also in the report: per-arm accepted-patch rate and gate conformance with N and
95% Wilson intervals, the cost/quality frontier, the negative findings, the
reporting failures (a clean sweep across five arms is one), and the public-claim
gate. `--gate` exits 1 when there are reporting failures.

## Rollback

The registered fallback is `plan_waves.py` plus static roles. It shares #385's
graph, so the thing to exercise is the static projection:

```bash
python3 scripts/dag_engine.py project --waves --db <journal.db>
```

`tests/test_dag_experiment_harness.py::test_static_fallback_still_projects_a_wave_plan`
exercises it on every run. If that projection stops working there is nothing
to roll back to, and the rollback criteria in the pre-registration become
unenforceable.

## What is still a human decision

- running arms 1 to 5 (real budget, real wall-clock)
- the go / hold / rollback call at each rung of the ladder
- publishing anything, which the public-claim gate can only refuse, never grant

Amending the arms, the metrics, the formula or the margin after an arm has run
invalidates that arm. Record the amendment with its date and reason and re-run
on a fresh split.

# Quality ratchet: fix-forward without sliding backward

Chief Wiggum's implementation loops are autonomous between human checkpoints —
workers write code, merge waves, and ship PRs with the orchestrator as the only
reviewer in the loop. The ratchet is the deterministic mechanism that makes
that survivable: **quality may only move up or hold, never down**, and the
record proving it is tamper-evident.

Three rules, all mechanical:

1. **The pass-set never shrinks.** Every test case that has ever passed on the
   default branch is the **high-water mark**. A merge that would make a
   high-water case fail is blocked — no "we'll fix it next ticket".
2. **Passing by weakening doesn't count.** Every stable-ID'd contract block
   (`CTR-`/`INV-`/`BR-`, see [traceability.md](traceability.md)) is hashed. A
   high-water contract whose definition changed was *weakened*; one that
   disappeared was *removed*. Both block, exactly like a test regression —
   unless a human deliberately journals an `--amend`/`--retire`.
3. **Workers don't move their own goalposts.** Contracts, invariants,
   integration-test specs, formal models, and the ratchet's own state form the
   **protected pathset**. A worker branch that touches them is *parked* for
   human review, never auto-merged.

Plus one **report-only** dimension:

4. **Complexity & churn only ratchet down.** Mean cyclomatic complexity (McCabe
   1976), the share of functions over CCN 10, and relative churn (churned-LOC /
   total-LOC — Nagappan & Ball 2005) are snapshotted alongside the scorecard.
   This dimension is [new, so it ships **report-only**](gate-rollout.md): `check`
   prints the deltas but does not block on them unless you pass `--gate-quality`.

## Complexity & churn: direction and tolerance

The pass-set ratchets **up** (its high-water mark is the *largest* set; it
regresses when it *shrinks*). Complexity ratchets the **opposite** way: it is a
cost, so the high-water mark is the **lowest (best)** value ever merged, and a
metric that *rises* is the regression. The portfolio audit motivating this
(issue #110) saw `duplicat-rex` mean CCN drift 3.1→5.2 and `chief-wiggum` reach
16.7 % of functions over CCN 10 before self-correcting — exactly the upward
creep this dimension catches.

Because per-run noise is normal, a metric regresses only when it exceeds a
**tolerance band** around the best value:

```
regressed  ⇔  current  >  best × (1 + <metric>_rel)  +  <metric>_abs
```

The band is configurable per repo under `quality_tolerance` in `ratchet.json`
(defaults in `ratchet.py:DEFAULT_QUALITY_TOLERANCE`):

```json
{
  "quality_tolerance": {
    "ccn_mean_rel": 0.10,       "ccn_mean_abs": 0.5,
    "pct_ccn_gt10_rel": 0.10,   "pct_ccn_gt10_abs": 1.0,
    "relative_churn_rel": 0.25, "relative_churn_abs": 0.05
  }
}
```

`score` records `quality: {ccn_mean, pct_ccn_gt10, relative_churn, ...}` in the
hash-chained record. The snapshot is **optional and fast-failing**: it leans on
`lizard` (from the [code-metrics](../scripts/quality) engines), and if lizard is
absent it records `quality: {"skipped": ...}` and never crashes `score` — skipped
snapshots contribute nothing to the high-water mark and never register a
regression. Journals written before this dimension existed carry no `quality`
block; chain verification and high-water derivation tolerate that unchanged.

## Tamper-evident journal

The journal (`docs/quality/ratchet-journal.jsonl` in the target repo) is an
append-only **hash chain**: each record's hash covers its body plus the
previous record's hash. The high-water mark is *derived* from the verified
chain — never read from a separately-editable file — so editing history to
lower the bar breaks the chain and every subsequent `check` fails closed
(exit 4). `ratchet-highwater.json` is a display cache only.

Records also serve as **amnesia context**: `ratchet.py recent` replays the
last N iterations' notes so a fresh session doesn't oscillate on decisions a
previous one already made.

## State (committed to the target repo)

```
docs/quality/
├── ratchet.json            # config: suites, epic docs root, protected paths
├── ratchet-journal.jsonl   # append-only hash chain — never hand-edit
├── ratchet-highwater.json  # derived cache, display only
└── ratchet-scorecard.json  # latest `score` snapshot
```

`ratchet.json` declares the test suites project-agnostically — a command plus a
parser (`go-test-json`, `junit-xml`, or `pass-fail-lines`):

```json
{
  "suites": [
    {"name": "go", "cmd": "go test -json -count=1 ./...", "cwd": "backend", "parser": "go-test-json"},
    {"name": "web", "cmd": "npx vitest run --reporter=junit --outputFile=junit.xml",
     "cwd": "web", "parser": "junit-xml", "report": "web/junit.xml"}
  ],
  "epic_docs": "docs/epics",
  "protected_paths": ["docs/epics/*/contracts.md", "docs/quality/**", "..."],
  "quality_tolerance": {"ccn_mean_rel": 0.10, "ccn_mean_abs": 0.5, "...": "..."}
}
```

## CLI

```bash
python3 scripts/ratchet.py init --repo <target>        # starter config (autodetects go/pytest)
python3 scripts/ratchet.py score                       # run suites + hash contracts + complexity/churn → scorecard
python3 scripts/ratchet.py score --no-tests            # contract hashes only (cheap baseline)
python3 scripts/ratchet.py score --no-quality          # skip the complexity/churn snapshot (no lizard)
python3 scripts/ratchet.py score --venv <venv>         # point the quality snapshot at a venv with lizard
python3 scripts/ratchet.py check                       # exit 1 on regression/weakening/removal
python3 scripts/ratchet.py check --gate-quality        # ALSO exit 1 on complexity/churn regression (opt-in)
python3 scripts/ratchet.py protected --base origin/main  # exit 1 if goalposts touched
python3 scripts/ratchet.py record --event ticket --ref "#42" --merged --notes "..."
python3 scripts/ratchet.py record --event epic-close --ref order-lifecycle --merged \
    --amend CTR-order-001 --retire INV-order-003 --notes "contract revised per review"
python3 scripts/ratchet.py recent --n 5                # amnesia context for the next session
```

Exit codes: `0` ok, `1` gate violation, `2` usage/config error, `3` no
scorecard (run `score` first), `4` journal tamper.

## Where it gates

- **`/architect`** — after committing epic artifacts: `score --no-tests` +
  `record --event baseline --merged`, so the contract definitions enter the
  high-water mark the moment they're approved.
- **`/implement` Step 8** — after the full test suite: `score` + `check`. A
  violation is a hard blocker, same as a failing test.
- **`/implement-wave`** — per-ticket: `protected` on each worker branch before
  merging (hits ⇒ park the ticket for the human). Per-wave: `score` + `check`
  on the staging branch before promoting to main, then `record --event wave
  --merged` after the push.
- **`/close-epic`** — `score` + `check` must report *held* or *advanced* across
  the epic, then `record --event epic-close --merged`. Legitimate contract
  revisions are journaled here with `--amend`/`--retire` — a deliberate,
  visible human decision, not a silent edit.

The **complexity/churn** dimension is not wired as a blocker anywhere yet: per
[gate-rollout.md](gate-rollout.md) it ships report-only (`check` surfaces the
deltas in the run output but only `--gate-quality` blocks) until it has been
validated on a real, already-shipped repo. Promote it to `--gate-quality` in a
follow-up that cites the dry-run.

Like the other gates, it degrades gracefully: a target repo with no
`docs/quality/ratchet.json` skips the ratchet (the workflows treat it as
not-yet-adopted rather than failing). Adopt it with `init` + a baseline record.

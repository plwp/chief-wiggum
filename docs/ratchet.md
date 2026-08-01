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

   In **sidecar footprint mode** ([sidecar.md](sidecar.md)) this rule holds
   **by construction, not by diff inspection**: the goalposts live outside the
   target tree entirely, so a worker in the target worktree *physically
   cannot* write them — there is no diff to park because there is no path to
   the file. `protected` remains the enforcement for embedded targets, where
   the goalposts share the tree with the code.

Plus two further dimensions (one validated-and-wired, one report-only):

4. **Verifier-test bodies are goalposts too (#206).** The pass-set is keyed by
   test ID, so a high-water test's *body* can be rewritten to bless new
   behavior while its node ID stays green — **channel C1c** in the
   goalpost-integrity threat model. The experiment
   ([docs/paper/experiment/](paper/experiment/design.md)) proved this both
   ways: the scripted probe slipped through every detector, and an unprompted
   pilot worker independently invented the same evasion, deliberately keeping
   a stale test name "so the ratchet pass-set node ID does not shrink".
   Blanket-hashing all test bodies would park every honest TDD test edit (the
   noisy-gate failure mode [gate-rollout.md](gate-rollout.md) forbids), so
   the fix is targeted: a test annotated `@cw-trace verifies CTR-x` is the
   **executable expression of a contract**, and its function body is hashed
   into the scorecard (`verifier_test_hashes`) and ratcheted exactly like
   contract definitions. Revision is a journaled human act: `record
   --amend-verifier <relpath>::<function>` for a deliberate refactor, and
   `--retire-verifier` to drop a deleted test from the high-water mark (both
   validated against the current scorecard / high-water, so a typo'd ref is
   surfaced, not a silent no-op). Amending a CONTRACT does **not** silently
   bless its verifier tests' new bodies — a body that also changed must be
   named explicitly with `--amend-verifier` in the same record or the amend is
   refused, so a C1c rewrite can't ride along invisibly on an unrelated
   contract-wording amend (#206 soundness review); an unchanged body needs
   nothing. Annotations the extractor cannot hash (non-Python files, syntax
   errors, unattached or ambiguous annotations) are **counted and surfaced**
   by `score`, never silently dropped. The dimension's
   [gate-validation record](gate-validation.md) **passed** (journaled
   `rec-00021`; wired `rec-00022` via `check_gate_validation.py ratchet
   --wire`, chief-wiggum#208), so `/implement` Step 8 and `/close-epic`
   Step 2f now pass `--gate-verifier-tests` whenever
   `check_gate_validation.py ratchet --gate` confirms the record still
   passes — with the standard downgrade-to-report-only-and-surface posture
   if it ever goes stale. At the CLI the flag stays opt-in: a bare `check`
   prints weakened/removed verifier tests without blocking.
   Authority boundary (v1): the hash covers the annotated test function's own
   source span — assertions relocated into a shared helper the test calls are
   a documented no-fire boundary (probed by the record's
   `evasion-config-indirection` seed); only Python is extracted; annotations
   are read from comment tokens and docstrings only (a string literal can
   never pin — fixture-generator tests are not verifier tests); documentation
   formats (`.md`/`.rst`/`.txt`) are out of scope by design, since their
   annotations are syntax examples, not tests; and a duplicate qualified name
   (a function redefined under a conditional, a shadowed class) is ambiguous —
   surfaced as unscanned and refused, never recorded as a silently-overwritten
   single hash.

5. **Complexity & churn only ratchet down.** Mean cyclomatic complexity (McCabe
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

## High-water test-file cue in `protected` (#207)

`score` also records which file (or go package directory) each passing case
was collected from (`test_files` in the scorecard; junit `classname`/`file`
resolution, go package-suffix matching — cases that resolve to nothing are
listed in `test_files_unresolved`, surfaced rather than guessed). `protected`
uses that map to print a **permanently report-only** notice whenever the
branch diff modifies a file hosting high-water cases, naming the cases so the
reviewer knows which test diffs to scrutinize. It never changes the exit
code: the blocking answer to a test-body rewrite is the verifier-hash
dimension above; this is the day-one reviewer cue for everything else. In the
agentic pilot both tampering workers modified a high-water test file and both
honest workers did not — the cue flags exactly the branch that slipped every
gate. A missing scorecard or broken journal chain degrades **visibly**
("cue unavailable"), never silently. File/dir resolution is implemented for
the `junit-xml` and `go-test-json` parsers only; a `pass-fail-lines` suite
resolves no files (every case lands in `test_files_unresolved`), so the cue
does not cover it — an accepted gap, since the blocking answer to a test-body
rewrite is the verifier-hash dimension above, not this cue.

## Trust boundary: what the chain does and does not prove (#209)

The hash chain detects **interior** rewrites and fails closed (exit 4): tamper
a record and its stale hash breaks (probe C5a); recompute the tampered
record's own hash and the *next* record's link breaks (C5b). The **tail
record has no next link** — a tail tamper with a recomputed hash produces a
chain that verifies. This blind spot is by construction, and it is covered by
the layers outside the chain, not by the chain itself:

- `protected` parks any worker branch touching `docs/quality/**` — a
  rewritten journal riding a worker branch is caught before merge (probe C5c
  demonstrated exactly this: full-journal forgery, parked).
- git history is the outer anchor for anything that reached the default
  branch: a journal rewrite is a visible diff in a protected path.
- A human with force-push rights over the default branch is **outside the
  threat model**: the ratchet's claim is tamper-evidence against the agent
  and against accident, within the repo's own trust boundary.

In-band anchoring (embedding commit SHAs in records, countersigning, DSSE) is
**deliberately not built** — the same deferral, for the same reason, as
[gate-validation.md](gate-validation.md)'s "no signing/DSSE": not until an
external party requires attestation beyond an append-only chain inside the
repo's own trust boundary. This is a stated assumption, not a TODO. Probe
results: [docs/paper/experiment/results/channel-probes.md](paper/experiment/results/channel-probes.md).

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
python3 scripts/ratchet.py check --gate-verifier-tests # ALSO exit 1 on verifier-test body changes (opt-in, #206)
python3 scripts/ratchet.py check --gate-quality        # ALSO exit 1 on complexity/churn regression (opt-in)
python3 scripts/ratchet.py protected --base origin/main  # exit 1 if goalposts touched
python3 scripts/ratchet.py record --event ticket --ref "#42" --merged --notes "..."
python3 scripts/ratchet.py record --event epic-close --ref order-lifecycle --merged \
    --amend CTR-order-001 --retire INV-order-003 --notes "contract revised per review"
python3 scripts/ratchet.py record --event epic-close --ref order-lifecycle --merged \
    --amend-verifier "tests/test_order.py::test_date_range" --notes "verifier refactor (#206)"
python3 scripts/ratchet.py recent --n 5                # amnesia context for the next session
```

Exit codes: `0` ok, `1` gate violation, `2` usage/config error, `3` no
scorecard (run `score` first), `4` journal tamper.

## Where it gates

- **`/architect`** — after committing epic artifacts: `score --no-tests` +
  `record --event baseline --merged`, so the contract definitions enter the
  high-water mark the moment they're approved.
- **`/implement` Step 8** — after the full test suite: `score` + `check
  --gate-verifier-tests` (the flag is passed whenever `check_gate_validation.py
  ratchet --gate` confirms the dimension's record still passes, #208). A
  violation is a hard blocker, same as a failing test.
- **`/implement-wave`** — per-ticket: `protected` on each worker branch before
  merging (hits ⇒ park the ticket for the human). Per-wave: `score` + `check
  --gate-verifier-tests` (same record-gated flag) on the staging branch before
  promoting to main, then `record --event wave --merged` after the push.
- **`/close-epic`** — `score` + `check --gate-verifier-tests` (same
  record-gated flag as `/implement`, Step 2c2/2f) must report *held* or
  *advanced* across the epic, then `record --event epic-close --merged`.
  Legitimate contract revisions are journaled here with `--amend`/`--retire`,
  and deliberate verifier-test revisions with
  `--amend-verifier`/`--retire-verifier` — a deliberate, visible human
  decision, not a silent edit.

The **complexity/churn** dimension is not wired as a blocker anywhere yet: per
[gate-rollout.md](gate-rollout.md) it ships report-only (`check` surfaces the
deltas in the run output but only `--gate-quality` blocks) until it has been
validated on a real, already-shipped repo. Promote it to `--gate-quality` in a
follow-up that cites the dry-run.

`check`/`regressed` also cross-reference `docs/quality/trace-links.json` (the
suspect-link sidecar written by `check_traceability.py --write-links`, #169)
against the CURRENT scorecard's `contract_hashes`: any link recorded against a
hash that no longer matches is printed as a `suspect_links` finding — a
definition-hash change with surviving suspect links is **visible, not
silent**, even though (like complexity/churn) it is report-only and does not
change `check`'s exit code. See [traceability.md](traceability.md) for the
sidecar format and how a link becomes suspect.

Like the other gates, it degrades gracefully: a target repo with no
`docs/quality/ratchet.json` skips the ratchet (the workflows treat it as
not-yet-adopted rather than failing). Adopt it with `init` + a baseline record.

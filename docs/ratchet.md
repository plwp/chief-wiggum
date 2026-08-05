# Quality ratchet: fix-forward without sliding backward

Chief Wiggum's implementation loops are autonomous between human checkpoints —
workers write code, merge waves, and ship PRs with the orchestrator as the only
reviewer in the loop. The ratchet is the deterministic mechanism that makes
that survivable: **quality may only move up or hold, never down**, and the
record proving it is tamper-evident.

Three rules, all mechanical:

1. **The pass-set never shrinks.** Every test case that has ever passed on the
   default branch is the **high-water mark**. A merge that would make a
   high-water case fail is blocked — no "we'll fix it next ticket". The one
   sanctioned way out is a journaled `record --retire-case` carrying a
   reason, owner, and expiry — the same JUSTIFIED-waiver shape `/adopt` uses
   for grandfathers (#278). It is a human act, it is tamper-evident, and it
   **expires**.
2. **Passing by weakening doesn't count.** Every stable-ID'd contract block
   (`CTR-`/`INV-`/`BR-`, see [traceability.md](traceability.md)) is hashed. A
   high-water contract whose definition changed was *weakened*; one that
   disappeared was *removed*. Both block, exactly like a test regression —
   unless a human deliberately journals an `--amend`/`--retire`.
3. **Workers don't move their own goalposts.** Contracts, invariants,
   integration-test specs, formal models, and the ratchet's own state form the
   **protected pathset**. A worker branch that touches them is *parked* for
   human review, never auto-merged.

   In **sidecar footprint mode** ([sidecar.md](sidecar.md)) the goalposts
   live outside the target tree entirely, so a goalpost edit **cannot ride in
   the worker's reviewed diff** — there is no path in the tree for a branch
   to touch, which removes the C2-style channel (a goalpost move hidden
   inside a reviewed code change). The scope of that claim is the diff, not
   the disk: workers are not filesystem-sandboxed, and same-user filesystem
   access (or setting `CHIEF_WIGGUM_USER_DIR`) can still reach the sidecar —
   see sidecar.md's "Trust boundary". `protected` remains the enforcement for
   embedded targets, where the goalposts share the tree with the code.

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
(issue #110) saw an agentic SaaS-cloning tool's mean CCN drift 3.1→5.2 and `chief-wiggum` reach
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
the `junit-xml`, `go-test-json`, and `trx` parsers; a `pass-fail-lines` suite
resolves no files (every case lands in `test_files_unresolved`), so the cue
does not cover it — an accepted gap, since the blocking answer to a test-body
rewrite is the verifier-hash dimension above, not this cue. TRX records the
test DLL and never the source file, so its resolution goes through the class
name and only lands when exactly one tracked `<ClassName>.cs` exists under the
suite cwd — ambiguous or absent matches stay unresolved, never guessed.

### The `trx` parser (.NET, #263)

`dotnet test --logger trx` is the built-in VSTest logger: it needs no NuGet
package added to the target, unlike a JUnit logger — which matters because
adoption must not mutate the repo it is only surveying. Three properties are
load-bearing, and each one is the difference between a real pass-set and a
vacuously empty one:

- **`report` names a DIRECTORY, not a file.** A solution runs one logger per
  test project, so a fixed `LogFileName` makes each project overwrite the
  last (VSTest even says `WARNING: Overwriting results file`) and silently
  drops every project but one. Every `*.trx` under the directory is parsed.
- **The directory is cleared before each run.** It accumulates across runs, so
  a stale file would keep a since-deleted test in the high-water mark forever.
- **Only `outcome="Passed"` counts.** TRX has ~10 outcomes; `NotExecuted` is a
  skip and `Failed`/`Error`/`Timeout`/`Aborted`/`Inconclusive`/
  `PassedButRunAborted` are not passes. A case seen both passed and failed
  (a flaky retry) is not passing, matching the other parsers.

Case IDs are `ClassName::LocalName`, resolved through `<TestDefinitions>`, and
keep xunit `[Theory]` data rows distinct (`ParameterisedPasses(n: 2)`) so
losing one data row reads as the regression it is. A run that writes no TRX at
all raises rather than returning an empty pass-set — "the runner produced
nothing" must never render as "nothing was wrong".

Autodetection names each solution explicitly (`dotnet test "Api.sln"`): a bare
`dotnet test` in a root holding more than one solution fails outright with
MSB1011, and real .NET monoliths routinely ship several. Note `dotnet test`
builds into per-project `bin/`/`obj/` inside the tree, which a standard .NET
`.gitignore` covers; only the TRX output is re-pointed outside the tree during
`/adopt`.

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

## chief-wiggum's own pass-set (#262)

This repo ratchets itself. `docs/quality/ratchet.json` configures the pytest
suite, and the baseline record puts the whole passing suite into the high-water
mark. Before #262 `suites` was `[]`, so the pass-set mark was vacuously zero —
it could not slide because there was nothing to slide, while the contract (49)
and verifier-hash (116) dimensions were always real. #259's `NOT MEASURED`
marker is what surfaced it.

Two consequences worth knowing before you touch a test here:

- **Test ids are now goalposts.** Renaming, moving, re-parametrising or
  deleting a test drops its case id from the pass-set and `check` blocks with
  `missing_tests`. The sanctioned way out is a journaled `record --retire-case`
  (below), never an edit to the scorecard. Parametrised cases are pinned
  individually, so one `@pytest.mark.parametrize` id edit moves a whole table —
  use a glob or `--retire-case-file`.
- **`score` runs the full suite** (~3 min), including inside `/implement`
  Step 8, which has already run it. That double run is tracked in #284.

`--retire-case` is a quarantine with a mandatory expiry, designed so a flaky
test stays visible pressure. A renamed or deleted case never comes back, so a
quarantine on it would just expire and re-block, needing renewal forever —
`--retire-case-permanent` is the distinct, honestly-named path for that case
(#290; see "Permanently retiring a pass-set case" below).

## Retiring a pass-set case (flaky quarantine, #278)

A test case excluded from the suite — moved, deleted, or disabled because it
is flaky or order-dependent — does NOT leave the high-water mark on its own:
`derive_highwater` only ever *unions* `pass_set`, so the case sits there
forever and every subsequent `check` blocks with `missing_tests`, a
permanent-red state no amount of unrelated fixing clears. The sanctioned way
out is a journaled `record --retire-case`, not silence, not an edit to the
scorecard, and not `--force`.

```bash
python3 scripts/ratchet.py record --event ticket --ref "#42" \
    --retire-case 'pytest::tests/test_flaky.py::TestOrder::test_a' \
    --retire-case-reason "order-dependent shared state; passes in isolation" \
    --retire-case-owner plwp --retire-case-expiry 2026-11-01
```

- **Per-record reason/owner/expiry, not per-case.** The real scenario is one
  flaky class → one justification → many case IDs, so distinct reasons get
  distinct records (each its own chain link) rather than per-case metadata.
- **Scaling to hundreds of cases**: `--retire-case` accepts an `fnmatch` GLOB
  (not `_glob_to_re` — its `*` compiles to `[^/]*` and cannot match a go case
  ID carrying `/` in its package path), and `--retire-case-file PATH` reads
  newline-separated case IDs/globs (blank lines and `#`-comments ignored) —
  feed it straight from `ratchet.py regressed | jq -r '.missing_tests[]'`.
  **Globs are expanded and MATERIALIZED at record time**: the journal stores
  only the explicit case IDs a glob matched *today*, never the pattern, so a
  quarantine can never silently widen to catch a future, unrelated test.
- **Entry shape** — byte-compatible with `/adopt`'s grandfather entries (minus
  `source_engine`), so `grandfather.is_expired` reads it unchanged:
  `{"id", "reason", "owner", "expiry", "created_at"}`.
- **Naming split (deliberate)**: the journal key is `retired_cases` (what
  *this record did* — an event-log verb); the derived high-water key is
  `quarantined` (what *state the repo is in* — a condition noun). `check`,
  `highwater`, `recent`, and `/status` all read the latter.
- **The eight guards** (docs/ratchet.md's bar-lowering-hole checklist):
  1. `docs/quality/**` is already in the protected pathset — a quarantining
     branch is parked by `ratchet.py protected` before merge, same as any
     other journal write.
  2. The append-only hash chain makes the retirement attributable and
     tamper-evident (interior tamper fails closed, exit 4).
  3. **Mandatory non-empty reason** — a waiver without one is amnesty, not a
     journaled decision.
  4. **Mandatory expiry**, defaulted to 90 days, never accepted in the past;
     a missing or unparseable expiry counts as **expired** (inherited from
     `grandfather.is_expired`) — a hand-edited entry that drops its expiry
     fails closed rather than becoming permanent amnesty.
  5. **Refuses to retire a currently-PASSING case** — an agent cannot
     pre-emptively quarantine the cases it is about to break.
  6. **Refuses a case not in the high-water mark** — the same doctrine as
     `--retire-verifier`: a typo'd case ID is SURFACED as an error, never a
     silent no-op.
  7. **Globs are materialized, never stored** (above).
  8. **Volume is surfaced everywhere**: `check` (a report-only `[report-only]`
     line plus a modified OK line naming the count), `recent` (`[quarantined]`
     status), `highwater` (the `quarantined` map with a live `expired` flag
     per entry), `/status` (PARTIAL COVERAGE + count + nearest expiry +
     WARNING on expiry), and `reflect ratchet_health` (`retired_cases` count).
- **Self-healing, no second human act**: a case that returns to a LATER
  *merged* record's `pass_set` is restored to `pass_set` by the fold, and its
  stale quarantine metadata is dropped in the same pass (`quarantined.pop`) —
  a fixed flaky test does not show as "quarantined" forever. There is no
  `--unretire-case` flag; the fold already reaches that state.
- **Renewal, not amnesty**: expiry is the clock that forces re-litigation. Once
  a quarantine expires, the case re-enters `missing_tests` and blocks again
  (via the EXISTING finding class — no new blocking dimension was introduced;
  see the gate-validation cost calculus below). Renewing it is a NEW record
  (`record --retire-case` again with a fresh expiry), never an edit to the old
  one — the journal's last-wins semantics make the new entry the live one, so
  the chain shows exactly how many times a quarantine was rolled over.
- **No new blocking finding class, no new exit code.** `check --format json`
  gains `quarantined`/`expired_quarantines` keys additively; the five
  original `violations()` keys are unchanged in name and meaning. An expired
  quarantine's case reappears in the pre-existing `missing_tests` class — the
  detection, its exit semantics, and its eight seeded-defect trials are all
  unchanged, so no new gate-validation record was required (only a
  `scanner_version` restamp — see docs/gate-validation.md's cost calculus for
  why a new blocking dimension was deliberately avoided here).
- **Backward compatibility**: a pre-#278 `ratchet.py` reading a post-#278
  journal ignores the unknown `retired_cases` key and keeps every quarantined
  case in the high-water mark — it fails toward MORE strictness, never less.

## Permanently retiring a pass-set case (#290)

**Quarantine says "this should come back"; permanent retirement says "this is
gone."** Only the first deserves an expiry. A test case that was renamed,
re-parametrised, moved, or deleted will never pass again under its old case
ID — quarantining it just defers the problem: the quarantine expires, the
case re-enters `missing_tests`, and the only remedy is renewing the
quarantine every 90 days, forever, for a test that isn't coming back.

`--retire-case-permanent` is a DISTINCT terminal path, not a longer expiry:

```bash
python3 scripts/ratchet.py record --event ticket --ref "#42" \
    --retire-case 'pytest::tests/test_old_name.py::test_x' \
    --retire-case-reason "renamed to test_new_name.py::test_x; old id is gone for good" \
    --retire-case-owner plwp --retire-case-permanent
```

- **Same resolution path as quarantine** (`--retire-case`/`--retire-case-file`,
  exact-ID-before-glob, materialized at record time, refuses a still-passing
  or not-in-high-water case) — only the terminal `kind` and the expiry policy
  differ. A case can also **graduate** from a standing quarantine straight to
  permanent (an operator gives up renewing it): it stays reachable through the
  same CLI path quarantine renewal uses.
- **Mandatory reason, and a MANDATORY EXPLICIT owner** — attribution is not
  the thing being relaxed for a permanent decision. Unlike quarantine, the
  lax `unassigned` default is refused; `--retire-case-owner` must be given.
- **Rejects an expiry as a usage error.** `--retire-case-expiry` /
  `--retire-case-expiry-days` alongside `--retire-case-permanent` is refused
  outright — accepting one would make a permanent retirement indistinguishable
  from a quarantine nobody intends to renew, defeating the entire point of a
  distinct, honestly-named path.
- **Entry shape**: `{"id", "reason", "owner", "expiry": null, "created_at",
  "kind": "removed"}` — the same journal key (`retired_cases`) as quarantine,
  distinguished by `"kind"`. An entry with no `kind` key (every pre-#290
  journal record) defaults to quarantined — unchanged backward compatibility.
- **A SEPARATE derived bucket, `removed_cases`** (`derive_highwater`), never
  `quarantined` — so the flaky-quarantine list stays a list of flakes. No
  expiry logic applies to it AT ALL: `effective_pass_set` never reads it, so a
  permanently retired case never re-enters `missing_tests`, regardless of
  elapsed time — there is nothing to check, by construction, not merely a
  very long timer.
- **Renders separately everywhere a human looks**: `check` (its own
  `[report-only]` line, `N case(s) permanently retired`, plus its own clause
  on the OK line), `check --format json` (`removed_cases` key, additive),
  `recent` (`[removed]` status, distinct from `[quarantined]`), `highwater`
  (its own `removed_cases` map, with no `expired` overlay since none applies),
  and `/status` (its own `permanently retired: N case(s)` line, separate from
  the quarantine section, no expiry/warning language at all).
- **Self-healing, same doctrine as quarantine**: if the exact case ID somehow
  reappears in a later *merged* record's `pass_set`, it is restored and its
  stale `removed_cases` metadata is dropped — no second human act needed.
- **No new blocking finding class, no new exit code** — same cost calculus as
  quarantine: a permanently retired case simply never contributes to
  `missing_tests`, ever. Only a `scanner_version` restamp was needed, not a
  new gate-validation record.
- **Backward compatibility**: a pre-#290 `ratchet.py` reading a post-#290
  journal has no notion of `"kind": "removed"` and folds every retired-cases
  entry into `quarantined` regardless of kind — since a permanent entry
  carries `expiry: null`, and a missing/unparseable expiry counts as expired
  (inherited fail-closed posture), such a reader treats a permanent
  retirement as an ALREADY-EXPIRED quarantine and re-blocks on it. This fails
  toward MORE strictness, never less — the same direction #278's own
  backward-compat guarantee takes.

## State (committed to the target repo)

```
docs/quality/
├── ratchet.json            # config: suites, epic docs root, protected paths
├── ratchet-journal.jsonl   # append-only hash chain — never hand-edit
├── ratchet-highwater.json  # derived cache, display only
└── ratchet-scorecard.json  # latest `score` snapshot
```

`ratchet.json` declares the test suites project-agnostically — a command plus a
parser (`go-test-json`, `junit-xml`, `trx`, or `pass-fail-lines`):

```json
{
  "suites": [
    {"name": "go", "cmd": "go test -json -count=1 ./...", "cwd": "backend", "parser": "go-test-json"},
    {"name": "web", "cmd": "npx vitest run --reporter=junit --outputFile=junit.xml",
     "cwd": "web", "parser": "junit-xml", "report": "web/junit.xml"},
    {"name": "dotnet", "cmd": "dotnet test \"Api.sln\" --logger trx --results-directory .ratchet-trx",
     "cwd": ".", "parser": "trx", "report": ".ratchet-trx"}
  ],
  "epic_docs": "docs/epics",
  "protected_paths": ["docs/epics/*/contracts.md", "docs/quality/**", "..."],
  "quality_tolerance": {"ccn_mean_rel": 0.10, "ccn_mean_abs": 0.5, "...": "..."}
}
```

## CLI

```bash
python3 scripts/ratchet.py init --repo <target>        # starter config (autodetects go/pytest/dotnet)
python3 scripts/ratchet.py state --repo <target>       # absent|stub|unbaselined|real|invalid — "was this repo
                                                       # ever ratcheted?" (#356); the journal, not the config
                                                       # file's existence, is the history signal
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
python3 scripts/ratchet.py record --event ticket --ref "#42" \
    --retire-case 'pytest::tests/test_flaky.py::*' --retire-case-reason "order-dependent shared state" \
    --retire-case-owner plwp --retire-case-expiry 2026-11-01   # quarantine a flaky class (#278)
python3 scripts/ratchet.py record --event ticket --ref "#42" \
    --retire-case 'pytest::tests/test_old_name.py::test_x' \
    --retire-case-reason "renamed, old id gone for good" \
    --retire-case-owner plwp --retire-case-permanent          # permanent retirement, no expiry (#290)
python3 scripts/ratchet.py highwater      # includes `quarantined` (live expiry) + `removed_cases`
python3 scripts/ratchet.py recent --n 5                # amnesia context for the next session
```

Exit codes are UNCHANGED by #278/#290: `0` ok, `1` gate violation, `2`
usage/config error, `3` no scorecard (run `score` first), `4` journal tamper.

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
  deliberate verifier-test revisions with
  `--amend-verifier`/`--retire-verifier`, flaky/order-dependent pass-set
  cases with `--retire-case` (#278), and renamed/re-parametrised/deleted
  cases with `--retire-case --retire-case-permanent` (#290) — a deliberate,
  visible human decision, not a silent edit.

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

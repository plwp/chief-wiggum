# /adopt — brownfield entry: survey, elect, baseline, grandfather, record

Every other pipeline path starts at `/transcribe` or `/seed` — on a repo CW
didn't build, the gates either hard-fail (`ratchet.py` with no config,
`check_single_writer.py` with no epic dir) or pass vacuously. `/adopt`
(chief-wiggum#215) is the missing entry arrow, and it is deliberately
**lightweight**: it measures, elects, baselines, and waives — it does **not**
infer contracts (see [the deferral](#contract-inference-is-deferred)).

```
existing codebase → /adopt → ( /architect per feature epic | /plan-epic --from-debt )
```

Mechanics live in `scripts/adopt.py`; the `/adopt` command is a thin adapter
(`.claude/commands/adopt.md`).

## The sequence

1. **Shape survey** (`adopt.py survey`) — repo age (first-commit date), size
   (tracked files; languages via the same `quality.population` the debt
   engines scan), per-language test-file counts, a **real coverage-baseline
   attempt** (the test command is detected the way `run_verification.py`
   detects it and actually run; pass/fail counts are parsed from the runner's
   own output or honestly reported unparsed — never fabricated; no detected
   runner is stated as exactly that), and CI presence. Output includes a
   **persisted per-gate applicability verdict** — `applicable` /
   `report-only` / `inapplicable`, each with a one-line reason — for the 7
   shipped gates plus the debt inventory (e.g. `saas_gate` inapplicable
   without a base URL; `check_traceability` report-only until contracts
   exist; `ratchet` applicable once baselined). Written to
   `<meta root>/adoption/survey.json`, stamped with `target_sha`.
2. **Elections** (`adopt.py elect`) — footprint mode + domain scope via the
   #213 resolver ([sidecar.md](sidecar.md)). Defaults: **`sidecar` +
   whole-repo scope**. `--scope-from-codeowners` seeds `scope.json` from
   CODEOWNERS patterns when the file exists (mapping: leading `/` stripped,
   trailing `/` → `<dir>/*`, bare patterns kept — fnmatch semantics, `*`
   crosses `/`; catch-alls skipped) and skips with a note when it doesn't.
3. **Ratchet baseline** (`adopt.py baseline`) — `ratchet.py init` (module
   call), then a **real test run** for the pass-set baseline using ratchet's
   own suite autodetection and scoring — never `--no-tests` (the `/architect`
   baseline's empty pass-set is exactly what adoption must not record).
   Journaled as a merged `baseline` record (`--notes "adoption baseline"`).
   Quality dimensions (complexity/churn) baseline at current values — `score`
   snapshots them. From day one: **debt cannot grow**. Junit reports are
   re-pointed outside the target tree, and the suite run's **child
   environment** sets `PYTHONDONTWRITEBYTECODE=1` plus
   `PYTEST_ADDOPTS="-p no:cacheprovider"` — env-based so the suppression
   reaches pytest even through `make`/wrapper commands — leaving the target
   tree genuinely byte-clean in sidecar mode. **Empty baselines are recorded
   as not-run**: when no suite is detected (or the configured suites yield
   zero passing cases), the score falls back to `--no-tests` semantics
   (`tests_run: false`), prints `no test suites ran — pass-set baseline EMPTY
   (recorded as not-run)`, cross-checks the survey's no-runner verdict, and
   the journal note says exactly that — an empty pass-set is never journaled
   as a real test run. Recording it honestly is not enough on its own, though:
   see **Not measured is not measured clean** below.
4. **Debt inventory baseline** — the #214 inventory
   (`debt_inventory.build_inventory`) written to the resolver quality dir's
   `debt.json`: the adoption debt baseline.
5. **Grandfathering** (`adopt.py grandfather`) — every baseline finding is
   recorded in `<meta root>/adoption/grandfathered.json`, modeled on the
   JUSTIFIED-waiver shape ([traceability.md](traceability.md)).
6. **The adoption record** (`adopt.py record`) —
   `<meta root>/adoption/adoption.json`, the brownfield **switch**.

`adopt.py run` chains all of it with per-step output. One ordering note: `run`
performs the **election first** — the election decides where the survey (and
every later artifact) persists, so surveying before electing would write the
survey into the embedded default, i.e. the very target tree a sidecar adoption
exists to keep clean. Step-by-step adoptions **must** elect first: standalone
`survey`/`baseline`/`grandfather`/`record` on a target with **no election
file refuse** (exit 2, "embedded mode is an explicit choice") rather than
falling through to the resolver's silent embedded default and writing the
target's own tree.

**Re-adoption is an explicit operator act.** `run` on a target whose
`adoption.json` already exists refuses ("already adopted (adopted_at …)")
unless `--re-adopt` is passed. Likewise `grandfather` against an existing
`grandfathered.json` refuses by default, printing the exact ids that would be
**newly added** (those are post-adoption findings — waiving them is amnesty,
not baselining); `--extend` performs it explicitly with a loud "amnestying N
POST-adoption finding(s)" line, preserving the original `created_at` and the
original entries' expiry (only the new entries get a fresh expiry from now,
and the file gains `extended_at`).

## Adoption-record schema (`adoption/1`)

```json
{
  "schema": "adoption/1",
  "adopted_at": "2026-08-02T…Z",
  "brownfield": true,
  "mode": "sidecar",
  "backing": "local",
  "scope": "whole repo (no scope.json at …)",
  "gates": { "<gate>": {"verdict": "applicable|report-only|inapplicable", "reason": "…"} },
  "baseline": {
    "ratchet_record_id": "rec-00001",
    "debt_sha256": "…",
    "debt_items": 42
  },
  "grandfather": { "file": "…/adoption/grandfathered.json", "entries": 42, "nearest_expiry": "2026-10-31" },
  "target_sha": "<HEAD at adoption>"
}
```

Consumers:

- **`/architect`** reads it in place of the old `IS_NEW_PRODUCT` two-file
  existence check: an adoption record ⇒ NOT a new product (fixes the
  misclassification of imported code), and DST-readiness invariants are not
  stamped — retrofitting determinism seams is a separate, deliberate decision.
- **#216 (`/implement`/`/implement-wave`)** keys **scope-discipline mode** off
  it for every ticket kind on this repo: declared touch plan at the approach
  step, out-of-pathset hunk flagging, found-≠-fixed filing into the inventory,
  no-collateral-edit signal. Brownfield is a property of the **repo**, not the
  ticket; the mechanics live in #216 — this record is the switch.
- **`/status`** shows the Adoption section: brownfield flag, grandfather
  counts, nearest expiry, and prominent EXPIRED warnings.

## Not measured is not measured clean (#259)

An honest report of "nothing measurable" and a real report of "everything
measured, all clean" used to render **identically** on every surface a human
reads. Adopting a 12,551-file .NET monolith produced `pass-set: 0 case(s)`,
`inventory present, zero items`, `grandfathered: none` — all technically
truthful, all indistinguishable from a healthy repo. The ratchet's guarantee
was intact and meaningless: a high-water mark of zero can never slide.

`/status` now carries a `not_measured` map (section → reason) and prints a
`NOT MEASURED:` line above the affected section:

```
## Ratchet high-water

NOT MEASURED: no test runner detected — ratchet.json configures 0 suite(s), so
the high-water mark is zero and can never slide
pass-set: 0 case(s) | contracts: 0 | verifier hashes: 0

## Debt

NOT MEASURED: no known-language source files in the scan population: the
engines had nothing to scan — unscanned: unknown-language (.cs): 8316, ...
inventory present, zero items — absence of findings is NOT health
```

The marker is **proved, never inferred** — which is what keeps it from crying
wolf:

- The ratchet reason distinguishes three states: no suite configured at all,
  suites configured but the score recorded `tests_run: false`, and suites that
  genuinely ran and produced zero passing cases.
- The debt reason fires only when the inventory's own
  `engines.dead_code.files_in_population` is **0**. Zero findings over a real
  population is health, and is never marked; an older inventory that predates
  that field claims nothing rather than asserting a gap it cannot demonstrate.

Counts still print underneath the marker — it adds context, never hides data.

## Grandfathering semantics

`grandfathered.json` (`grandfather/1`): one entry per baseline finding —
`{id, reason: "pre-adoption baseline", owner, expiry, created_at,
source_engine}` — the JUSTIFIED-waiver shape (`reason`/`owner`/`expiry`),
scoped to finding IDs (`DEBT-` ids from the inventory; `<gate>:<kind>:<id>`
keys for gate findings beyond it). Default expiry: **+90 days** (`--expiry` /
`--expiry-days`). The per-entry `created_at` (chief-wiggum#216 F8) is what
lets `plan_from_debt.py verify` demand that a remediation-epic waiver
POSTDATE the plan — entries without timestamps (pre-#216 files) never waive
a ticketed id.

- **Expiry = visible pressure, not amnesty.** Grandfathered findings stay
  **in** the inventory — `debt_inventory` marks them (`"grandfathered": true`,
  `"grandfather_expiry"`, `"grandfather_expired"`) and every surfacing layer
  labels them: the code-metrics debt section, the slop-gate debt block,
  `code_query orient` facts, and `/status`. An expired grandfather surfaces as
  **EXPIRED grandfather**, prominently, everywhere. The stored
  `grandfather_expired` flag is a **build-time snapshot**; every renderer
  recomputes expired-ness **live** from `grandfather_expiry` vs today
  (`chief_wiggum.grandfather.expired_live`), so an inventory built before the
  expiry date still shows EXPIRED once it passes.
- **The blocking gates read the file.** `check_traceability` and
  `check_single_writer` resolve `<meta root>/adoption/grandfathered.json` for
  their `--source` target (override: `--grandfather PATH`), mirroring the
  JUSTIFIED-waiver mechanics: a finding whose key matches a **non-expired**
  entry is reported under a *Grandfathered* section (counts + JSON field) and
  does **not** count toward the blocking exit under `--gate`; an **expired**
  entry does not waive — the finding blocks again, labeled **EXPIRED
  grandfather**. Keys are exactly what `adopt.py` writes (documented in
  `chief_wiggum.grandfather`): `check_traceability:uncovered:<ID>` /
  `check_traceability:untested:<ID>` and
  `check_single_writer:<INV-id>:<field>:<file>`.
- **Only NEW findings are gate-eligible.** For debt, nothing blocks today (the
  inventory is report-only per [gate-rollout.md](gate-rollout.md)), so the
  honoring is the labeling plus #216's consumption: when debt-driven
  back-pressure arrives, a grandfathered (unexpired) finding is non-blocking
  by construction, and a finding absent from the grandfather file is new and
  gate-eligible. An **expired** grandfather is no longer a waiver — re-triage
  or remediate.
- **Debt-id inheritance nuance.** `DEBT-` ids are content-anchored
  (`engine + normalized path + anchor`, never a line number): a finding that
  is fixed and later **reintroduced with the same anchor in the same file**
  (e.g. the identical TODO text, the same dead symbol name) reuses the old id
  and therefore **inherits its grandfather entry**. The waiver follows the
  finding's *identity*, not its occurrence — a reintroduction that matters
  should be caught in review, and a changed anchor (different text/symbol)
  mints a fresh, gate-eligible id.
- **The adoption artifacts are goalposts.** `ratchet.py protected` parks any
  worker diff touching `docs/adoption/*.json` (embedded mode) — a worker must
  not flip the brownfield switch or amnesty its own findings; a sidecar
  election keeps the files outside the tree entirely.
- An unparseable expiry counts as **expired**, never a silent pass (same
  posture as the JUSTIFIED-waiver `is_expired`).

## Contract inference is deferred

Recorded here per ticket-every-deferral (chief-wiggum#215):

- **Trigger**: the first cross-team breakage at a domain seam on an adopted
  repo, **or** an explicit operator request.
- **When it fires**: start at **domain-boundary contracts** — what our scope
  exposes to / consumes from the rest of the repo: the smallest,
  highest-value, already-implicitly-agreed surface. Do **not** attempt
  whole-repo inference; inferred contracts can canonize existing bugs as
  intended behavior.
- Until then, contracts arrive **organically**: the first `/architect` run
  for a real epic authors contracts for the code that epic touches; coverage
  grows epic by epic. Post-adopt, gates over the not-yet-contracted portions
  report `inapplicable`/`unscanned` per #213 — never green.

## Brownfield-mode consequences

The record flips repo-wide discipline (consumed by #216):

- `/architect`: not a new product; no DST stamping (above).
- `/implement`/`/implement-wave`: scope-discipline mode for every ticket —
  declared touch plans, out-of-pathset flagging (`ratchet.py pathset`),
  found-≠-fixed filing, no collateral edits.
- Gates: verdicts per the survey — a gate that cannot apply says
  `inapplicable`/`report-only` with a reason; nothing passes vacuously.

## What proves it

`tests/test_adopt.py` runs the sequence against tmp brownfield targets
(sidecar mode asserts the target tree never gains a CW file, including the
baseline's junit report), and the #215 acceptance run exercised the real
sequence against a real repo in sidecar mode with an isolated
`CHIEF_WIGGUM_USER_DIR`.

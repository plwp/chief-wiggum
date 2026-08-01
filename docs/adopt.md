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
   re-pointed outside the target tree and pytest cache/bytecode writes are
   suppressed — in sidecar mode the baseline run leaves the target byte-clean.
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
exists to keep clean. Step-by-step adoptions should elect first for the same
reason.

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

## Grandfathering semantics

`grandfathered.json` (`grandfather/1`): one entry per baseline finding —
`{id, reason: "pre-adoption baseline", owner, expiry, source_engine}` — the
JUSTIFIED-waiver shape (`reason`/`owner`/`expiry`), scoped to finding IDs
(`DEBT-` ids from the inventory; `<gate>:<kind>:<id>` keys for gate findings
beyond it). Default expiry: **+90 days** (`--expiry` / `--expiry-days`).

- **Expiry = visible pressure, not amnesty.** Grandfathered findings stay
  **in** the inventory — `debt_inventory` marks them (`"grandfathered": true`,
  `"grandfather_expiry"`, `"grandfather_expired"`) and every surfacing layer
  labels them: the code-metrics debt section, the slop-gate debt block,
  `code_query orient` facts, and `/status`. An expired grandfather surfaces as
  **EXPIRED grandfather**, prominently, everywhere.
- **Only NEW findings are gate-eligible.** Nothing blocks on debt today (the
  inventory is report-only per [gate-rollout.md](gate-rollout.md)), so the
  honoring is the labeling plus #216's consumption: when debt-driven
  back-pressure arrives, a grandfathered (unexpired) finding is non-blocking
  by construction, and a finding absent from the grandfather file is new and
  gate-eligible. An **expired** grandfather is no longer a waiver — re-triage
  or remediate.
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

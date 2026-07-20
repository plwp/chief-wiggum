# Gate validation: proving a gate deserves to block

`docs/gate-rollout.md` established the rule that a new gate ships report-only
and is "validated on a real, already-shipped repo before it is wired as a
blocker." That rule was prose — a human judgment call about "an acceptable
false-positive rate." This doc makes it a **protocol**: a per-gate record with
a fixed schema, produced by actually running seeded defects and clean corpora,
that `/close-epic` checks mechanically before it will let a gate block.

This is deliberately a **manual/scripted protocol**, not an automated
"designer" that invents seeds for you. Building that generator is deferred
until the gate count makes hand-authoring seeds a real burden (cost-lens
trigger: revisit past 5 gates). Below 5 gates, a human writing 3-6 seeds per
gate is cheaper than a meta-tool to write them.

## Why this exists

Two things a gate ledger entry and a `--help` string cannot show:

1. **Does the gate actually fire on the defect it claims to catch?** A gate
   that has never been run against an injected instance of its own claim is
   an assertion, not a proof.
2. **Does the gate's "clean" verdict mean anything?** A gate that finds
   nothing because it never looked (a broken glob, an empty file list, a
   silently-skipped scan) is indistinguishable from a gate that looked
   everywhere and found nothing — unless the record also shows *what* it
   exercised.

Live telemetry (`factory_log.py`'s `gate`/`escape` events, `/reflect`'s
recall numbers) tells you how a gate performs **after** it ships. This
protocol is the **pre-flight** check: proving the gate deserves live traffic
in blocking mode at all.

## The record: `docs/quality/validation/<gate>.json`

One JSON record per gate, at `docs/quality/validation/<gate>.json` — sibling to
the ratchet's own state (`docs/quality/ratchet-journal.jsonl` corroborates the
record; see "Recording results"). The record lives **in the repo that ships the
gate**: CW's own gate suite (`check_single_writer`, `check_traceability`, ...)
carries its records in the chief-wiggum repo; a target repo hosting gates of
its own keeps theirs at the same relative path. Schema:
`templates/gate-validation-record-schema.json`.

```json
{
  "gate": "check_single_writer",
  "protocol_version": "1",
  "scanner_version": "<hash from --scanner-version>",
  "telemetry_dependent": false,
  "concurrency_applicable": false,
  "concurrency_note": "static analysis over checked-in source scans deterministically; no runtime concurrency channel to evade",
  "authority_boundary": {
    "proves": "every writer of a controlled field outside sanctioned_writers is reported as a violation, over the file extensions/paths the scanner walks",
    "artifact": "a git worktree copy of the target repo's tracked source tree",
    "assumptions": [
      "the field's controlling invariant carries well-formed controls_field + sanctioned_writers metadata",
      "vendor/node_modules/dist/build subtrees are out of scope by design (SKIP_PARTS)",
      "a writer that never appears as a literal quoted/assigned token (fully dynamic field construction) is not detectable by a regex scanner"
    ]
  },
  "seeded_defect_trials": [
    {
      "seed_id": "sw-direct-01",
      "seed_class": "direct",
      "seed_version": "1",
      "repo": "tests/fixtures/gate_validation/single_writer_clean",
      "sha": "sha256:<content digest of the corpus — check_gate_validation.corpus_digest>",
      "injected": "re-add ChangePlan: an unsanctioned second writer of provider.stripe_plan",
      "expected": "fire",
      "result": "fired",
      "passed": true
    }
  ],
  "clean_corpus_runs": [
    {
      "repo": "tests/fixtures/gate_validation/single_writer_clean",
      "sha": "sha256:<content digest of the corpus — check_gate_validation.corpus_digest>",
      "findings": 0,
      "coverage": {"writers_found": 4, "invariants_checked": 4},
      "passed": true
    }
  ],
  "status": "passed",
  "validated_at": "2026-07-19T00:00:00Z",
  "validated_by": "chief-wiggum#168",
  "ratchet_record_id": "rec-00001"
}
```

### Seeded-defect trials

Each seed is one row: inject the defect into a worktree copy of a real,
already-shipped repo, run the gate, and record whether it fired. A seed's
`seed_class` groups it into one of four buckets:

- **`direct`** — the textbook instance of the claim the gate makes (at least
  one direct trial is required). This is the sanity check: if the gate can't
  catch the obvious case, nothing else matters.
- **`evasion-*`** — seed classes derived mechanically from the gate's claim
  inventory that probe how the gate could be dodged, not just triggered.
  Every gate's seed set MUST include an attempt at each of:
  - `evasion-omission` — the defect exists but is not phrased/placed the way
    the gate's "obvious" detector expects (e.g. hidden inside a nested/
    anonymous scope with no direct enclosing symbol).
  - `evasion-config-indirection` — the defect is reached through a layer of
    indirection (a shared helper/wrapper, a config-driven dispatch) instead
    of the literal shape the gate greps for.
  - `evasion-sampling-gap` — the defect lives in a part of the artifact the
    gate's own scope rules exclude or under-sample (an excluded directory, an
    unscanned file extension, a rarely-exercised code path).
  - `evasion-concurrency` — **where applicable**: the defect only manifests
    under concurrent/racing writers. A gate whose artifact has no concurrent
    dimension (e.g. a single-pass static scan of checked-in source) marks
    `concurrency_applicable: false` with a `concurrency_note` justifying why,
    instead of omitting the seed silently.
- **`instrumentation-deleted`** — **mandatory for any telemetry-dependent
  gate** (`telemetry_dependent: true`): the seed removes/disables the
  instrumentation the gate reads (a log line, a metric, an emitted event) and
  proves the gate visibly reports "no signal" rather than a false "clean."
  Gates with no telemetry dependency (static source/doc scanners) set
  `telemetry_dependent: false` and omit this seed.

**A seed's `expected` outcome is not always `fire`.** A seed that targets a
scanner's *documented* scope boundary (e.g. vendor/ exclusion, an unscanned
file extension) legitimately expects `"no-fire"` — the point of that trial is
to prove the boundary is exactly what the authority-boundary statement
claims, not to pretend the gate is omniscient. `passed` is `result ==
expected`, not `result == "fired"`. A trial whose IN-SCOPE claim silently
fails to fire (`expected: "fire"`, `result: "not-fired"`) is a genuine defect
in the gate and must set the record's overall `status` to `"failed"`.

Seeds are **versioned separately from the gate's own code**
(`seed_version`, independent of `scanner_version`) so a gate implementation
can't quietly "pass" by having its seed suite edited in lockstep with a
weakened implementation — the same overfitting concern the ratchet's hashed
contract blocks addresses for contracts. Bumping `scanner_version` alone does
not revalidate a gate; the seeded trials must be re-run and the record
re-authored.

### Clean-corpus runs

At least one run against a known-good state (`repo` + `sha`) with **zero
findings** is required — but "no findings" alone is not evidence, it's silence.
Each clean-corpus entry MUST carry `coverage`: concrete counts proving the gate
actually exercised the channels it polices (files scanned, invariants checked,
writers/annotations found, etc.). A clean run with `coverage: {}` or all-zero
coverage on a corpus known to contain the artifact class the gate polices is
not a passing clean-corpus run — it's an unexercised no-op wearing a green
checkmark.

### Authority boundary

The record states, in plain language, exactly what passing validation buys:
`proves` (the specific claim demonstrated), `artifact` (what kind of thing was
scanned — a git worktree of tracked source, a running app, an epic doc tree),
and `assumptions` (the boundary conditions under which the proof holds — scope
exclusions, metadata prerequisites, known blind spots). **Promoting a gate to
`--gate` never grants it authority beyond this statement.** A gate proven to
catch stripe-plan writers in Go/Mongo shipped code says nothing about a
Postgres-only repo unless the assumptions say so.

## Recording results

Results are appended to the **existing ratchet journal** — its hash chain is
already the tamper-evident provenance mechanism (`docs/ratchet.md`); this
protocol reuses it rather than inventing a second one. **No signing or DSSE**
— that is explicitly deferred until an external party requires attestations
beyond an append-only hash chain inside the repo's own trust boundary.

```bash
python3 "$CW_HOME/scripts/ratchet.py" record --repo "$REPO_HOSTING_THE_GATE" \
  --event gate-validation --ref check_single_writer --merged \
  --notes "seeded-defect + clean-corpus trials passed; see docs/quality/validation/check_single_writer.json"
```

The journal lives beside the validation dir
(`docs/quality/ratchet-journal.jsonl`, sibling of `docs/quality/validation/`).
The record's `ratchet_record_id` field names the resulting journal entry
(`rec-NNNNN`), and `check_gate_validation.py` **verifies** the cross-reference:
the id must exist in a hash-chain-verified journal, in a `gate-validation`
event whose `ref` names this gate. A record without that corroboration — or a
journal whose chain doesn't verify — has no provenance and fails.

## The gate-of-gates: `scripts/check_gate_validation.py`

```bash
python3 "$CW_HOME/scripts/check_gate_validation.py" check_single_writer \
  --validation-dir "$CW_HOME/docs/quality/validation"
```

Report-only by default (prints the record's status and exits 0). `--gate`
makes it block:

```bash
python3 "$CW_HOME/scripts/check_gate_validation.py" check_single_writer \
  --validation-dir "$CW_HOME/docs/quality/validation" --gate
```

Exits 1 when:
- no `<gate>.json` record exists at all,
- the record is malformed against `templates/gate-validation-record-schema.json`,
- **provenance fails** — the record's `gate` field doesn't name the gate being
  checked (a copied record grants nothing); its `scanner_version` differs from
  the gate's live `--scanner-version` output (stale — re-run the trials); or
  its `ratchet_record_id` isn't corroborated by the chain-verified ratchet
  journal beside the validation dir (see "Recording results"),
- `status != "passed"`,
- any seeded-defect trial fails **as derived**: a trial only counts when
  `result` matches `expected` (`fire`→`fired`, `no-fire`→`not-fired`) AND its
  `passed` flag agrees — a forged `passed: true` with a contradicting `result`
  fails,
- any clean-corpus run fails as derived: it needs `passed: true` AND
  `findings: 0` AND non-empty, not-all-zero `coverage`,
- a mandatory seed class lacks a genuinely-passing trial (`direct` always —
  the sanity check; `evasion-omission`, `evasion-config-indirection`,
  `evasion-sampling-gap` always; `evasion-concurrency` unless
  `concurrency_applicable: false`; `instrumentation-deleted` when
  `telemetry_dependent: true`).

**`/close-epic` refuses `--gate` for any checker lacking a passing validation
record.** Before Step 2d (traceability coverage) and Step 2e (single-writer
coverage) invoke their checkers with `--gate coverage`, `/close-epic` runs
`check_gate_validation.py --gate` for that checker first. A missing/failing
record does not silently skip the gate — it downgrades **that invocation** to
report-only for this close and surfaces a blocking finding in the close report
directing the operator to complete the protocol (or explicitly accept the risk
with the human checkpoint). This is the same "report-only until proven"
posture as `docs/gate-rollout.md`, just enforced mechanically instead of by
convention.

As of #184 every blocking-capable gate carries a record under this protocol:
`ratchet.py`, `saas_gate.py`, `ci_scaffold.py`, `quality_slop_gate.py`, and
`check_architecture.py` (the fifth gate, per ADR-fh-06 — one seed per entry in
its frozen `CHECKS` inventory) join `check_single_writer.py` and
`check_traceability.py`. Each ships a hash-derived `--scanner-version`
(`chief_wiggum.hashing.scanner_version` — INV-fh-005), and validity is always
read via `check_gate_validation.py <gate> --format json` reporting
`passing == true`, never the default exit code (INV-fh-003). The two gates with
non-deterministic live targets pin **fixture harnesses** instead (CTR-fh-044):
`saas_gate` a scripted local HTTP fixture server
(`tests/fixtures/gate_validation/saas_gate_clean/`), `quality_slop_gate`
recorded band files (`tests/fixtures/gate_validation/quality_slop_gate_clean/`)
fed to its pure verdict functions — a record validated against a prod URL or a
live AI band could never be re-verified.

## Auto-demotion: a blocking gate's record going stale (chief-wiggum#198)

The demotion rule below fires on a **production escape**. A gate can also
lose blocking authority with no escape at all — its validation record simply
rotted while the gate was still wired `--gate`: a scanner edit bumped
`--scanner-version` out from under it, the ratchet journal's hash chain broke,
or the record was deleted/regressed to `status != "passed"`. `INV-fh-003` ("no
blocking without a passing record") already made `check_gate_validation`
report `passing == false` in this case; #198 closed the remaining gap — the
system must not just report `false`, it must actively **track and surface**
that a gate that WAS blocking no longer is.

`check_gate_validation.py --wire` opts a gate into blocking-authority tracking
(**only** when its record currently passes — a non-passing `--wire` can never
reach `blocking`, INV-fh-003) via a persisted `<gate>.authority.json` sidecar
beside the validation record. Every ordinary check thereafter re-derives the
authority transition (`compute_transition`/`check_and_transition`, mirroring
`docs/epics/epic-factory-hardening/models/state-machines.json`'s Gate
Blocking-Authority Lifecycle):

- **Blocking + record goes stale or missing/invalid** → auto-demotes to
  `demoted` (fail-to-report-only, ADR-fh-04), emits the GENERIC `DEMOTION`
  event via `factory_log.emit_stale_demotion(gate, reason,
  previous_authority="blocking")` with `reason` `"stale"` (scanner_version/
  journal-chain drift, otherwise clean) or `"record_missing"` (missing/
  schema-invalid/forged/failed) — never `emit_demotion`, which requires a
  `seed_class` this path never has (nothing escaped in production; the record
  itself just went bad).
- **Merely `validated` (not currently wired) + record goes stale or invalid** →
  downgrades to `report_only` — no demotion event, since nothing was
  blocking.
- **Recovery**: re-authoring and re-journaling a `demoted` (or downgraded)
  record restores `validated` once `passing == true` again — never straight
  back to `blocking` (the model's `invalid_transitions` explicitly forbid
  `demoted -> blocking`). A `--wire` on a still-`demoted` gate resolves to
  `validated` (the re-derivation half); a **second** explicit `--wire` then
  promotes it to `blocking`.

`--wire`/`--unwire` obey the model's legal-vs-invalid transitions strictly, so
the sidecar can never record blocking authority the record doesn't currently
earn:

- A **non-passing `--wire`** never yields or persists `blocking` — it falls
  through to the natural lifecycle (demoting a stale/missing-while-blocking
  gate and emitting the DEMOTION, or downgrading otherwise) and reports the
  refusal.
- **`--unwire`** is the clean voluntary edge (`blocking -> validated`) ONLY
  when the record still passes; un-wiring a gate whose record has ALSO gone
  bad does **not** mask the demotion — it still goes `blocking -> demoted` and
  emits.

The sidecar is a **corroborated** trust record, not a bare file an attacker
can drop to forge authority (the exact class of forgeable-trust bug this epic
exists to prevent). `read_authority` trusts any real authority claim only when
the sidecar's `gate` field matches, its `ratchet_record_id` is a
chain-verified `gate-validation` journal entry for the gate, and — when a live
record exists — that rid matches the record's. A sidecar that fails any check
(forged `blocking` with no journaled rid, an rid contradicting a re-authored
record, a tampered/edited file) is treated as `unknown`/untrusted, so it can
neither assert authority nor manufacture a false demotion.

The actual enforcement point stays exactly where it already was: a workflow
only passes `--gate coverage` onward when `check_gate_validation.py --gate`
exits 0, so a demoted/downgraded gate is already refused blocking authority by
that existing guard (INV-fh-003). `check_and_transition`'s job is detection,
telemetry, and bookkeeping — making the demotion visible and recording
`previous_authority` — not re-implementing the refusal.

```bash
python3 "$CW_HOME/scripts/check_gate_validation.py" ratchet \
  --validation-dir "$CW_HOME/docs/quality/validation" --wire   # first promotion
# ... later, after a scanner edit bumps ratchet's --scanner-version ...
python3 "$CW_HOME/scripts/check_gate_validation.py" ratchet \
  --validation-dir "$CW_HOME/docs/quality/validation" --format json
# {"passing": false, ..., "authority": {"previous_state": "blocking",
#  "new_state": "demoted", "demoted": true, "demotion_reason": "stale",
#  "previous_authority": "blocking", "instruction": "DEMOTE ratchet ..."}}
```

## Demotion: an escape a seed class should have caught

The live confusion matrix (`factory_log.py`'s `gate`/`escape` events,
`docs/factory-telemetry.md`) already measures recall — `caught / (caught +
escaped)`. This protocol adds a **demotion rule** on top of it: if a real,
production escape is logged against a gate (`missed_by`) **and** tagged with
the `seed_class` it resembles, and that gate's validation record certifies it
passed a trial of exactly that seed class, the validation was **wrong about
production recall** — not a one-off miss to shrug off.

```bash
python3 "$CW_HOME/scripts/factory_log.py" bug --repo acme/app \
  --summary "reset endpoint leaks account existence via timing" --severity high \
  --missed-by check_single_writer --seed-class evasion-omission \
  --found-in close-epic-review --ticket 42
```

When `--seed-class` matches a class the named gate's validation record
(`--validation-dir`, default: chief-wiggum's own `docs/quality/validation/`)
certified as **caught** — a trial with `expected: "fire"`, `result: "fired"`,
`passed: true` — `factory_log.py` prints a **DEMOTION** instruction to stderr,
writes the `seed_class` into the escape event, and emits a `demotion`
telemetry event. A passing `expected: "no-fire"` trial certifies a documented
NON-coverage boundary (e.g. a sampling-gap seed proving `vendor/` is out of
scope); an escape through that boundary is consistent with the record's
authority statement and does **not** demote:

1. **Revert the gate to report-only** — drop `--gate`/`--gate coverage` from
   its workflow wiring (`/architect`, `/close-epic`) until re-validated.
2. **File a tracking ticket** to re-derive and re-run that seed class — the
   seed as authored did not represent the real evasion technique that
   actually shipped, so the seed itself (not just the gate) needs revision.

This mirrors "quality ratchets, never slides" (CLAUDE.md): a gate's blocking
authority is a high-water mark too, and a demonstrated production miss of a
*validated* seed class is exactly the kind of regression that must move the
mark back down, not get logged and forgotten.

## Retroactive validation

`check_single_writer.py` and `check_traceability.py` predate this doctrine —
they were wired as blockers under the older, prose-only `gate-rollout.md` rule.
#168 completed retroactive validation records for both (seeded-defect trials
including all mandatory evasion classes, clean-corpus runs with coverage
evidence, authority boundary statements) against the fixture corpora under
`tests/fixtures/gate_validation/`. The records ship at
`docs/quality/validation/check_single_writer.json` and
`docs/quality/validation/check_traceability.json`, journaled as `rec-00001` /
`rec-00002` in `docs/quality/ratchet-journal.jsonl`; each trial's `sha` is a
content digest of its fixture corpus (`check_gate_validation.corpus_digest`).
`tests/test_gate_validation_retroactive.py` re-executes every trial by
`seed_id` and re-derives the digests and scanner versions, so any drift
between the shipped records and the gates' live behavior — a renamed trial, a
changed corpus, a changed scanner — fails the suite.

# Fail-closed gates: "it failed to run" must never render as "it passed"

A gate whose *empty findings* path is also its *I crashed / I parsed nothing / my
scanner covered 0 files* path passes exactly when it is most broken. A green
result is then indistinguishable from a broken instrument, and the factory's
entire quality story rests on measurements that may never have been taken.

This page is the audit called for by chief-wiggum#289: every gate/checker CLI ×
its failure paths × current behaviour. It is the companion to
[gate-rollout.md](gate-rollout.md) (which governs when a gate may block at all)
and [gate-validation.md](gate-validation.md) (which governs proving a gate
catches what it claims).

## The standard outcome model

Every gate reports exactly one of four states, in text AND in JSON:

| Outcome | Meaning |
| --- | --- |
| `pass` | The measurement ran, and found nothing. |
| `findings` | The measurement ran, and found something. |
| `inapplicable` | The **preconditions for measurement are absent** — no artifacts at all, zero-byte, whitespace-only, nothing declared. Honest absence. |
| `error` | Inputs **exist** and the instrument saw none of them. A broken instrument. Never a pass. |

The `inapplicable` / `error` split is the whole point. Getting it backwards in
either direction is a failure:

- calling a genuinely-absent input `error` makes the gate noisy, and the
  operator learns to `--force` past it — eroding trust in *every* gate
  ([gate-rollout.md](gate-rollout.md));
- calling an unseen-but-present input `inapplicable` is the bug this page
  exists to kill.

### The reference implementation

`scripts/check_traceability.py` is the template (chief-wiggum#281). Copy its
idiom rather than inventing a second one:

1. an `applicability` field on the report — `applicable` / `inapplicable` / `error`;
2. a **derived** `outcome` property (`pass | findings | inapplicable | error`).
   Derived, never stored: "failed to run" and "found nothing" must not be able
   to drift apart into two fields that disagree;
3. a `measured` denominator in `to_dict()` — *what was actually scanned*, printed
   on every run, green or not, so a zero is visible without reading the JSON;
4. the report's own booleans (`soundness_ok`/`coverage_ok`/…) follow
   `applicability`, so the JSON can never say `outcome: error` beside
   `soundness_ok: true`;
5. an explicit `main()` rule: `if args.gate and report.applicability == "error": return 1`,
   with a loud banner printed in **every** mode (report-only still says
   `OUTCOME: error — scanner saw 0 of N inputs`).

Exit codes stay in the uniform `0` / `1` / `2` contract every gate shares. The
state is carried by the banner and the JSON, not by a new exit code.

## Audit table

Status is as of chief-wiggum#289. "Fixed" cites the PR that closed it.

| Gate / checker | Blocking surface | No inputs at all | Inputs exist, scanner saw none | Subprocess / tool failure | Swallowed parse failure | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `check_traceability.py` | `--gate soundness\|coverage` | `inapplicable` + banner, exit 0 | **`error`, exit 1** — `unparsed_artifacts`, `malformed_ids`; `measured.id_bearing_artifacts` | n/a (pure Python) | `unscanned` files named with paths (report-only by binding decision) | **Fixed** — #281 (PR #297), #282 (PR #299). The template. |
| `ratchet.py` — contract hashes | blocks by default | `inapplicable` | **`error`, exit 1** via `contract_measurement` (hard tier) | n/a | malformed IDs named | **Fixed** — #295 (PR #302) |
| `ratchet.py` — pass-set | blocks by default | `inapplicable` (`--no-tests`, no suites configured) | **`error`, exit 1** via `suite_measurement` (hard tier): a configured suite contributing 0 passing cases | dead command → 0 passing → `error`; junit report pre-cleared so a stale file cannot fabricate a pass count; unparseable report → `RatchetError` (exit 2), not an ElementTree traceback | zero-collection warning now fires whatever the exit code was | **Fixed** — #289 |
| `check_single_writer.py` | `--gate soundness\|coverage` | `inapplicable` + banner, exit 0 | **`error`, exit 1** — 0 source files read while invariants are declared; `measured.source_files_scanned` | `--changed-since` git failure → exit 2; nonexistent `--source` → exit 2 | unparseable `state-machines.json`/`invariants.md` → `error` (was: collapsed to `inapplicable`); `unscanned` files named (report-only, #282) | **Fixed** — #289 |
| `check_unresolved.py` | blocks by default (no `--gate` flag) | `inapplicable` + banner, exit 0 | n/a — every collected file is either read or listed as unparsed | n/a | **`error`, exit 1** — unreadable/unparseable artifacts named; telemetry no longer logs a broken run as `pass` | **Fixed** — #289 |
| `verify_transitions.py` | `--gate` (errors only) | `inapplicable` + banner, exit 0 | **`error`** — 0 source files scanned while transitions are declared (the Go-only scanner pointed at a non-Go repo); `measured.source_files_scanned` | absent/unloadable model file → `error` (was: warn-and-skip); malformed transition → `ModelLoadError`, not a `KeyError` traceback | unreadable source files named | **Fixed** — #289. `--gate` blocks on `error` only; honest missing/undocumented transitions stay report-only, so no caller newly blocks. |
| `check_gate_validation.py` | `--gate` | absent record → `passing: false`, exit 1 | n/a | **`_live_scanner_version` returns `None` when the gate script is absent, raises, or exits non-zero — the staleness check then silently does not run.** Byte-identical to a clean pass, and locked in by a test | `jsonschema` unimportable → `_schema_errors` returns `[]`; a corrupt record is misreported as "no record found" | **Partly sound.** Staleness itself IS caught and blocks (see below); the *probe* for it fails open. **Open** |
| `check_architecture.py` | `--gate` | **model file absent → `ok: true`, exit 0 even under `--gate`** (deliberate, `test_cli_absent_model_exits_zero_even_under_gate`) | 1 node / 0 edges: six of eight checks measure nothing, `ok: true` | `--system-contracts` unloadable → a `not_checked` entry, `ok` untouched, exit 0 | n/a | **Open** |
| `check_patterns.py` | blocks by default | `inapplicable` + banner, exit 0 (an empty registry with zero manifest-bearing `patterns/` dirs) | an ERROR **finding**, exit 1 — a registry<->`patterns/` bijection: any direct child of `patterns/` carrying its own `manifest.json` must be listed in `patterns[]` or `candidates[]`; `measured.pattern_dirs_scanned` (the scan itself did not fail — a real, present manifest was found unregistered, `applicability: applicable`) | n/a | top-level JSON array → `applicability: error`, structured finding, exit 1 (was: uncaught `AttributeError`) | **Fixed** — #289 |
| `ux_gate.py` | blocks by default | non-frontend ticket → `inapplicable`, exit 0 | **`error`, exit 1** — a named `--ui-spec` that is not there on a FRONTEND ticket, or a `--design-dir` that is not a directory while the ui-spec declares a design contract. Scoped to frontend tickets deliberately: `/implement` passes both flags unconditionally and a backend-only epic legitimately has neither | absent `--changed-files` → exit 2 (was an uncaught traceback that, under `/implement`'s `>` redirect, left an EMPTY manifest reading as "nothing to do") | malformed ui-spec → exit 1 | **Fixed** — #289. `--have-playwright`/`--have-browser-use` remain operator-asserted booleans, never probed — a known limitation, not a fail-open of the scan itself |
| `saas_gate.py` | `--gate` | empty/nonexistent `--repo` → 5 SKIPPED, 0 PASS, `ok: true`, exit 0 | `--repo` is never validated; `stack` is decorative — no check depends on it | **network failure → the whole header/CSRF battery becomes one `SKIPPED`, and `SKIPPED` can never fail the gate**; only the health check turns unreachability into `FAIL` | typo'd `--log-sample` → "no log sample provided" | **Open** |
| `ci_scaffold.py` | `--gate` | absence IS the finding: `ci_present: false`, exit 1 (correct) | **`detect_ci` is a filename test — `touch .github/workflows/ci.yml` defeats the gate**; a 0-byte or non-YAML workflow reports `ci_present: true` | n/a | n/a | **Open** |
| `quality_slop_gate.py` | `--gate` | **a repo with zero source files reports "0.0% duplication (beats the pre-AI human baseline)" — the healthiest band available** | same mechanism for a wrong ignore glob or an unparsed language | **an engine crash is rendered as "skipped"**: `duplication.py` builds `status: "crashed"`, the gate branches only on the legacy `"skipped"` key. `survival.py` never checks `returncode` and never clears a stale `survival.json`, so a crashed rerun parses the previous run's numbers as fresh | `except … return None` (report-only by design) | **Open** |
| `run_verification.py` | no `--gate`; `ok` drives exit | zero steps detected → `ok: false`, exit 1 (fail-closed) | n/a | runner exception → `ok: false`, exit 1 (fail-closed) | n/a | **Sound**, with one caveat: exit 1 is identical to "tests failed", `--repo` existence is unvalidated, and `--dry-run` always returns 0 |
| `code_query.py` | not a gate (locator) | `unscanned` envelope, `applicability: "inapplicable"` — a machine field, not just a summary-string prefix | `applicability: "error"` — a queried path/handle exists but couldn't be decoded; `governing_facts_for_file`/`cmd_show` now read through `chief_wiggum.textio.read_text_safe` (#282) instead of an unguarded `read_text()` that crashed with an uncaught `UnicodeDecodeError` | n/a | `_locate_definitions` still drops unreadable epic docs with no warning (matches `check_traceability.py`'s own `extract_defined_ids`/`scan_epic_annotations` epic-doc-read convention — out of scope here) | **Fixed** — #289 |

## The stale-validation-record question (#289's own comment)

> A stale/absent gate-validation record must surface as `error` (loud, and
> blocking under `--gate`), never as a quiet demotion to report-only.

Half of this is already sound, and the sound half should not be churned:

- **Detection blocks.** A `scanner_version` mismatch appends to
  `provenance_errors`, drops `passing` to `false`, and `--gate` exits 1. An
  absent record does the same via `record_found: false`. Both are covered by
  tests (`test_stale_scanner_version_fails`,
  `test_record_omitting_scanner_version_fails_when_live_available`,
  `test_failure_kind_classifies_stale_vs_invalid`).
- **A wired gate's demotion is journaled and loud** — `authority.demoted: true`,
  `demotion_reason: "stale"`, a `factory_log` demotion event, and a stderr
  `DEMOTION` banner.

Two gaps remain, both real:

1. **The staleness probe itself fails open.** `_live_scanner_version` returns
   `None` — skipping the comparison entirely — when the gate script is absent,
   when `subprocess.run` raises, **and when the probe exits non-zero**. That is
   byte-identical to a clean pass, and `test_gate_without_scanner_version_support_skips_the_check`
   asserts it. There is also no `--scripts-dir` flag, so for a *target repo's*
   own gate scripts the live version is never found and staleness is silently
   unchecked. This is the same defect class one level up: the instrument that
   detects broken instruments is itself fail-open.
2. **The consumer converts a blocking exit into a silent skip.** `/close-epic`
   Step 2c2's contract is: if `check_gate_validation --gate` exits non-zero,
   *drop the corresponding blocking flag and report a finding instead*. That is
   a deliberate design (a gate without a valid record has no authority to
   block), but it means a stale record's practical effect is that the gate
   stops gating, and nothing goes red unless the agent writes the prose finding.

Both are enumerated as remaining work below rather than changed here: the fix
for (2) is a policy decision about `/close-epic`'s posture, not a bug fix.

## Remaining work

Tracked under chief-wiggum#289 until each is closed. Ordered by severity of
the fail-open, worst first:

- [ ] `quality_slop_gate.py` — branch on `status: "crashed"` (not the legacy
      `"skipped"` key); check `returncode` and clear stale `survival.json`
      in `survival.py`; a zero-source corpus must be `inapplicable`/`error`,
      never the best band.
- [ ] `saas_gate.py` — validate `--repo`; a `SKIPPED` caused by an unreachable
      target must be `error`, not silence; report a measured denominator.
- [ ] `ci_scaffold.py` — parse the workflow, do not just stat its filename.
- [ ] `check_architecture.py` — an absent model under `--gate`, and an
      unloadable `--system-contracts`, must not exit 0.
- [ ] `check_gate_validation.py` — a `--scanner-version` probe that fails must
      be `error`, not a skipped check; add `--scripts-dir` so target-repo gates
      are checkable.
- [x] `check_patterns.py` — an empty registry is not "OK"; add a
      registry ↔ `patterns/` bijection check. **Done — #289.**
- [x] `code_query.py` — route file reads through `read_text_safe`; make
      `unscanned` a machine field, not a summary-string prefix. **Done — #289.**
- [ ] Per-gate `instrument-broken` seeded trials in each gate's
      `validation/<gate>.json` once the class becomes mandatory
      (see [gate-validation.md](gate-validation.md)).

## Rules for new gates

1. Report the four states, in text and JSON.
2. Print the denominator on every run, green or not.
3. `error` exits non-zero under `--gate`; report-only prints the outcome loudly.
4. Never swallow a subprocess's non-zero exit, OOM kill, or timeout into an
   empty finding list — propagate it as `error`.
5. Ship the broken-instrument test with the gate: artifacts present, scanner
   sees nothing ⇒ `error`, not `pass`/`inapplicable`.
6. A new blocking finding class needs a precision dry-run on real artifacts
   before it blocks ([gate-rollout.md](gate-rollout.md)).

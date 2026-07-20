# Traceability Matrix — Epic: Factory Hardening

Maps each ticket's acceptance criteria to the business rule, the contracts /
invariants that realize it, and the planned unit + integration tests. Machine-checked links
(`@cw-trace realizes`) live in `invariants.md` and `contracts.json`; this matrix
is the human-readable join.

Legend: IT-fh-* = integration test (see `integration-tests.md`); CTR/INV ids are
the realizing contracts/invariants.

**Updated at `/close-epic` (2026-07-20), all 6 tickets merged (#191–#196).**
`passing` = the named test(s) were re-run against the merged code by the closer
and pass. `covered` = implemented and exercised, but full verification was
partial in this environment (e.g. a `lizard`-dependent assertion skips here the
same way it does in CI — no `lizard` on `PATH` — or the AC is a workflow/manual-review
item, not a unit-testable one). `missing` = genuinely not implemented; see the
close-epic retrospective for the one row marked `missing` (IT-fh-06 → #198).
`check_traceability --gate coverage` initially failed on CTR-fh-043 /
CTR-fh-044 / INV-fh-005 (implemented + tested but never `@cw-trace`-linked);
fixed in the same close via #197 — annotation-only `guards`/`verifies` tags at
the genuinely-covering sites (`check_gate_validation.py` `passing` property and
`corpus_digest`, the five gates' `_scanner_version` functions, and the covering
tests). The coverage gate now passes and `docs/quality/trace-links.json` is
written.
Note: `scripts/traceability.py update` could not flip these rows mechanically —
every table in this file omits (or leaves blank) the numeric `--ticket` cell the
updater requires, so this pass edited the file by hand. Flagged as a minor
follow-up in the retrospective.

---

## #83 — run_review.py drops ticket comments (BR-fh-001)

| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |
|--------|---------------------|-----------|-----------------|----------|--------|
| #83 | Fold issue comments (≥ design refinements) into the assembled review prompt | test_review_pipeline.py | IT-fh-01, IT-fh-10 | — | passing |
| #83 | TWO labeled regions: "Accepted AC amendments (authoritative-on-conflict)" + "Discussion/context (non-authoritative)" — raw thread never labeled authoritative | test_review_pipeline.py | IT-fh-01 | — | passing |
| #83 | `from_dict` preserves `comments` (no silent drop) | test_review_pipeline.py | IT-fh-02 | — | passing |
| #83 | Upstream `ticket.json` writer serializes comments incl. `author_association` (absent key = failure) | test_review_pipeline.py | IT-fh-10 | — | passing |
| #83 | Amendments vs discussion authority (adversarial-safe); stored ACs never rewritten | test_review_pipeline.py | IT-fh-01 | — | passing |

## #134 — consult_ai per-provider token usage → emit_consult (BR-fh-002)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Each provider captures tokens_in/out + model id from real usage | CTR-fh-010, CTR-fh-013 | IT-fh-05 (per-adapter canned samples) | passing |
| Call `emit_consult(provider, model, tin, tout, repo=…, ticket=…)` after success | CTR-fh-013 | IT-fh-05 (emit spy asserts args, `--ticket` threaded) | passing |
| Resolve codex → billed model id (unpriced today); resolved `name` never a bare CLI alias | CTR-fh-013 | IT-fh-05 (resolved-but-unpriced → cost None; alias-name negative) | passing |
| Real captured usage sample per provider, incl. stderr-only and partial payloads | CTR-fh-010, CTR-fh-012, CTR-fh-015 | IT-fh-05 fixtures (stderr-only sample; partial → both tokens null, status 'partial') | passing |
| Cost derived only (consult events); null never fabricated; both-tokens-or-null | INV-fh-002, INV-fh-011, CTR-fh-014, CTR-fh-015 | IT-fh-05 (honest degradation matrix) | passing |

## #174 — architecture.json + check_architecture static checks (BR-fh-003)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Schema + checker + tests, fixtures per consistency rule | CTR-fh-020, CTR-fh-021, CTR-fh-022 | IT-fh-07 (+ per-rule fixtures) | passing |
| Voice-agent example checks clean | CTR-fh-023 | `test_check_architecture` clean model | passing |
| Seeded violations each caught (retired-node edge, unlabelled external, tier inversion, label-propagation, missing tier, authored crossing label) | CTR-fh-025, error_cases | IT-fh-07 per-rule fixtures | passing |
| Budget-tree/telemetry cross-refs to declared nodes | INV-fh-008 | IT-fh-07 (undeclared `ARC-` finding) | passing |
| Authority line printed; absent-model graceful ("not checked" ≠ "passed") | CTR-fh-023, CTR-fh-024 | unit: stdout contains authority line; absent → exit 0; IT-fh-09 | passing |
| Report-only default / `--gate` exit semantics (0 findings-report-only, 1 gated, 2 usage) | CTR-fh-023, error_cases | IT-fh-09 (exit-mode matrix) | passing |
| Crossing labels derived, not authored | INV-fh-006, CTR-fh-025 | IT-fh-07 (authored non-null crossing → finding) | passing |
| `--scanner-version` (hash-derived) — fifth #184 gate per ADR-fh-06 | CTR-fh-026, INV-fh-005 | unit: round-trip; IT-fh-04 (table row) | passing |
| Frozen `CHECKS =` inventory exposed for #184's record | CTR-fh-026, ADR-fh-06 | IT-fh-04 (one seed per entry) | passing |
| /architect authoring step + docs/system-layer.md | (workflow doc) | manual review | covered |

## #184 — extend gate-validation records + --scanner-version, FIVE gates (BR-fh-004)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Add hash-derived `--scanner-version` to ratchet/saas_gate/ci_scaffold/quality_slop + check_architecture (fifth, via #174) | CTR-fh-040, CTR-fh-041, CTR-fh-026, INV-fh-005 | unit: round-trip per gate; complete-dep-list test (mechanical chief_wiggum-imports check) | passing |
| Author + live-verify a record per gate; validity read via `--format json passing==true`, never default exit code | CTR-fh-043 | IT-fh-04 (table-driven over ALL FIVE records), IT-fh-09 | passing |
| Journaled via `ratchet record --event gate-validation` | CTR-fh-043 | `test_ratchet` journal corroboration | passing |
| **Fixture harnesses for saas_gate (recorded target) + quality_slop_gate (pinned band) — explicit AC, BLOCKER for those two records** | CTR-fh-044 | `test_saas_gate` / `test_quality_slop_gate` fixture-target runs | passing |
| Record for #174's check_architecture in same pass, AFTER CHECKS freezes | INV-fh-003, ADR-fh-06 | IT-fh-04 (one seed per `CHECKS` entry; early-record negative) | passing |
| Stale record auto-demotes when blocking; downgrades to report_only when not [^stale-gap] | INV-fh-003, INV-fh-005 | IT-fh-06 (blocking→stale→demoted; validated→stale→report_only) | passing |
| Single `DEFAULT_VALIDATION_DIR` (import from factory_log, not a second definition) | INV-fh-004 | `test_validation_dir_is_defined_once_and_imported` | passing |

[^stale-gap]: **Resolved by chief-wiggum#198.** Was never implemented at epic
close (`check_gate_validation.py`/`factory_log.py` carried no
`previous_authority`, no distinct `demoted` state, and no generic
`emit(DEMOTION, gate=gate, details='stale')` path — only a `passing: bool`
collapse, plus the unrelated escape-driven `demotion_check(missed_by,
seed_class)`; `IT-fh-06` had no test anywhere in `tests/`). #198 implemented the
decision the model already specified: `check_gate_validation.py` gained
`check_and_transition` (persisted `<gate>.authority.json` sidecar tracking
`authority`/`previous_authority`, `--wire`/`--unwire` CLI flags) and
`factory_log.emit_stale_demotion` (the generic `DEMOTION` event, `details=`
`'stale'` or `'record_missing'`, no `seed_class`). `IT-fh-06` is now covered —
see `integration-tests.md`'s Status line and `retrospective.md`'s addendum.

## #185 — code_query orient inferred-binding over-match (BR-fh-005)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| IDF-weight word overlap OR require entity+verb combination (stdlib, deterministic) | CTR-fh-050, CTR-fh-051, INV-fh-012 | unit: `test_code_query` — single-common-entity-word file yields NO inferred fact (the auth-provider negative) | passing |
| Inferred facts stay labeled `inferred`, ranked below `direct` via leading relation-tier `_rank_key` element | CTR-fh-052, INV-fh-012 | `test_code_query_golden` envelope ordering; IT-fh-03 case (d) | passing |
| Channel separation from the #187 hotspot tier (measured = exact membership only) | CTR-fh-053, INV-fh-007 | IT-fh-03 (lexically-hot-but-not-listed → no fact) | passing |
| Deterministic across runs/platforms | CTR-fh-051 | unit: repeated-run byte-identical envelope | passing |

## #187 — hotspot discovery as derived gnosis (BR-fh-006)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| hotspots.py + coupling REUSE (process.py, no second engine) + CLI; synthetic-outlier + determinism tests [^lizard-gap] | CTR-fh-030, CTR-fh-032, INV-fh-001 | IT-fh-08 (+ reimplementation negative: module imports process.analyze) | covered |
| `window_days` derived by hotspot_discovery from commit dates (churn.analyze has no window param) and recorded | CTR-fh-031 | IT-fh-08 (`--check` compares recorded vs re-derived window) | passing |
| orient surfaces hotspot facts; provenance carries generating sha; measured tier sorts last | CTR-fh-033, CTR-fh-034, CTR-fh-052 | IT-fh-03 (incl. case (d) direct-sorts-first) | passing |
| No stable IDs; never gates; `--check` staleness [^lizard-gap] | CTR-fh-031, CTR-fh-033, INV-fh-007 | IT-fh-08 (`--check` nonzero; generate never gates) | covered |
| /architect + /implement consume the report | (workflow doc) | manual review | covered |
| Run against chief-wiggum + one shipped repo, findings in PR | (report-only validation) | manual review — see PR #194 body (real runs against chief-wiggum HEAD `3edc067` and `dogeared-coach`, findings summarized inline) | covered |

[^lizard-gap]: `lizard` is not installed on `PATH` in this environment (nor in CI —
same as every other `lizard`-dependent numeric assertion in this repo, e.g.
`tests/test_quality_metrics.py`). The `lizard`-independent parts of these rows
(single-coupling-engine reuse, no-stable-id field, `--check`-missing-file) were
re-run and pass; the `lizard`-dependent determinism/outlier/staleness-after-advance
assertions were skipped, not failed, and were verified during ticket implementation
per PR #194's description (byte-identical real-repo runs against chief-wiggum and
dogeared-coach).

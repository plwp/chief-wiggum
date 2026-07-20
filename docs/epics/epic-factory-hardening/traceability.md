# Traceability Matrix — Epic: Factory Hardening

Maps each ticket's acceptance criteria to the business rule, the contracts /
invariants that realize it, and the planned unit + integration tests. Status is
`pending` for every row (architect stage — no code yet). Machine-checked links
(`@cw-trace realizes`) live in `invariants.md` and `contracts.json`; this matrix
is the human-readable join.

Legend: IT-fh-* = integration test (see `integration-tests.md`); CTR/INV ids are
the realizing contracts/invariants.

---

## #83 — run_review.py drops ticket comments (BR-fh-001)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Fold issue comments (≥ design refinements) into the assembled review prompt | CTR-fh-001, CTR-fh-003 | unit: `test_review_pipeline` prompt has `{{TICKET_COMMENTS}}` token; IT-fh-01 | pending |
| TWO labeled regions: "Accepted AC amendments (authoritative-on-conflict)" + "Discussion/context (non-authoritative)" — raw thread never labeled authoritative | CTR-fh-003, INV-fh-010 | unit: `test_review_pipeline` two-section split (amendment lands in region 1, discussion in region 2, both present even when one is empty); IT-fh-01 | pending |
| `from_dict` preserves `comments` (no silent drop) | CTR-fh-001, INV-fh-009 | unit + IT-fh-02 (dict & legacy-string round-trip) | pending |
| Upstream `ticket.json` writer serializes comments incl. `author_association` (absent key = failure) | CTR-fh-002 | IT-fh-02, IT-fh-10 (writer golden) | pending |
| Amendments vs discussion authority (adversarial-safe); stored ACs never rewritten | INV-fh-010, INV-fh-009 | IT-fh-01 (adversarial comment stays discussion; acceptance_criteria byte-identical) | pending |

## #134 — consult_ai per-provider token usage → emit_consult (BR-fh-002)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Each provider captures tokens_in/out + model id from real usage | CTR-fh-010, CTR-fh-013 | IT-fh-05 (per-adapter canned samples) | pending |
| Call `emit_consult(provider, model, tin, tout, repo=…, ticket=…)` after success | CTR-fh-013 | IT-fh-05 (emit spy asserts args, `--ticket` threaded) | pending |
| Resolve codex → billed model id (unpriced today); resolved `name` never a bare CLI alias | CTR-fh-013 | IT-fh-05 (resolved-but-unpriced → cost None; alias-name negative) | pending |
| Real captured usage sample per provider, incl. stderr-only and partial payloads | CTR-fh-010, CTR-fh-012, CTR-fh-015 | IT-fh-05 fixtures (stderr-only sample; partial → both tokens null, status 'partial') | pending |
| Cost derived only (consult events); null never fabricated; both-tokens-or-null | INV-fh-002, INV-fh-011, CTR-fh-014, CTR-fh-015 | IT-fh-05 (honest degradation matrix) | pending |

## #174 — architecture.json + check_architecture static checks (BR-fh-003)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Schema + checker + tests, fixtures per consistency rule | CTR-fh-020, CTR-fh-021, CTR-fh-022 | IT-fh-07 (+ per-rule fixtures) | pending |
| Voice-agent example checks clean | CTR-fh-023 | `test_check_architecture` clean model | pending |
| Seeded violations each caught (retired-node edge, unlabelled external, tier inversion, label-propagation, missing tier, authored crossing label) | CTR-fh-025, error_cases | IT-fh-07 per-rule fixtures | pending |
| Budget-tree/telemetry cross-refs to declared nodes | INV-fh-008 | IT-fh-07 (undeclared `ARC-` finding) | pending |
| Authority line printed; absent-model graceful ("not checked" ≠ "passed") | CTR-fh-023, CTR-fh-024 | unit: stdout contains authority line; absent → exit 0; IT-fh-09 | pending |
| Report-only default / `--gate` exit semantics (0 findings-report-only, 1 gated, 2 usage) | CTR-fh-023, error_cases | IT-fh-09 (exit-mode matrix) | pending |
| Crossing labels derived, not authored | INV-fh-006, CTR-fh-025 | IT-fh-07 (authored non-null crossing → finding) | pending |
| `--scanner-version` (hash-derived) — fifth #184 gate per ADR-fh-06 | CTR-fh-026, INV-fh-005 | unit: round-trip; IT-fh-04 (table row) | pending |
| Frozen `CHECKS =` inventory exposed for #184's record | CTR-fh-026, ADR-fh-06 | IT-fh-04 (one seed per entry) | pending |
| /architect authoring step + docs/system-layer.md | (workflow doc) | manual review | pending |

## #184 — extend gate-validation records + --scanner-version, FIVE gates (BR-fh-004)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| Add hash-derived `--scanner-version` to ratchet/saas_gate/ci_scaffold/quality_slop + check_architecture (fifth, via #174) | CTR-fh-040, CTR-fh-041, CTR-fh-026, INV-fh-005 | unit: round-trip per gate; complete-dep-list test (mechanical chief_wiggum-imports check) | pending |
| Author + live-verify a record per gate; validity read via `--format json passing==true`, never default exit code | CTR-fh-043 | IT-fh-04 (table-driven over ALL FIVE records), IT-fh-06, IT-fh-09 | pending |
| Journaled via `ratchet record --event gate-validation` | CTR-fh-043 | `test_ratchet` journal corroboration | pending |
| **Fixture harnesses for saas_gate (recorded target) + quality_slop_gate (pinned band) — explicit AC, BLOCKER for those two records** | CTR-fh-044 | `test_saas_gate` / `test_quality_slop_gate` fixture-target runs | pending |
| Record for #174's check_architecture in same pass, AFTER CHECKS freezes | INV-fh-003, ADR-fh-06 | IT-fh-04 (one seed per `CHECKS` entry; early-record negative) | pending |
| Stale record auto-demotes when blocking; downgrades to report_only when not | INV-fh-003, INV-fh-005 | IT-fh-06 (blocking→stale→demoted; validated→stale→report_only) | pending |
| Single `DEFAULT_VALIDATION_DIR` (import from factory_log, not a second definition) | INV-fh-004 | IT-fh-06 (import-identity assertion) | pending |

## #185 — code_query orient inferred-binding over-match (BR-fh-005)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| IDF-weight word overlap OR require entity+verb combination (stdlib, deterministic) | CTR-fh-050, CTR-fh-051, INV-fh-012 | unit: `test_code_query` — single-common-entity-word file yields NO inferred fact (the auth-provider negative) | pending |
| Inferred facts stay labeled `inferred`, ranked below `direct` via leading relation-tier `_rank_key` element | CTR-fh-052, INV-fh-012 | `test_code_query_golden` envelope ordering; IT-fh-03 case (d) | pending |
| Channel separation from the #187 hotspot tier (measured = exact membership only) | CTR-fh-053, INV-fh-007 | IT-fh-03 (lexically-hot-but-not-listed → no fact) | pending |
| Deterministic across runs/platforms | CTR-fh-051 | unit: repeated-run byte-identical envelope | pending |

## #187 — hotspot discovery as derived gnosis (BR-fh-006)

| Acceptance criterion | Contracts / invariants | Planned tests | Status |
|---|---|---|---|
| hotspots.py + coupling REUSE (process.py, no second engine) + CLI; synthetic-outlier + determinism tests | CTR-fh-030, CTR-fh-032, INV-fh-001 | IT-fh-08 (+ reimplementation negative: module imports process.analyze) | pending |
| `window_days` derived by hotspot_discovery from commit dates (churn.analyze has no window param) and recorded | CTR-fh-031 | IT-fh-08 (`--check` compares recorded vs re-derived window) | pending |
| orient surfaces hotspot facts; provenance carries generating sha; measured tier sorts last | CTR-fh-033, CTR-fh-034, CTR-fh-052 | IT-fh-03 (incl. case (d) direct-sorts-first) | pending |
| No stable IDs; never gates; `--check` staleness | CTR-fh-031, CTR-fh-033, INV-fh-007 | IT-fh-08 (`--check` nonzero; generate never gates) | pending |
| /architect + /implement consume the report | (workflow doc) | manual review | pending |
| Run against chief-wiggum + one shipped repo, findings in PR | (report-only validation) | manual review | pending |

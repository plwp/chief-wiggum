## Gate Blocking-Authority Lifecycle

The lifecycle of a blocking-capable gate's authority to block, spanning #184 (validation records + --scanner-version for five gates incl. check_architecture) and the existing check_gate_validation.py + factory_log demotion semantics. Core safety property: a gate may not block unless its CURRENT scanner version is validated by a corroborated record (INV-fh-003). Validity is ALWAYS read via `check_gate_validation <gate> --format json` passing==true — never the default exit code, which is 0 in report-only mode even when not validated.

```mermaid
stateDiagram-v2
    [*] --> unknown
    unknown --> report_only: run_without_record [Gate is invoked; a record exists but check_gate_validation reports passing==false (a gate with NO record at all stays in 'unknown', also report-only in effect)]
    unknown --> validated: author_record [Author + live-verify + journal a record; check_gate_validation --format json reports passing==true (never the default exit code)]
    report_only --> validated: author_record [A complete, corroborated record now passes: check_gate_validation --format json passing==true]
    report_only --> unknown: record_removed [The validation record file was deleted or is no longer present in the canonical validation dir]
    validated --> blocking: wire_gate [Only a validated gate (passing==true via --format json, current scanner_version, journaled provenance) may be wired --gate (INV-fh-003)]
    validated --> stale: scanner_or_journal_drift [Live scanner version no longer matches the record, or the journal hash-chain breaks; previous_authority := validated]
    validated --> report_only: record_missing_or_invalid [The record regressed without scanner drift: schema-invalid, status!=passed, or otherwise passing==false]
    blocking --> validated: unwire_gate [Operator intentionally removes --gate wiring; the record itself still passes]
    blocking --> stale: scanner_or_journal_drift [A scanner edit changed --scanner-version (or journal broke) while the gate was blocking; previous_authority := blocking]
    blocking --> demoted: escape_matches_certified_seed_class [A production escape matched a seed_class the record certified caught — factory_log.demotion_check(escape.missed_by, escape.seed_class) returns a demotion instruction (real signature)]
    blocking --> demoted: record_missing_or_invalid [The record was deleted or regressed to passing==false while the gate was wired --gate — blocking authority cannot outlive its record]
    stale --> demoted: auto_demote [Fail-to-report-only policy: a record that went stale WHILE BLOCKING demotes blocking authority. NOTE: stale demotion emits the GENERIC emit(DEMOTION, gate=gate, details='stale') event — factory_log.emit_demotion requires a seed_class, which a staleness (non-escape) demotion does not have]
    stale --> report_only: downgrade_nonblocking_stale [The record went stale while merely validated (never wired) — no blocking authority to demote; it downgrades to report_only until re-derived]
    stale --> validated: re_derive_record [The record is re-authored against the current scanner and re-journaled; check_gate_validation --format json passing==true again]
    demoted --> validated: re_derive_and_rejournal [Re-run the seed classes + clean corpus, re-journal, and pass check_gate_validation (--format json passing==true) before re-promotion]
```

### States
- `unknown` (initial) — No validation record exists (or it was removed). The gate may RUN report-only but MUST NOT carry --gate.
- `report_only` — A record exists but is not currently valid: check_gate_validation --format json reports passing==false (status!=passed, schema-invalid, or provenance failure). The gate runs and never blocks. NOTE: a fully-validated-but-unwired gate is in 'validated', NOT here — this state means the record itself does not pass.
- `validated` — check_gate_validation --format json passing==true: schema ok, status==passed, scanner_version current, journal corroborated. ELIGIBLE to be wired with --gate (wiring is the separate wire_gate event).
- `blocking` — Validated AND wired with --gate. Actively blocks the workflow on a finding. Normal (not terminal) — can go stale, be demoted, or be intentionally un-wired back to validated.
- `stale` — Scanner version or journal provenance no longer matches the record (e.g. #184 added --scanner-version and a subsequent scanner edit bumped the hash). The system must NOT silently remain blocking. previous_authority records where it came from.
- `demoted` — Forcibly removed from blocking authority — a production escape matched a seed_class the record certified caught (factory_log demotion_check), a stale-while-blocking record auto-demoted (fail-to-report-only), or the record went missing/invalid while blocking. Record must be re-derived before re-promotion.

### Transitions
| From | To | Trigger | Guard Conditions |
|------|----|---------|-----------------|
| unknown | report_only | run_without_record | Gate is invoked; a record exists but check_gate_validation reports passing==false (a gate with NO record at all stays in 'unknown', also report-only in effect) |
| unknown | validated | author_record | Author + live-verify + journal a record; check_gate_validation --format json reports passing==true (never the default exit code) |
| report_only | validated | author_record | A complete, corroborated record now passes: check_gate_validation --format json passing==true |
| report_only | unknown | record_removed | The validation record file was deleted or is no longer present in the canonical validation dir |
| validated | blocking | wire_gate | Only a validated gate (passing==true via --format json, current scanner_version, journaled provenance) may be wired --gate (INV-fh-003) |
| validated | stale | scanner_or_journal_drift | Live scanner version no longer matches the record, or the journal hash-chain breaks; previous_authority := validated |
| validated | report_only | record_missing_or_invalid | The record regressed without scanner drift: schema-invalid, status!=passed, or otherwise passing==false |
| blocking | validated | unwire_gate | Operator intentionally removes --gate wiring; the record itself still passes |
| blocking | stale | scanner_or_journal_drift | A scanner edit changed --scanner-version (or journal broke) while the gate was blocking; previous_authority := blocking |
| blocking | demoted | escape_matches_certified_seed_class | A production escape matched a seed_class the record certified caught — factory_log.demotion_check(escape.missed_by, escape.seed_class) returns a demotion instruction (real signature) |
| blocking | demoted | record_missing_or_invalid | The record was deleted or regressed to passing==false while the gate was wired --gate — blocking authority cannot outlive its record |
| stale | demoted | auto_demote | Fail-to-report-only policy: a record that went stale WHILE BLOCKING demotes blocking authority. NOTE: stale demotion emits the GENERIC emit(DEMOTION, gate=gate, details='stale') event — factory_log.emit_demotion requires a seed_class, which a staleness (non-escape) demotion does not have |
| stale | report_only | downgrade_nonblocking_stale | The record went stale while merely validated (never wired) — no blocking authority to demote; it downgrades to report_only until re-derived |
| stale | validated | re_derive_record | The record is re-authored against the current scanner and re-journaled; check_gate_validation --format json passing==true again |
| demoted | validated | re_derive_and_rejournal | Re-run the seed classes + clean corpus, re-journal, and pass check_gate_validation (--format json passing==true) before re-promotion |

### Invalid Transitions (must be rejected)
- unknown → blocking (blocking is unreachable without a passing, corroborated validation record — a gate with no record must never carry --gate (INV-fh-003).)
- report_only → blocking (a failing/invalid record cannot be wired to block; it must reach 'validated' (passing==true via --format json) first.)
- stale → blocking (a stale record cannot block; scanner_version and journal must corroborate (-> validated) before re-promotion.)
- demoted → blocking (a demoted gate must be re-validated (-> validated) before blocking again; direct demoted->blocking would skip re-derivation.)

### Invariants
- **INV-fh-001** [consistency]: Change-coupling has ONE engine. Change-coupling/co-change confidence is computed only by scripts/quality/process.py; hotspots.py consumes it and no other module recomputes co-change (opus correction: #187 reuses, does not rebuild).
- **INV-fh-002** [consistency]: Consult cost is derived and single-sourced — scoped to `consult` events only (CLAUDE_CODE records ingest OTEL-reported costs and are out of scope). cost_usd on a consult record is conditionally derived via factory_log.cost_for against config/model_pricing.json: null when either token count is unknown, else exactly cost_for's value. No consult path stores an author-computed dollar figure.
- **INV-fh-003** [operational_safety]: No blocking without a passing record. A gate may be wired with --gate in a workflow only if `check_gate_validation.py <gate> --format json` reports passing==true (NEVER inferred from the default exit code, which is 0 in report-only mode even when not validated) with current scanner_version and journaled provenance. The gate-lifecycle machine's core safety property.
- **INV-fh-004** [consistency]: Validation records live in exactly one place. The validation directory docs/quality/validation/ is defined ONCE. BUG: currently defined twice — factory_log.DEFAULT_VALIDATION_DIR and check_gate_validation.DEFAULT_VALIDATION_DIR — and the two VALUES already differ in form (absolute vs relative), so equality-by-accident cannot be assumed. The fix is an IMPORT: check_gate_validation imports the constant from factory_log (one definition site); check_single_writer will flag any re-definition as an unsanctioned writer.
- **INV-fh-005** [operational_safety]: Scanner version is hash-derived, never hand-set. Every gate that supports --scanner-version derives it via chief_wiggum.hashing.scanner_version(__file__, *deps) — never a literal constant — and the dep list covers every finding-affecting chief_wiggum import. This makes 'stale record' structurally detectable.
- **INV-fh-006** [data_integrity]: Derived crossing labels are computed, not authored. In architecture.json, trust_zone_crossing/region_crossing (and any propagated carries label) are computed by check_architecture.py from node attributes; the schema permits a null placeholder but ANY authored non-null value is a finding. Prevents a hand-authored 'safe' label masking a real trust-zone violation.
- **INV-fh-007** [consistency]: Derived artifacts never enter Plane A. docs/quality/hotspots.json (and any measured/rebuildable artifact) carries NO ARC-/CTR-/INV-/BR- stable IDs, is referenced by NO @cw-trace link, and is surfaced by code_query only as a 'measured' fact. The below-direct ranking is ENFORCED mechanically: code_query._rank_key's LEADING element is the relation tier (direct=0, inferred=1, measured=2), placed before the exact key — so a measured fact can never outrank a direct or inferred one regardless of its exact-match flag.
- **INV-fh-008** [consistency]: architecture.json and system-contracts.json cross-refs resolve. Every node/connector referenced by system-contracts.json budget-tree chains and telemetry bindings must name a declared ARC-/EDG- in architecture.json, and vice-versa where declared. Neither model silently invents the other's nodes.
- **INV-fh-009** [temporal]: Comment thread is append-only, order-preserving. TicketContext.comments preserves source (chronological) order and is never re-sorted, de-duplicated, or merged into acceptance_criteria. Amendment semantics depend on order.
- **INV-fh-010** [authorization]: Comments are context unless promoted to amendments by a defined rule — and amendments alter AC PRESENTATIONALLY only. An amendment (a comment by the issue author or a maintainer — author_association OWNER/MEMBER/COLLABORATOR — carrying an explicit 'AC:' block) may change the acceptance criteria the reviewer is told are in force, rendered in the labeled amendments region of the prompt; the STORED acceptance_criteria field is never rewritten (reconciled with INV-fh-009). A non-authoritative comment is shown as discussion, never as a requirement.
- **INV-fh-011** [data_integrity]: Usage cost is honest — never a silent zero. tokens_in/tokens_out obey both-tokens-or-null; both are null (never 0, never estimated) when a provider does not surface complete usage; usage_status names the true source (provider-json | sdk-metadata | partial | unavailable); cost_usd is null or nonzero-derived, never a fabricated 0.
- **INV-fh-012** [consistency]: Inferred (artifact-derived) bindings are precision-bounded. code_query orient's inferred facts are always labeled 'inferred' and sort below any 'direct' via the leading relation-tier rank key; the lexical matcher must not surface an inferred fact on single common-entity-word overlap alone (#185: IDF-weighting over the epic's own operations/routes or entity+verb combination, stdlib + deterministic). The #187 hotspot fact is a SEPARATE exact-membership tier (relation 'measured', tier 2), never routed through the lexical matcher.

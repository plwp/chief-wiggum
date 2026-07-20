# Test Plan: Gate Blocking-Authority Lifecycle

Generated from formal model. 20 paths covering 6/6 states and 11 transitions.

## Positive Test Cases (valid paths)

### Path 1: → report_only
```
unknown--run_without_record-->report_only
```

### Path 2: → report_only
```
unknown--author_record-->validated → validated--wire_gate-->blocking → blocking--scanner_or_journal_drift-->stale → stale--downgrade_nonblocking_stale-->report_only
```

### Path 3: → report_only
```
unknown--author_record-->validated → validated--scanner_or_journal_drift-->stale → stale--downgrade_nonblocking_stale-->report_only
```

### Path 4: → report_only
```
unknown--author_record-->validated → validated--record_missing_or_invalid-->report_only
```

### Path 5: → validated
```
unknown--run_without_record-->report_only → report_only--author_record-->validated
```

### Path 6: → validated
```
unknown--author_record-->validated
```

### Path 7: → blocking
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--wire_gate-->blocking
```

### Path 8: → blocking
```
unknown--author_record-->validated → validated--wire_gate-->blocking
```

### Path 9: → stale
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--wire_gate-->blocking → blocking--scanner_or_journal_drift-->stale
```

### Path 10: → stale
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--scanner_or_journal_drift-->stale
```

### Path 11: → stale
```
unknown--author_record-->validated → validated--wire_gate-->blocking → blocking--scanner_or_journal_drift-->stale
```

### Path 12: → stale
```
unknown--author_record-->validated → validated--scanner_or_journal_drift-->stale
```

### Path 13: → demoted
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--wire_gate-->blocking → blocking--scanner_or_journal_drift-->stale → stale--auto_demote-->demoted
```

### Path 14: → demoted
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--wire_gate-->blocking → blocking--escape_matches_certified_seed_class-->demoted
```

### Path 15: → demoted
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--wire_gate-->blocking → blocking--record_missing_or_invalid-->demoted
```

### Path 16: → demoted
```
unknown--run_without_record-->report_only → report_only--author_record-->validated → validated--scanner_or_journal_drift-->stale → stale--auto_demote-->demoted
```

### Path 17: → demoted
```
unknown--author_record-->validated → validated--wire_gate-->blocking → blocking--scanner_or_journal_drift-->stale → stale--auto_demote-->demoted
```

### Path 18: → demoted
```
unknown--author_record-->validated → validated--wire_gate-->blocking → blocking--escape_matches_certified_seed_class-->demoted
```

### Path 19: → demoted
```
unknown--author_record-->validated → validated--wire_gate-->blocking → blocking--record_missing_or_invalid-->demoted
```

### Path 20: → demoted
```
unknown--author_record-->validated → validated--scanner_or_journal_drift-->stale → stale--auto_demote-->demoted
```

## Negative Test Cases (must be rejected)

- **unknown → blocking**: blocking is unreachable without a passing, corroborated validation record — a gate with no record must never carry --gate (INV-fh-003). — expect 400/409
- **report_only → blocking**: a failing/invalid record cannot be wired to block; it must reach 'validated' (passing==true via --format json) first. — expect 400/409
- **stale → blocking**: a stale record cannot block; scanner_version and journal must corroborate (-> validated) before re-promotion. — expect 400/409
- **demoted → blocking**: a demoted gate must be re-validated (-> validated) before blocking again; direct demoted->blocking would skip re-derivation. — expect 400/409

## Invariant Checks (verify at each state)

- **INV-fh-001**: Change-coupling has ONE engine. Change-coupling/co-change confidence is computed only by scripts/quality/process.py; hotspots.py consumes it and no other module recomputes co-change (opus correction: #187 reuses, does not rebuild).
- **INV-fh-002**: Consult cost is derived and single-sourced — scoped to `consult` events only (CLAUDE_CODE records ingest OTEL-reported costs and are out of scope). cost_usd on a consult record is conditionally derived via factory_log.cost_for against config/model_pricing.json: null when either token count is unknown, else exactly cost_for's value. No consult path stores an author-computed dollar figure.
- **INV-fh-003**: No blocking without a passing record. A gate may be wired with --gate in a workflow only if `check_gate_validation.py <gate> --format json` reports passing==true (NEVER inferred from the default exit code, which is 0 in report-only mode even when not validated) with current scanner_version and journaled provenance. The gate-lifecycle machine's core safety property.
- **INV-fh-004**: Validation records live in exactly one place. The validation directory docs/quality/validation/ is defined ONCE. BUG: currently defined twice — factory_log.DEFAULT_VALIDATION_DIR and check_gate_validation.DEFAULT_VALIDATION_DIR — and the two VALUES already differ in form (absolute vs relative), so equality-by-accident cannot be assumed. The fix is an IMPORT: check_gate_validation imports the constant from factory_log (one definition site); check_single_writer will flag any re-definition as an unsanctioned writer.
- **INV-fh-005**: Scanner version is hash-derived, never hand-set. Every gate that supports --scanner-version derives it via chief_wiggum.hashing.scanner_version(__file__, *deps) — never a literal constant — and the dep list covers every finding-affecting chief_wiggum import. This makes 'stale record' structurally detectable.
- **INV-fh-006**: Derived crossing labels are computed, not authored. In architecture.json, trust_zone_crossing/region_crossing (and any propagated carries label) are computed by check_architecture.py from node attributes; the schema permits a null placeholder but ANY authored non-null value is a finding. Prevents a hand-authored 'safe' label masking a real trust-zone violation.
- **INV-fh-007**: Derived artifacts never enter Plane A. docs/quality/hotspots.json (and any measured/rebuildable artifact) carries NO ARC-/CTR-/INV-/BR- stable IDs, is referenced by NO @cw-trace link, and is surfaced by code_query only as a 'measured' fact. The below-direct ranking is ENFORCED mechanically: code_query._rank_key's LEADING element is the relation tier (direct=0, inferred=1, measured=2), placed before the exact key — so a measured fact can never outrank a direct or inferred one regardless of its exact-match flag.
- **INV-fh-008**: architecture.json and system-contracts.json cross-refs resolve. Every node/connector referenced by system-contracts.json budget-tree chains and telemetry bindings must name a declared ARC-/EDG- in architecture.json, and vice-versa where declared. Neither model silently invents the other's nodes.
- **INV-fh-009**: Comment thread is append-only, order-preserving. TicketContext.comments preserves source (chronological) order and is never re-sorted, de-duplicated, or merged into acceptance_criteria. Amendment semantics depend on order.
- **INV-fh-010**: Comments are context unless promoted to amendments by a defined rule — and amendments alter AC PRESENTATIONALLY only. An amendment (a comment by the issue author or a maintainer — author_association OWNER/MEMBER/COLLABORATOR — carrying an explicit 'AC:' block) may change the acceptance criteria the reviewer is told are in force, rendered in the labeled amendments region of the prompt; the STORED acceptance_criteria field is never rewritten (reconciled with INV-fh-009). A non-authoritative comment is shown as discussion, never as a requirement.
- **INV-fh-011**: Usage cost is honest — never a silent zero. tokens_in/tokens_out obey both-tokens-or-null; both are null (never 0, never estimated) when a provider does not surface complete usage; usage_status names the true source (provider-json | sdk-metadata | partial | unavailable); cost_usd is null or nonzero-derived, never a fabricated 0.
- **INV-fh-012**: Inferred (artifact-derived) bindings are precision-bounded. code_query orient's inferred facts are always labeled 'inferred' and sort below any 'direct' via the leading relation-tier rank key; the lexical matcher must not surface an inferred fact on single common-entity-word overlap alone (#185: IDF-weighting over the epic's own operations/routes or entity+verb combination, stdlib + deterministic). The #187 hotspot fact is a SEPARATE exact-membership tier (relation 'measured', tier 2), never routed through the lexical matcher.

## Coverage Summary

| Metric | Value |
|--------|-------|
| Total paths | 20 |
| States covered | 6/6 |
| Transitions covered | 11 |
| Invalid transitions to test | 4 |
| Invariants to verify | 12 |

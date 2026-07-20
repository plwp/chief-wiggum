"""
Auto-generated Hypothesis RuleBasedStateMachine for: Gate Blocking-Authority Lifecycle
Generated from formal model. Do not edit by hand.
"""

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize


class GateBlocking-AuthorityLifecycle(RuleBasedStateMachine):
    """State machine test: The lifecycle of a blocking-capable gate's authority to block, spanning #184 (validation records + --scanner-version for five gates incl. check_architecture) and the existing check_gate_validation.py + factory_log demotion semantics. Core safety property: a gate may not block unless its CURRENT scanner version is validated by a corroborated record (INV-fh-003). Validity is ALWAYS read via `check_gate_validation <gate> --format json` passing==true — never the default exit code, which is 0 in report-only mode even when not validated."""

    VALID_STATES = ['unknown', 'report_only', 'validated', 'blocking', 'stale', 'demoted']
    TERMINAL_STATES = []

    @initialize()
    def init(self):
        self.state = "unknown"

    @rule()
    def transition_unknown_to_report_only_via_run_without_record(self):  # Guards: Gate is invoked; a record exists but check_gate_validation reports passing==false (a gate with NO record at all stays in 'unknown', also report-only in effect)
        if self.state != "unknown":
            return
        self.state = "report_only"

    @rule()
    def transition_unknown_to_validated_via_author_record(self):  # Guards: Author + live-verify + journal a record; check_gate_validation --format json reports passing==true (never the default exit code)
        if self.state != "unknown":
            return
        self.state = "validated"

    @rule()
    def transition_report_only_to_validated_via_author_record(self):  # Guards: A complete, corroborated record now passes: check_gate_validation --format json passing==true
        if self.state != "report_only":
            return
        self.state = "validated"

    @rule()
    def transition_report_only_to_unknown_via_record_removed(self):  # Guards: The validation record file was deleted or is no longer present in the canonical validation dir
        if self.state != "report_only":
            return
        self.state = "unknown"

    @rule()
    def transition_validated_to_blocking_via_wire_gate(self):  # Guards: Only a validated gate (passing==true via --format json, current scanner_version, journaled provenance) may be wired --gate (INV-fh-003)
        if self.state != "validated":
            return
        self.state = "blocking"

    @rule()
    def transition_validated_to_stale_via_scanner_or_journal_drift(self):  # Guards: Live scanner version no longer matches the record, or the journal hash-chain breaks; previous_authority := validated
        if self.state != "validated":
            return
        self.state = "stale"

    @rule()
    def transition_validated_to_report_only_via_record_missing_or_invalid(self):  # Guards: The record regressed without scanner drift: schema-invalid, status!=passed, or otherwise passing==false
        if self.state != "validated":
            return
        self.state = "report_only"

    @rule()
    def transition_blocking_to_validated_via_unwire_gate(self):  # Guards: Operator intentionally removes --gate wiring; the record itself still passes
        if self.state != "blocking":
            return
        self.state = "validated"

    @rule()
    def transition_blocking_to_stale_via_scanner_or_journal_drift(self):  # Guards: A scanner edit changed --scanner-version (or journal broke) while the gate was blocking; previous_authority := blocking
        if self.state != "blocking":
            return
        self.state = "stale"

    @rule()
    def transition_blocking_to_demoted_via_escape_matches_certified_seed_class(self):  # Guards: A production escape matched a seed_class the record certified caught — factory_log.demotion_check(escape.missed_by, escape.seed_class) returns a demotion instruction (real signature)
        if self.state != "blocking":
            return
        self.state = "demoted"

    @rule()
    def transition_blocking_to_demoted_via_record_missing_or_invalid(self):  # Guards: The record was deleted or regressed to passing==false while the gate was wired --gate — blocking authority cannot outlive its record
        if self.state != "blocking":
            return
        self.state = "demoted"

    @rule()
    def transition_stale_to_demoted_via_auto_demote(self):  # Guards: Fail-to-report-only policy: a record that went stale WHILE BLOCKING demotes blocking authority. NOTE: stale demotion emits the GENERIC emit(DEMOTION, gate=gate, details='stale') event — factory_log.emit_demotion requires a seed_class, which a staleness (non-escape) demotion does not have
        if self.state != "stale":
            return
        self.state = "demoted"

    @rule()
    def transition_stale_to_report_only_via_downgrade_nonblocking_stale(self):  # Guards: The record went stale while merely validated (never wired) — no blocking authority to demote; it downgrades to report_only until re-derived
        if self.state != "stale":
            return
        self.state = "report_only"

    @rule()
    def transition_stale_to_validated_via_re_derive_record(self):  # Guards: The record is re-authored against the current scanner and re-journaled; check_gate_validation --format json passing==true again
        if self.state != "stale":
            return
        self.state = "validated"

    @rule()
    def transition_demoted_to_validated_via_re_derive_and_rejournal(self):  # Guards: Re-run the seed classes + clean corpus, re-journal, and pass check_gate_validation (--format json passing==true) before re-promotion
        if self.state != "demoted":
            return
        self.state = "validated"

    @invariant()
    def check_inv_fh_001(self):
        """Change-coupling has ONE engine. Change-coupling/co-change confidence is computed only by scripts/quality/process.py; hotspots.py consumes it and no other module recomputes co-change (opus correction: #187 reuses, does not rebuild)."""
        # TODO: implement check — expression: co_change_confidence_writers == {'scripts/quality/process.py'}
        pass

    @invariant()
    def check_inv_fh_002(self):
        """Consult cost is derived and single-sourced — scoped to `consult` events only (CLAUDE_CODE records ingest OTEL-reported costs and are out of scope). cost_usd on a consult record is conditionally derived via factory_log.cost_for against config/model_pricing.json: null when either token count is unknown, else exactly cost_for's value. No consult path stores an author-computed dollar figure."""
        # TODO: implement check — expression: record.event == 'consult' implies (record.cost_usd is None if (record.tokens_in is None or record.tokens_out is None) else record.cost_usd == cost_for(record.name, record.tokens_in, record.tokens_out))
        pass

    @invariant()
    def check_inv_fh_003(self):
        """No blocking without a passing record. A gate may be wired with --gate in a workflow only if `check_gate_validation.py <gate> --format json` reports passing==true (NEVER inferred from the default exit code, which is 0 in report-only mode even when not validated) with current scanner_version and journaled provenance. The gate-lifecycle machine's core safety property."""
        # TODO: implement check — expression: gate_wired implies (check_gate_validation_json(gate)['passing'] is True and record.scanner_version == live_scanner_version(gate))
        pass

    @invariant()
    def check_inv_fh_004(self):
        """Validation records live in exactly one place. The validation directory docs/quality/validation/ is defined ONCE. BUG: currently defined twice — factory_log.DEFAULT_VALIDATION_DIR and check_gate_validation.DEFAULT_VALIDATION_DIR — and the two VALUES already differ in form (absolute vs relative), so equality-by-accident cannot be assumed. The fix is an IMPORT: check_gate_validation imports the constant from factory_log (one definition site); check_single_writer will flag any re-definition as an unsanctioned writer."""
        # TODO: implement check — expression: check_gate_validation.DEFAULT_VALIDATION_DIR is factory_log.DEFAULT_VALIDATION_DIR  # import, not a second assignment
        pass

    @invariant()
    def check_inv_fh_005(self):
        """Scanner version is hash-derived, never hand-set. Every gate that supports --scanner-version derives it via chief_wiggum.hashing.scanner_version(__file__, *deps) — never a literal constant — and the dep list covers every finding-affecting chief_wiggum import. This makes 'stale record' structurally detectable."""
        # TODO: implement check — expression: for gate in scanner_version_gates: gate.scanner_version == scanner_version(gate.__file__, *gate.deps) and set(gate.deps) >= chief_wiggum_imports(gate)
        pass

    @invariant()
    def check_inv_fh_006(self):
        """Derived crossing labels are computed, not authored. In architecture.json, trust_zone_crossing/region_crossing (and any propagated carries label) are computed by check_architecture.py from node attributes; the schema permits a null placeholder but ANY authored non-null value is a finding. Prevents a hand-authored 'safe' label masking a real trust-zone violation."""
        # TODO: implement check — expression: all(edge.trust_zone_crossing is None and edge.region_crossing is None for edge in authored_edges)
        pass

    @invariant()
    def check_inv_fh_007(self):
        """Derived artifacts never enter Plane A. docs/quality/hotspots.json (and any measured/rebuildable artifact) carries NO ARC-/CTR-/INV-/BR- stable IDs, is referenced by NO @cw-trace link, and is surfaced by code_query only as a 'measured' fact. The below-direct ranking is ENFORCED mechanically: code_query._rank_key's LEADING element is the relation tier (direct=0, inferred=1, measured=2), placed before the exact key — so a measured fact can never outrank a direct or inferred one regardless of its exact-match flag."""
        # TODO: implement check — expression: not ID_RE.search(json.dumps(hotspots_json)) and _rank_key(fact)[0] == {'direct': 0, 'inferred': 1, 'measured': 2}[fact.relation]
        pass

    @invariant()
    def check_inv_fh_008(self):
        """architecture.json and system-contracts.json cross-refs resolve. Every node/connector referenced by system-contracts.json budget-tree chains and telemetry bindings must name a declared ARC-/EDG- in architecture.json, and vice-versa where declared. Neither model silently invents the other's nodes."""
        # TODO: implement check — expression: budget_tree_refs <= declared_arc_edg_ids and telemetry_bindings_refs <= declared_arc_edg_ids
        pass

    @invariant()
    def check_inv_fh_009(self):
        """Comment thread is append-only, order-preserving. TicketContext.comments preserves source (chronological) order and is never re-sorted, de-duplicated, or merged into acceptance_criteria. Amendment semantics depend on order."""
        # TODO: implement check — expression: ctx.comments == source_order(ctx.comments) and 'comments' not in writers_of(ctx.acceptance_criteria)
        pass

    @invariant()
    def check_inv_fh_010(self):
        """Comments are context unless promoted to amendments by a defined rule — and amendments alter AC PRESENTATIONALLY only. An amendment (a comment by the issue author or a maintainer — author_association OWNER/MEMBER/COLLABORATOR — carrying an explicit 'AC:' block) may change the acceptance criteria the reviewer is told are in force, rendered in the labeled amendments region of the prompt; the STORED acceptance_criteria field is never rewritten (reconciled with INV-fh-009). A non-authoritative comment is shown as discussion, never as a requirement."""
        # TODO: implement check — expression: rendered_effective_ac == apply_amendments(ctx.acceptance_criteria, ctx.amendments) and stored(ctx.acceptance_criteria) == baseline(ctx.acceptance_criteria) and (alters_rendered_ac(c) implies is_amendment(c))
        pass

    @invariant()
    def check_inv_fh_011(self):
        """Usage cost is honest — never a silent zero. tokens_in/tokens_out obey both-tokens-or-null; both are null (never 0, never estimated) when a provider does not surface complete usage; usage_status names the true source (provider-json | sdk-metadata | partial | unavailable); cost_usd is null or nonzero-derived, never a fabricated 0."""
        # TODO: implement check — expression: ((tokens_in is None) == (tokens_out is None)) and ((usage_status in ('unavailable', 'partial')) == (tokens_in is None)) and (cost_usd is None or cost_usd != 0.0)
        pass

    @invariant()
    def check_inv_fh_012(self):
        """Inferred (artifact-derived) bindings are precision-bounded. code_query orient's inferred facts are always labeled 'inferred' and sort below any 'direct' via the leading relation-tier rank key; the lexical matcher must not surface an inferred fact on single common-entity-word overlap alone (#185: IDF-weighting over the epic's own operations/routes or entity+verb combination, stdlib + deterministic). The #187 hotspot fact is a SEPARATE exact-membership tier (relation 'measured', tier 2), never routed through the lexical matcher."""
        # TODO: implement check — expression: fact.relation == 'inferred' implies (_rank_key(fact)[0] == 1 and not single_common_word_overlap(fact)) and fact.relation == 'measured' implies fact.source == 'hotspot-membership'
        pass


    # --- Invalid transition assertions ---

    @rule()
    def invalid_unknown_to_blocking(self):
        """Must be rejected: blocking is unreachable without a passing, corroborated validation record — a gate with no record must never carry --gate (INV-fh-003)."""
        if self.state != "unknown":
            return
        # Assert this transition is not possible
        assert self.state != "blocking" or self.state == "unknown"

    @rule()
    def invalid_report_only_to_blocking(self):
        """Must be rejected: a failing/invalid record cannot be wired to block; it must reach 'validated' (passing==true via --format json) first."""
        if self.state != "report_only":
            return
        # Assert this transition is not possible
        assert self.state != "blocking" or self.state == "report_only"

    @rule()
    def invalid_stale_to_blocking(self):
        """Must be rejected: a stale record cannot block; scanner_version and journal must corroborate (-> validated) before re-promotion."""
        if self.state != "stale":
            return
        # Assert this transition is not possible
        assert self.state != "blocking" or self.state == "stale"

    @rule()
    def invalid_demoted_to_blocking(self):
        """Must be rejected: a demoted gate must be re-validated (-> validated) before blocking again; direct demoted->blocking would skip re-derivation."""
        if self.state != "demoted":
            return
        # Assert this transition is not possible
        assert self.state != "blocking" or self.state == "demoted"


TestStateMachine = GateBlocking-AuthorityLifecycle.TestCase
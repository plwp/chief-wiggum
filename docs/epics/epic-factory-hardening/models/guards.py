"""
Auto-generated guard clauses from formal contracts.
Generated from formal model. Do not edit by hand.
"""

def run_review_with_comments_(assemble_review_prompt)(request):
    """#83: fold ticket comment thread into TicketContext + TWO labeled prompt regions (amendments vs discussion), and fix the upstream ticket.json writer. Reviewers must judge against CURRENT authoritative state, not stale body ACs alone."""
    # REQUIRES: --ticket-context JSON parses and TicketContext.from_dict preserves EVERY key including comments (no silent drop — the #83 bug).
    if not (ctx = TicketContext.from_dict(json.loads(path.read_text())); assert 'comments' not in json_keys or ctx.comments is not None):
        raise HTTPError(1, "--ticket-context JSON parses and TicketContext.from_dict preserves EVERY key including comments (no silent drop — the #83 bug).")

    # REQUIRES: Upstream ticket.json writer (the /implement skill shell) fetched and serialized comments incl. author_association via `gh issue view <n> --json comments`.
    if not (ticket_json.get('comments') is not None  # root-cause: empty upstream => field stays empty in production):
        raise HTTPError(1, "Upstream ticket.json writer (the /implement skill shell) fetched and serialized comments incl. author_association via `gh issue view <n> --json comments`.")

    # --- implementation ---

    # ENSURES:
    # Prompt renders TWO labeled regions, both distinct from {{ACCEPTANCE_CRITERIA}}: 'Accepted AC amendments (authoritative-on-conflict)' — only comments passing the promotion rule — and 'Discussion/context (non-authoritative)' for everything else. The raw comment thread as a whole is NEVER labeled authoritative. Empty thread renders a placeholder.
    # Substitution stays single-pass: a body that literally contains `{{TICKET_COMMENTS}}` is never re-scanned; comment source order is preserved within each rendered region.


def consult_ai_usage_capture_(_emit_consult_telemetry)(request):
    """#134: each consult_* switches to a usage-bearing output format, parses that CLI's usage shape + resolved model id, threads it through consult_provider, then emit_consult(provider, model, tin, tout, repo=..., ticket=...). Cost computed only inside emit_consult."""
    # REQUIRES: The provider CLI/SDK was invoked with a usage-bearing output format (claude -p --output-format json; gemini json; vertex response.usage_metadata; codex exec JSON event stream; claude-interactive has none => usage_status 'unavailable' by construction).
    if not (adapter in USAGE_BEARING_FORMATS or usage_status == 'unavailable'):
        raise HTTPError(0, "The provider CLI/SDK was invoked with a usage-bearing output format (claude -p --output-format json; gemini json; vertex response.usage_metadata; codex exec JSON event stream; claude-interactive has none => usage_status 'unavailable' by construction).")

    # REQUIRES: Usage parsing is wrapped so a parse failure NEVER fails the consult itself (the consult's text output is the product; telemetry is best-effort — preserve _emit_consult_telemetry's swallow-all contract).
    if not (try: usage = parse_usage(out, err) except Exception: usage = None  # consult already returned its text):
        raise HTTPError(0, "Usage parsing is wrapped so a parse failure NEVER fails the consult itself (the consult's text output is the product; telemetry is best-effort — preserve _emit_consult_telemetry's swallow-all contract).")

    # REQUIRES: Capture BOTH stdout and stderr: some CLIs print the JSON usage payload to stderr; _run_capture currently returns stdout only.
    if not (captured.stdout is not None and captured.stderr is not None):
        raise HTTPError(0, "Capture BOTH stdout and stderr: some CLIs print the JSON usage payload to stderr; _run_capture currently returns stdout only.")

    # --- implementation ---

    # ENSURES:
    # emit_consult called exactly once per successful consult with the RESOLVED billed model id; the resolved name is never a bare CLI alias.
    # cost_usd is conditionally derived inside emit_consult and nowhere else (INV-fh-002): null when either token count is unknown, else exactly cost_for's value.
    # usage_status reflects the true source; both-tokens-or-null holds; tokens/cost are null (never 0, never estimated) when usage is unavailable or partial (INV-fh-011).


def check_architecture_(static_consistency)(request):
    """#174: STATIC declared-model consistency. Report-only by default (exit 0 even WITH findings), --gate opts into blocking (exit 1 on findings), exit 2 usage errors only. Emits emit_gate('check_architecture', pass|fail, caught=n) best-effort. Prints the authority line verbatim every run. Follow check_budget_tree's bespoke _validate_value walker so findings carry a JSON path. Per ADR-fh-06 this checker is the FIFTH #184 gate: it ships --scanner-version and receives a validation record in the same pass, after its CHECKS inventory freezes."""
    # REQUIRES: architecture.json parses as JSON and validates against architecture-schema.json (ARC-/EDG-/ASM- patterns, additionalProperties:false).
    if not (json.loads(path); validate(doc, 'architecture') == []):
        raise HTTPError(0, "architecture.json parses as JSON and validates against architecture-schema.json (ARC-/EDG-/ASM- patterns, additionalProperties:false).")

    # REQUIRES: Every edge.from / edge.to resolves to a declared node id (the base 'endpoints exist' check).
    if not (all(e['from'] in node_ids and e['to'] in node_ids for e in edges)):
        raise HTTPError(0, "Every edge.from / edge.to resolves to a declared node id (the base 'endpoints exist' check).")

    # REQUIRES: Every node declares a tier/criticality_tier (missing tier is a finding, not a skip).
    if not (all(n.get('criticality_tier') for n in nodes)):
        raise HTTPError(0, "Every node declares a tier/criticality_tier (missing tier is a finding, not a skip).")

    # --- implementation ---

    # ENSURES:
    # Reports, never mutates: the checker opens artifacts read-only and creates/modifies no file under the scanned repo; prints authority line verbatim: 'proves the DECLARED model is internally consistent; does not prove the code matches the model'.
    # Absent architecture.json => exit 0 with a 'no architecture model found' note ('not checked' is distinguished from 'passed'), so /architect can adopt incrementally. Same for absent optional cross-artifacts (system-contracts.json): reported 'not checked', never 'passed'.
    # trust_zone_crossing / region_crossing / propagated carries labels are COMPUTED by the checker; an authored non-null value yields an authored-crossing-label finding (INV-fh-006).
    # Supports --scanner-version: prints chief_wiggum.hashing.scanner_version(__file__, *deps) and exits 0 with no other action — making check_architecture the fifth #184 gate whose validation record can be staleness-checked (INV-fh-005).


def code_query_orient_(inferred-binding_precision)(request):
    """#185: tighten the _path_matches_literal_segments-driven inferred facts so a single common-entity-word overlap no longer binds a file to every operation mentioning that word. Deterministic, stdlib-only, no symbol resolution (tree-sitter explicitly out of phase 1 per #159)."""
    # REQUIRES: An inferred fact requires MORE than a single common-word overlap: either the overlapping words' summed IDF weight (computed over the epic's own operations/routes — no external corpus) clears a fixed threshold, or the overlap includes an entity+verb/route-tail word combination — never one high-document-frequency word alone.
    if not (surface_inferred(pattern, rel) implies (idf_weight(overlap_words, epic_corpus) >= THRESHOLD or has_entity_and_verb(overlap_words))):
        raise HTTPError(0, "An inferred fact requires MORE than a single common-word overlap: either the overlapping words' summed IDF weight (computed over the epic's own operations/routes — no external corpus) clears a fixed threshold, or the overlap includes an entity+verb/route-tail word combination — never one high-document-frequency word alone.")

    # REQUIRES: The matcher is deterministic and stdlib-only: same epic artifacts + same file path => identical fact set and ordering across runs and platforms (IDF from the epic's own corpus, fixed tie-breaks, no randomness, no new dependencies).
    if not (orient(repo, rel) == orient(repo, rel)  # byte-identical envelope; module imports are stdlib-only):
        raise HTTPError(0, "The matcher is deterministic and stdlib-only: same epic artifacts + same file path => identical fact set and ordering across runs and platforms (IDF from the epic's own corpus, fixed tie-breaks, no randomness, no new dependencies).")

    # --- implementation ---

    # ENSURES:
    # Facts sort by the relation-tier-first rank key: every direct fact precedes every inferred fact, which precedes every measured (hotspot) fact — enforced by _rank_key's leading tier element, not by list-construction order.
    # Channel separation: inferred facts come only from the lexical matcher; measured hotspot facts come only from exact hotspots.json path membership and NEVER pass through _path_matches_literal_segments (the #187/#185 non-regression seam).


def hotspot_discovery(request):
    """#187: compose+normalize churn x complexity + reuse process.py coupling into docs/quality/hotspots.json. Consumed by code_query orient (top-decile measured fact via exact membership), /architect context, /implement review-depth escalation. NEVER gates."""
    # REQUIRES: --repo is a git repo with history; discovery reuses churn.analyze / complexity.lizard_ccn / process.analyze — it must NOT re-implement git-log parsing or coupling (INV-fh-001).
    if not (is_git_repo(repo) and coupling_source == 'scripts/quality/process.py'):
        raise HTTPError(0, "--repo is a git repo with history; discovery reuses churn.analyze / complexity.lizard_ccn / process.analyze — it must NOT re-implement git-log parsing or coupling (INV-fh-001).")

    # REQUIRES: --check mode: hotspots.json exists, its git_sha equals current HEAD, and its recorded window_days equals the value hotspot_discovery re-derives from commit dates at check time (the composer owns the window — churn.analyze has no window parameter).
    if not (check_mode implies (path.exists() and record['git_sha'] == head_sha and record['window_days'] == derived_window_days(repo))):
        raise HTTPError(0, "--check mode: hotspots.json exists, its git_sha equals current HEAD, and its recorded window_days equals the value hotspot_discovery re-derives from commit dates at check time (the composer owns the window — churn.analyze has no window parameter).")

    # --- implementation ---

    # ENSURES:
    # Deterministic: same (git_sha, window_days, normalization) => byte-identical hotspots array; ties broken by (score desc, file asc).
    # NEVER writes stable IDs: hotspots.json has no field matching ID_RE; surfaced only as a measured fact whose rank is enforced BELOW direct and inferred by _rank_key's leading relation-tier element (direct=0, inferred=1, measured=2) — never a governing contract (INV-fh-007).
    # The orient top-decile fact is EXACT on file-path identity (file in hotspots.json) — it NEVER calls _path_matches_literal_segments, and it sorts after any direct or inferred fact for the same file via the leading relation-tier rank key.


def --scanner-version_additions_(ratchet,_saas_gate,_ci_scaffold,_quality_slop_gate,_check_architecture)(request):
    """#184: add a hash-derived --scanner-version to each of the five gates (mirroring check_single_writer/check_traceability), then author + live-verify a gate-validation record for each. This ACTIVATES the stale-record auto-demotion edge for these gates. check_architecture's record is authored only after #174 freezes its CHECKS inventory (ADR-fh-06)."""
    # REQUIRES: --scanner-version prints scanner_version(__file__, *chief_wiggum_deps) to stdout and exits 0, taking NO other action (side-effect-free).
    if not (stdout == scanner_version(__file__, *deps) and exit_code == 0 and no_side_effects):
        raise HTTPError(1, "--scanner-version prints scanner_version(__file__, *chief_wiggum_deps) to stdout and exits 0, taking NO other action (side-effect-free).")

    # REQUIRES: The hashed dep list is COMPLETE, checked mechanically: for every `from chief_wiggum import X` / `import chief_wiggum.X` in the gate's source whose logic affects findings, X's module file is among the hash inputs (e.g. ratchet.py hashes hashing.py + trace_ids.py). Omitting a dep = silent staleness (INV-fh-005).
    if not (set(hash_inputs(gate)) >= {module_file(m) for m in chief_wiggum_imports(gate_source)}):
        raise HTTPError(1, "The hashed dep list is COMPLETE, checked mechanically: for every `from chief_wiggum import X` / `import chief_wiggum.X` in the gate's source whose logic affects findings, X's module file is among the hash inputs (e.g. ratchet.py hashes hashing.py + trace_ids.py). Omitting a dep = silent staleness (INV-fh-005).")

    # --- implementation ---

    # ENSURES:
    # Adding the flag changes nothing else — report-only default preserved, --gate semantics unchanged.
    # The authored record's scanner_version equals live output; validity is `check_gate_validation <gate> --format json` reporting passing==true (NEVER the default exit code, which is 0 report-only even when not validated). A gate may carry --gate in a workflow only then (INV-fh-003).
    # saas_gate & quality_slop_gate records pin a fixture/recorded target (captured response corpus / fixture band file), not a live/AI-non-deterministic dependency, so clean_corpus_runs are reproducible. Building these fixture harnesses is an explicit #184 AC and a blocker for those two records.


// Auto-generated guard clauses from formal contracts.
// Generated from formal model. Do not edit by hand.

package handlers

import "fmt"

// POST cli://scripts/run_review.py
func Run_reviewWithComments(assemble_review_prompt)(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: --ticket-context JSON parses and TicketContext.from_dict preserves EVERY key including comments (no silent drop — the #83 bug).
	if !(ctx = TicketContext.from_dict(json.loads(path.read_text())); assert 'comments' not in json_keys or ctx.comments is not None) {
		http.Error(w, "--ticket-context JSON parses and TicketContext.from_dict preserves EVERY key including comments (no silent drop — the #83 bug).", 1)
		return
	}

	// REQUIRES: Upstream ticket.json writer (the /implement skill shell) fetched and serialized comments incl. author_association via `gh issue view <n> --json comments`.
	if !(ticket_json.get('comments') is not None  # root-cause: empty upstream => field stays empty in production) {
		http.Error(w, "Upstream ticket.json writer (the /implement skill shell) fetched and serialized comments incl. author_association via `gh issue view <n> --json comments`.", 1)
		return
	}

	// --- implementation ---

	// ENSURES:
	// Prompt renders TWO labeled regions, both distinct from {{ACCEPTANCE_CRITERIA}}: 'Accepted AC amendments (authoritative-on-conflict)' — only comments passing the promotion rule — and 'Discussion/context (non-authoritative)' for everything else. The raw comment thread as a whole is NEVER labeled authoritative. Empty thread renders a placeholder.
	// Substitution stays single-pass: a body that literally contains `{{TICKET_COMMENTS}}` is never re-scanned; comment source order is preserved within each rendered region.
}


// POST cli://scripts/consult_ai.py
func Consult_aiUsageCapture(_emit_consult_telemetry)(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: The provider CLI/SDK was invoked with a usage-bearing output format (claude -p --output-format json; gemini json; vertex response.usage_metadata; codex exec JSON event stream; claude-interactive has none => usage_status 'unavailable' by construction).
	if !(adapter in USAGE_BEARING_FORMATS or usage_status == 'unavailable') {
		http.Error(w, "The provider CLI/SDK was invoked with a usage-bearing output format (claude -p --output-format json; gemini json; vertex response.usage_metadata; codex exec JSON event stream; claude-interactive has none => usage_status 'unavailable' by construction).", 0)
		return
	}

	// REQUIRES: Usage parsing is wrapped so a parse failure NEVER fails the consult itself (the consult's text output is the product; telemetry is best-effort — preserve _emit_consult_telemetry's swallow-all contract).
	if !(try: usage = parse_usage(out, err) except Exception: usage = None  # consult already returned its text) {
		http.Error(w, "Usage parsing is wrapped so a parse failure NEVER fails the consult itself (the consult's text output is the product; telemetry is best-effort — preserve _emit_consult_telemetry's swallow-all contract).", 0)
		return
	}

	// REQUIRES: Capture BOTH stdout and stderr: some CLIs print the JSON usage payload to stderr; _run_capture currently returns stdout only.
	if !(captured.stdout is not None and captured.stderr is not None) {
		http.Error(w, "Capture BOTH stdout and stderr: some CLIs print the JSON usage payload to stderr; _run_capture currently returns stdout only.", 0)
		return
	}

	// --- implementation ---

	// ENSURES:
	// emit_consult called exactly once per successful consult with the RESOLVED billed model id; the resolved name is never a bare CLI alias.
	// cost_usd is conditionally derived inside emit_consult and nowhere else (INV-fh-002): null when either token count is unknown, else exactly cost_for's value.
	// usage_status reflects the true source; both-tokens-or-null holds; tokens/cost are null (never 0, never estimated) when usage is unavailable or partial (INV-fh-011).
}


// GET cli://scripts/check_architecture.py
func Check_architecture(staticConsistency)(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: architecture.json parses as JSON and validates against architecture-schema.json (ARC-/EDG-/ASM- patterns, additionalProperties:false).
	if !(json.loads(path); validate(doc, 'architecture') == []) {
		http.Error(w, "architecture.json parses as JSON and validates against architecture-schema.json (ARC-/EDG-/ASM- patterns, additionalProperties:false).", 0)
		return
	}

	// REQUIRES: Every edge.from / edge.to resolves to a declared node id (the base 'endpoints exist' check).
	if !(all(e['from'] in node_ids and e['to'] in node_ids for e in edges)) {
		http.Error(w, "Every edge.from / edge.to resolves to a declared node id (the base 'endpoints exist' check).", 0)
		return
	}

	// REQUIRES: Every node declares a tier/criticality_tier (missing tier is a finding, not a skip).
	if !(all(n.get('criticality_tier') for n in nodes)) {
		http.Error(w, "Every node declares a tier/criticality_tier (missing tier is a finding, not a skip).", 0)
		return
	}

	// --- implementation ---

	// ENSURES:
	// Reports, never mutates: the checker opens artifacts read-only and creates/modifies no file under the scanned repo; prints authority line verbatim: 'proves the DECLARED model is internally consistent; does not prove the code matches the model'.
	// Absent architecture.json => exit 0 with a 'no architecture model found' note ('not checked' is distinguished from 'passed'), so /architect can adopt incrementally. Same for absent optional cross-artifacts (system-contracts.json): reported 'not checked', never 'passed'.
	// trust_zone_crossing / region_crossing / propagated carries labels are COMPUTED by the checker; an authored non-null value yields an authored-crossing-label finding (INV-fh-006).
	// Supports --scanner-version: prints chief_wiggum.hashing.scanner_version(__file__, *deps) and exits 0 with no other action — making check_architecture the fifth #184 gate whose validation record can be staleness-checked (INV-fh-005).
}


// GET cli://scripts/code_query.py orient
func Code_queryOrient(inferred-bindingPrecision)(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: An inferred fact requires MORE than a single common-word overlap: either the overlapping words' summed IDF weight (computed over the epic's own operations/routes — no external corpus) clears a fixed threshold, or the overlap includes an entity+verb/route-tail word combination — never one high-document-frequency word alone.
	if !(surface_inferred(pattern, rel) implies (idf_weight(overlap_words, epic_corpus) >= THRESHOLD or has_entity_and_verb(overlap_words))) {
		http.Error(w, "An inferred fact requires MORE than a single common-word overlap: either the overlapping words' summed IDF weight (computed over the epic's own operations/routes — no external corpus) clears a fixed threshold, or the overlap includes an entity+verb/route-tail word combination — never one high-document-frequency word alone.", 0)
		return
	}

	// REQUIRES: The matcher is deterministic and stdlib-only: same epic artifacts + same file path => identical fact set and ordering across runs and platforms (IDF from the epic's own corpus, fixed tie-breaks, no randomness, no new dependencies).
	if !(orient(repo, rel) == orient(repo, rel)  # byte-identical envelope; module imports are stdlib-only) {
		http.Error(w, "The matcher is deterministic and stdlib-only: same epic artifacts + same file path => identical fact set and ordering across runs and platforms (IDF from the epic's own corpus, fixed tie-breaks, no randomness, no new dependencies).", 0)
		return
	}

	// --- implementation ---

	// ENSURES:
	// Facts sort by the relation-tier-first rank key: every direct fact precedes every inferred fact, which precedes every measured (hotspot) fact — enforced by _rank_key's leading tier element, not by list-construction order.
	// Channel separation: inferred facts come only from the lexical matcher; measured hotspot facts come only from exact hotspots.json path membership and NEVER pass through _path_matches_literal_segments (the #187/#185 non-regression seam).
}


// POST cli://scripts/hotspot_discovery.py
func Hotspot_discovery(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: --repo is a git repo with history; discovery reuses churn.analyze / complexity.lizard_ccn / process.analyze — it must NOT re-implement git-log parsing or coupling (INV-fh-001).
	if !(is_git_repo(repo) and coupling_source == 'scripts/quality/process.py') {
		http.Error(w, "--repo is a git repo with history; discovery reuses churn.analyze / complexity.lizard_ccn / process.analyze — it must NOT re-implement git-log parsing or coupling (INV-fh-001).", 0)
		return
	}

	// REQUIRES: --check mode: hotspots.json exists, its git_sha equals current HEAD, and its recorded window_days equals the value hotspot_discovery re-derives from commit dates at check time (the composer owns the window — churn.analyze has no window parameter).
	if !(check_mode implies (path.exists() and record['git_sha'] == head_sha and record['window_days'] == derived_window_days(repo))) {
		http.Error(w, "--check mode: hotspots.json exists, its git_sha equals current HEAD, and its recorded window_days equals the value hotspot_discovery re-derives from commit dates at check time (the composer owns the window — churn.analyze has no window parameter).", 0)
		return
	}

	// --- implementation ---

	// ENSURES:
	// Deterministic: same (git_sha, window_days, normalization) => byte-identical hotspots array; ties broken by (score desc, file asc).
	// NEVER writes stable IDs: hotspots.json has no field matching ID_RE; surfaced only as a measured fact whose rank is enforced BELOW direct and inferred by _rank_key's leading relation-tier element (direct=0, inferred=1, measured=2) — never a governing contract (INV-fh-007).
	// The orient top-decile fact is EXACT on file-path identity (file in hotspots.json) — it NEVER calls _path_matches_literal_segments, and it sorts after any direct or inferred fact for the same file via the leading relation-tier rank key.
}


// GET cli://scripts/{ratchet,saas_gate,ci_scaffold,quality_slop_gate,check_architecture}.py --scanner-version
func --scanner-versionAdditions(ratchet,Saas_gate,Ci_scaffold,Quality_slop_gate,Check_architecture)(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: --scanner-version prints scanner_version(__file__, *chief_wiggum_deps) to stdout and exits 0, taking NO other action (side-effect-free).
	if !(stdout == scanner_version(__file__, *deps) and exit_code == 0 and no_side_effects) {
		http.Error(w, "--scanner-version prints scanner_version(__file__, *chief_wiggum_deps) to stdout and exits 0, taking NO other action (side-effect-free).", 1)
		return
	}

	// REQUIRES: The hashed dep list is COMPLETE, checked mechanically: for every `from chief_wiggum import X` / `import chief_wiggum.X` in the gate's source whose logic affects findings, X's module file is among the hash inputs (e.g. ratchet.py hashes hashing.py + trace_ids.py). Omitting a dep = silent staleness (INV-fh-005).
	if !(set(hash_inputs(gate)) >= {module_file(m) for m in chief_wiggum_imports(gate_source)}) {
		http.Error(w, "The hashed dep list is COMPLETE, checked mechanically: for every `from chief_wiggum import X` / `import chief_wiggum.X` in the gate's source whose logic affects findings, X's module file is among the hash inputs (e.g. ratchet.py hashes hashing.py + trace_ids.py). Omitting a dep = silent staleness (INV-fh-005).", 1)
		return
	}

	// --- implementation ---

	// ENSURES:
	// Adding the flag changes nothing else — report-only default preserved, --gate semantics unchanged.
	// The authored record's scanner_version equals live output; validity is `check_gate_validation <gate> --format json` reporting passing==true (NEVER the default exit code, which is 0 report-only even when not validated). A gate may carry --gate in a workflow only then (INV-fh-003).
	// saas_gate & quality_slop_gate records pin a fixture/recorded target (captured response corpus / fixture band file), not a live/AI-non-deterministic dependency, so clean_corpus_runs are reproducible. Building these fixture harnesses is an explicit #184 AC and a blocker for those two records.
}


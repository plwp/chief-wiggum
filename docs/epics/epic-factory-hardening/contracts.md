## Entity: TicketContext

The reviewable state of a ticket as seen by run_review.py. #83 bug: from_dict drops `comments`, so reviewers judge diffs against stale body ACs. Modeled with an explicit authority lattice (codex): body+acceptance_criteria = authoritative baseline; comments = append-only observed context; amendments = the subset of comments PROMOTED to authoritative AC changes by a defined rule; discussion = non-authoritative context. Amendments alter AC PRESENTATIONALLY in the rendered prompt only — the stored acceptance_criteria field is never rewritten (INV-fh-009/010).

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| number | int|null | optional | gh issue number | null when the ticket is synthetic/local |
| title | string | always | gh issue title | — |
| body | string | always | gh issue body at author time | AUTHORITATIVE baseline spec. Class = authoritative-requirement in the provenance lattice. |
| acceptance_criteria | []string | optional | extracted-or-explicit AC currently in force | The AC baseline. NEVER mechanically merged with comment text and NEVER rewritten by amendments (amendments compose presentationally in the prompt — INV-fh-010). |
| comments | []TicketComment | optional | gh issue comments via `gh issue view <n> --json comments` | TicketComment = {id, url, author (gh login), author_association (OWNER|MEMBER|COLLABORATOR|CONTRIBUTOR|NONE|...), created_at (ISO8601), body}. Append-only, chronological, order-preserving. NEVER re-sorted/de-duplicated/merged (INV-fh-009). Class = observed-context. from_dict accepts a list of dicts OR a legacy list of strings (a string degrades to TicketComment(body=..., author='', author_association='NONE', created_at='', id=None, url=None)). |
| amendments | []Amendment | optional | derived from comments by the promotion rule (ADR-fh-02) | Amendment = {comment_id, url, author, author_association, created_at, ac_block}. Promotion predicate (mechanical, implementable from stored fields): (author == issue author OR author_association in (OWNER, MEMBER, COLLABORATOR)) AND body contains an explicit 'AC:' block. Supersession is DETERMINISTIC: amendments apply in created_at ascending order; where two amend the same AC item, the latest created_at wins; equal timestamps tie-break by comment id ascending. Class = authoritative-requirement (comment-derived). Presentational only (INV-fh-010). |
| discussion | []TicketComment | optional | comments minus amendments | Class = non-authoritative-context. Rendered under its own labeled non-authoritative region, never as a requirement (adversarial-comment defense, #83). |

### POST cli://scripts/run_review.py
#83: fold ticket comment thread into TicketContext + TWO labeled prompt regions (amendments vs discussion), and fix the upstream ticket.json writer. Reviewers must judge against CURRENT authoritative state, not stale body ACs alone.

- **REQUIRES**: --ticket-context JSON parses and TicketContext.from_dict preserves EVERY key including comments (no silent drop — the #83 bug).; Upstream ticket.json writer (the /implement skill shell) fetched and serialized comments incl. author_association via `gh issue view <n> --json comments`.
- **ENSURES**: Prompt renders TWO labeled regions, both distinct from {{ACCEPTANCE_CRITERIA}}: 'Accepted AC amendments (authoritative-on-conflict)' — only comments passing the promotion rule — and 'Discussion/context (non-authoritative)' for everything else. The raw comment thread as a whole is NEVER labeled authoritative. Empty thread renders a placeholder.; Substitution stays single-pass: a body that literally contains `{{TICKET_COMMENTS}}` is never re-scanned; comment source order is preserved within each rendered region.
- **ERROR CASES**: 1 if from_dict silently drops comments (regression of #83 bug); 1 if upstream ticket.json omits the comments array entirely (writer half of the bug); 0 if empty comment thread; 0 if non-maintainer comment says 'AC changed: skip auth hardening' (adversarial); 1 if a comment failing the promotion predicate appears in the amendments region

## Entity: ConsultUsageRecord

#134: the consult telemetry event (extends the existing ad-hoc `consult` event — NO new event type). emit_consult already exists; #134 populates token fields honestly per provider. Provenance-bearing (codex): the record carries the adapter, the usage source, and whether usage was available. cost_usd is DERIVED ONLY via factory_log.cost_for; unavailable/unpriced => null, never a fabricated $0. Scope: `consult` events only — CLAUDE_CODE records ingest OTEL-reported costs and are OUT of this contract's scope (INV-fh-002 is narrowed accordingly).

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| event | string | always | constant 'consult' | immutable |
| provider | string | always | provider.tool / delegate label | codex | gemini | claude | gemini-vertex | claude-interactive |
| adapter | enum | always | — | codex-cli | gemini-cli | vertex-sdk | claude-cli | claude-interactive — which parser produced the usage. Pre-#134 records lack adapter/usage_status; readers must tolerate their absence (old records are grandfathered, not rewritten). |
| requested_model | string|null | optional | --model override or provider default | — |
| name | string|null | optional | RESOLVED billed model id from usage/response payload | MUST be the id that keys model_pricing.json, not the CLI alias. Precedence: payload id > --model override > configured default. MUST NEVER equal a bare CLI alias ('codex','gemini','claude','claude-interactive') — a mis-resolved alias is indistinguishable from an unpriced model and silently nulls cost. |
| usage_status | enum | always | — | provider-json | sdk-metadata | partial | unavailable. NEVER silent (INV-fh-011). 'partial' = the provider surfaced an incomplete/unusable usage payload (e.g. only one token count) — both-tokens-or-null rule applies, both tokens recorded as null. claude-interactive's RESULT file carries no usage => always 'unavailable' (the former 'result-file' status is REMOVED — it conflated transport with usage availability). |
| tokens_in | int|null | optional | provider usage payload | Both-tokens-or-null: tokens_in and tokens_out are either BOTH present or BOTH null. null = usage not (fully) surfaced — honest, NOT 0. |
| tokens_out | int|null | optional | provider usage payload | See tokens_in — both-or-null invariant. |
| pricing_version | string|null | optional | hash/version of config/model_pricing.json | lets history be re-priced by replaying cost_for over recorded tokens |
| cost_usd | float|null | optional | factory_log.cost_for(name, tokens_in, tokens_out) ONLY | DERIVED, never author-supplied. null when model unpriced OR tokens null (INV-fh-002). Never silently $0. |
| repo | string|null | optional | call site | — |
| ticket | string|null | optional | call site | SFR: currently never threaded through _emit_consult_telemetry — thread a --ticket or cost-by-ticket stays permanently empty |

### POST cli://scripts/consult_ai.py
#134: each consult_* switches to a usage-bearing output format, parses that CLI's usage shape + resolved model id, threads it through consult_provider, then emit_consult(provider, model, tin, tout, repo=..., ticket=...). Cost computed only inside emit_consult.

- **REQUIRES**: The provider CLI/SDK was invoked with a usage-bearing output format (claude -p --output-format json; gemini json; vertex response.usage_metadata; codex exec JSON event stream; claude-interactive has none => usage_status 'unavailable' by construction).; Usage parsing is wrapped so a parse failure NEVER fails the consult itself (the consult's text output is the product; telemetry is best-effort — preserve _emit_consult_telemetry's swallow-all contract).; Capture BOTH stdout and stderr: some CLIs print the JSON usage payload to stderr; _run_capture currently returns stdout only.
- **ENSURES**: emit_consult called exactly once per successful consult with the RESOLVED billed model id; the resolved name is never a bare CLI alias.; cost_usd is conditionally derived inside emit_consult and nowhere else (INV-fh-002): null when either token count is unknown, else exactly cost_for's value.; usage_status reflects the true source; both-tokens-or-null holds; tokens/cost are null (never 0, never estimated) when usage is unavailable or partial (INV-fh-011).
- **ERROR CASES**: 0 if usage absent/unparseable for a provider; 0 if provider surfaces only ONE token count (partial payload); 1 if usage payload printed to stderr only while the parser reads stdout only; 1 if record.name is a bare CLI alias (mis-resolution); 0 if codex billed model id unresolved / unpriced row in model_pricing.json; 1 if any consult path computes/stores a dollar figure itself

## Entity: ArchitectureModel

#174: docs/system/architecture.json — a C4-flavored DECLARED system model. check_architecture.py runs STATIC consistency checks only. Authority (verbatim): 'proves the DECLARED model is internally consistent; does not prove the code matches the model' (reflexion deferred to #171). Nodes = ARC- deployables (external:true vendors carry ASM- refs); edges = EDG- connectors. Availability semantics (codex): `hard` is specifically an AVAILABILITY dependency — distinct from `carries` (data class) and from trust-zone crossing.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| nodes | []ARC-node | always | architecture.json (declared once — system-contracts.json may reference but must not create nodes, INV-fh-008) | node = {id ^ARC-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}$, name, kind: service|worker|db|queue|bucket|cron|external, repo|null, external, trust_zone: public|dmz|internal|restricted, region|null, failure_domain, criticality_tier: tier-1|tier-2|tier-3, emits: [telemetry binding names], status: active|deprecated|retired, asm_refs} |
| node.tier/criticality_tier | enum | always | — | REQUIRED on every node — a missing tier is a FINDING, never a skipped node (else a node silently opts out of the tier-inversion check). |
| node.asm_refs | []ASM-id | after external == true AND reached by a hard connector | — | non-empty required for external nodes on a hard edge — an unlabelled vendor is the whole point of ASM. |
| edges | []EDG-connector | always | — | edge = {id ^EDG-...$, from ARC-, to ARC- (both MUST resolve to a declared node), protocol, sync|async, criticality: hard|soft, on_failure:{fallback,degrade_to}, carries:[data-class labels], auth:{mechanism,tenant_scoped}, timeout_ms, ordering, dlq, active} |
| edge.criticality (hardness) | enum | always | — | hard = caller fails if callee down (AVAILABILITY dependency ONLY). A low-tier logging sink may carry sensitive data without being an availability dependency; a low-tier auth provider may be availability-critical without carrying payloads — different edge meanings (codex). |
| edge.carries | []data-class | optional | — | data-class lattice public < internal < pii < secret < official-sensitive so label-propagation is a monotone comparison, not string equality. |
| edge.trust_zone_crossing | enum|null | optional | DERIVED by check_architecture from from.trust_zone->to.trust_zone | NEVER authored — the schema permits the null placeholder, but ANY authored non-null value is a FINDING (INV-fh-006). |
| edge.region_crossing | bool|null | optional | DERIVED by check_architecture from from.region != to.region | NEVER authored — any authored non-null value is a FINDING (INV-fh-006). |

### GET cli://scripts/check_architecture.py
#174: STATIC declared-model consistency. Report-only by default (exit 0 even WITH findings), --gate opts into blocking (exit 1 on findings), exit 2 usage errors only. Emits emit_gate('check_architecture', pass|fail, caught=n) best-effort. Prints the authority line verbatim every run. Follow check_budget_tree's bespoke _validate_value walker so findings carry a JSON path. Per ADR-fh-06 this checker is the FIFTH #184 gate: it ships --scanner-version and receives a validation record in the same pass, after its CHECKS inventory freezes.

- **REQUIRES**: architecture.json parses as JSON and validates against architecture-schema.json (ARC-/EDG-/ASM- patterns, additionalProperties:false).; Every edge.from / edge.to resolves to a declared node id (the base 'endpoints exist' check).; Every node declares a tier/criticality_tier (missing tier is a finding, not a skip).
- **ENSURES**: Reports, never mutates: the checker opens artifacts read-only and creates/modifies no file under the scanned repo; prints authority line verbatim: 'proves the DECLARED model is internally consistent; does not prove the code matches the model'.; Absent architecture.json => exit 0 with a 'no architecture model found' note ('not checked' is distinguished from 'passed'), so /architect can adopt incrementally. Same for absent optional cross-artifacts (system-contracts.json): reported 'not checked', never 'passed'.; trust_zone_crossing / region_crossing / propagated carries labels are COMPUTED by the checker; an authored non-null value yields an authored-crossing-label finding (INV-fh-006).; Supports --scanner-version: prints chief_wiggum.hashing.scanner_version(__file__, *deps) and exits 0 with no other action — making check_architecture the fifth #184 gate whose validation record can be staleness-checked (INV-fh-005).
- **ERROR CASES**: 0 if schema violation / parse error (finding); 0 if dangling edge endpoint (from/to not a declared node); 0 if retired/deprecated node has an ACTIVE inbound/outbound edge; 0 if external node reached by a hard connector has empty asm_refs; 0 if hard-dependency path from a tier-1 node down through a lower-criticality node; 0 if carries x trust_zone x region label-propagation violation without an ASM/waiver; 0 if budget-tree chains / telemetry bindings reference an undeclared ARC-/EDG-; 0 if a node is missing its tier; 0 if an authored (non-null) trust_zone_crossing/region_crossing value; 2 if usage error (bad flags/paths)

## Entity: CodeQueryOrientBinding

#185: code_query orient's artifact-derived (inferred) binding of contracts.json operation paths / ui-spec routes to files. The lexical heuristic over-matches on common entity names ('provider' word-matching dozens of operations). The fix stays stdlib and deterministic: IDF-style weighting of word overlap across the epic's OWN operations/routes, or a required entity+verb word combination. Rank ordering across fact tiers is ENFORCED, not conventional: code_query._rank_key gains a LEADING relation-tier key (direct=0, inferred=1, measured=2) BEFORE the exact key, so direct always sorts above inferred, and inferred above measured (hotspot) facts.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| relation | enum | always | — | direct | inferred | measured. 'inferred' facts come from the lexical matcher; 'measured' facts (the #187 hotspot tier) come ONLY from exact hotspots.json path membership — the two channels never mix (INV-fh-012, INV-fh-007). |
| rank_key | tuple | always | code_query._rank_key | Leading element = relation tier (direct=0, inferred=1, measured=2), placed BEFORE the exact-match key. This is the mechanical guarantee behind 'inferred ranked below direct'. |
| exact | boolean | always | — | True only for direct annotations / code_locations matches and for measured hotspot facts (exact path identity); always False for inferred lexical matches. NOTE: exact=True on a measured fact does NOT promote it above inferred — the leading relation-tier key dominates. |

### GET cli://scripts/code_query.py orient
#185: tighten the _path_matches_literal_segments-driven inferred facts so a single common-entity-word overlap no longer binds a file to every operation mentioning that word. Deterministic, stdlib-only, no symbol resolution (tree-sitter explicitly out of phase 1 per #159).

- **REQUIRES**: An inferred fact requires MORE than a single common-word overlap: either the overlapping words' summed IDF weight (computed over the epic's own operations/routes — no external corpus) clears a fixed threshold, or the overlap includes an entity+verb/route-tail word combination — never one high-document-frequency word alone.; The matcher is deterministic and stdlib-only: same epic artifacts + same file path => identical fact set and ordering across runs and platforms (IDF from the epic's own corpus, fixed tie-breaks, no randomness, no new dependencies).
- **ENSURES**: Facts sort by the relation-tier-first rank key: every direct fact precedes every inferred fact, which precedes every measured (hotspot) fact — enforced by _rank_key's leading tier element, not by list-construction order.; Channel separation: inferred facts come only from the lexical matcher; measured hotspot facts come only from exact hotspots.json path membership and NEVER pass through _path_matches_literal_segments (the #187/#185 non-regression seam).
- **ERROR CASES**: 0 if file shares ONLY one common entity word (e.g. 'provider') with an operation path; 0 if file lexically resembles a hotspot path but is absent from hotspots.json; 1 if an inferred or measured fact ranks above a direct fact for the same file

## Entity: HotspotRecord

#187: docs/quality/hotspots.json — an explicitly OBSERVATIONAL, rebuildable artifact. NO stable IDs, referenced by NO @cw-trace link, surfaced by code_query only as a measured fact ranked below direct AND below inferred via the leading relation-tier rank key (INV-fh-007). Opus correction: #187 REUSES the existing coupling engine (scripts/quality/process.py) + churn (churn.py) + complexity (complexity.py) — NOT a new coupling engine (that would be a second co-change definition, violating INV-fh-001).

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| schema | string | always | constant 'hotspots/1' | — |
| generated_at | string | always | ISO8601 wall clock at generation | — |
| git_sha | string | always | HEAD the scan ran against | THE staleness key — the analogue of --scanner-version for a data artifact. --check compares this to current HEAD. |
| window_days | int | always | DERIVED by hotspot_discovery from commit dates (max commit date - min commit date of the analyzed range) — churn.analyze has NO window parameter, so the composer computes and RECORDS the window; never taken from datetime.now() | a floating wall-clock window makes the same SHA drift. --check compares git_sha AND window_days. |
| no_merges | bool | always | churn.py default True, recorded for reproducibility | — |
| normalization | string | always | — | how churn & complexity were scaled to 0..1 (e.g. 'max') |
| hotspots | []hotspot | always | — | hotspot = {file, symbol?, score = norm_churn*norm_complexity, norm_churn, norm_complexity, churn, commits, complexity, decile (10=top-decile orient fact), coupled_with:[{file,confidence,co_changes}] from process.py, trend}. Ordered (score desc, file asc) for a stable tie-break. |
| inputs | object | always | {churn: scripts/quality/churn.py, complexity: scripts/quality/complexity.py, coupling: scripts/quality/process.py} | provenance of the reused engines — coupling is computed ONLY by process.py (INV-fh-001). |

### POST cli://scripts/hotspot_discovery.py
#187: compose+normalize churn x complexity + reuse process.py coupling into docs/quality/hotspots.json. Consumed by code_query orient (top-decile measured fact via exact membership), /architect context, /implement review-depth escalation. NEVER gates.

- **REQUIRES**: --repo is a git repo with history; discovery reuses churn.analyze / complexity.lizard_ccn / process.analyze — it must NOT re-implement git-log parsing or coupling (INV-fh-001).; --check mode: hotspots.json exists, its git_sha equals current HEAD, and its recorded window_days equals the value hotspot_discovery re-derives from commit dates at check time (the composer owns the window — churn.analyze has no window parameter).
- **ENSURES**: Deterministic: same (git_sha, window_days, normalization) => byte-identical hotspots array; ties broken by (score desc, file asc).; NEVER writes stable IDs: hotspots.json has no field matching ID_RE; surfaced only as a measured fact whose rank is enforced BELOW direct and inferred by _rank_key's leading relation-tier element (direct=0, inferred=1, measured=2) — never a governing contract (INV-fh-007).; The orient top-decile fact is EXACT on file-path identity (file in hotspots.json) — it NEVER calls _path_matches_literal_segments, and it sorts after any direct or inferred fact for the same file via the leading relation-tier rank key.
- **ERROR CASES**: 0 if no commits / not a repo; 1 if --check and git_sha mismatch OR missing file; 1 if hotspots.py re-implements co-change/coupling instead of importing process.analyze; 0 if shallow clone / dirty worktree / renamed or ignored files

## Entity: GateValidationRecord

EXISTING record (templates/gate-validation-record-schema.json), consumed by check_gate_validation.py. This epic (#184) does NOT change its schema — it extends COVERAGE to FIVE gates: ratchet, saas_gate, ci_scaffold, quality_slop_gate, plus check_architecture as the FIFTH (#174's new checker, per ADR-fh-06 — its CHECKS inventory freezes first, then #184 authors its record in the same pass). Each gains a hash-derived --scanner-version so records can't go silently stale. 'Passing' is ALWAYS read via `check_gate_validation <gate> --format json` with `passing == true` (or --gate exit) — NEVER the default exit code, which is 0 in report-only mode even when not validated. Only the fields #184 touches are contracted here.

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| gate | string | always | the gate name; record.gate must equal the checked gate | — |
| scanner_version | string | always | live `<gate>.py --scanner-version` output | #184 ADDS this flag to the five gates. Hash-derived via chief_wiggum.hashing.scanner_version(__file__, *deps) — NEVER a hand-set constant (INV-fh-005). Must equal live output or the record is stale. |
| authority_boundary | object | always | {proves, artifact, assumptions[]} | each gate states exactly what its record proves |
| seeded_defect_trials | []trial | always | — | one genuinely-passing 'fire' trial per CLAIM/CHECK in the gate's inventory; required seed classes: direct + evasion-omission + config-indirection + sampling-gap always, evasion-concurrency unless concurrency_applicable:false, instrumentation-deleted if telemetry_dependent:true |
| clean_corpus_runs | []run | always | — | passed:true AND findings==0 AND non-zero coverage. SFR: saas_gate (live base-url) and quality_slop_gate (AI band) have NON-DETERMINISTIC targets — building their fixture/recorded-target harnesses is an EXPLICIT #184 acceptance criterion and a blocker for those two records (codex+opus agree this is the slip risk); a record validated against prod/live-band can never be re-verified. |
| ratchet_record_id | string | always | rec-\d+ in docs/quality/ratchet-journal.jsonl | journaled via `ratchet record --event gate-validation --ref <gate>`; hash-chain corroborated |
| status | enum | always | — | passed | ... ; validity is read via check_gate_validation --format json passing==true (never raw default exit code) |

### GET cli://scripts/{ratchet,saas_gate,ci_scaffold,quality_slop_gate,check_architecture}.py --scanner-version
#184: add a hash-derived --scanner-version to each of the five gates (mirroring check_single_writer/check_traceability), then author + live-verify a gate-validation record for each. This ACTIVATES the stale-record auto-demotion edge for these gates. check_architecture's record is authored only after #174 freezes its CHECKS inventory (ADR-fh-06).

- **REQUIRES**: --scanner-version prints scanner_version(__file__, *chief_wiggum_deps) to stdout and exits 0, taking NO other action (side-effect-free).; The hashed dep list is COMPLETE, checked mechanically: for every `from chief_wiggum import X` / `import chief_wiggum.X` in the gate's source whose logic affects findings, X's module file is among the hash inputs (e.g. ratchet.py hashes hashing.py + trace_ids.py). Omitting a dep = silent staleness (INV-fh-005).
- **ENSURES**: Adding the flag changes nothing else — report-only default preserved, --gate semantics unchanged.; The authored record's scanner_version equals live output; validity is `check_gate_validation <gate> --format json` reporting passing==true (NEVER the default exit code, which is 0 report-only even when not validated). A gate may carry --gate in a workflow only then (INV-fh-003).; saas_gate & quality_slop_gate records pin a fixture/recorded target (captured response corpus / fixture band file), not a live/AI-non-deterministic dependency, so clean_corpus_runs are reproducible. Building these fixture harnesses is an explicit #184 AC and a blocker for those two records.
- **ERROR CASES**: 1 if --scanner-version has a side effect or nonzero exit; 1 if incomplete dep list (script hashed but a finding-affecting chief_wiggum import omitted); 1 if workflow reads check_gate_validation's default exit code as validation success; 1 if saas_gate/quality_slop record validated against prod URL / live AI band; 1 if check_architecture record authored before #174 freezes its CHECKS inventory

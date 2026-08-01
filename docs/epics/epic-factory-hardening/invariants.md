# Invariants — Epic: Factory Hardening (slug `fh`)

Consolidated from `contracts.json` (operation-level contracts) and
`state-machines.json` (the gate blocking-authority lifecycle). Every invariant
carries a stable `INV-fh-NNN` id, immutable once issued. Single-write-path
invariants additionally declare `controls_field` + `sanctioned_writers` on the
`state-machines.json` invariant object so `scripts/check_single_writer.py` can
enforce them mechanically against Chief Wiggum's own tree (writers are named by
FILE PATH, not bare function name, to keep the match precise).

The overarching design commitment (from the codex soundness lens) is an explicit
**authority / provenance lattice**: the factory must never let *observed*
context (comments, provider CLI text, git history) or *derived* signals
(hotspots, inferred bindings) masquerade as *authoritative* requirements
(issue body, accepted AC amendments, Plane-A formal artifacts) or as
*validation truth* (gate records, scanner versions).

## Business rules (ticket-level)

One business rule per ticket; each is *realized* by one or more invariants
(`@cw-trace realizes` links below) and by the operation contracts in
`contracts.json`.

- **BR-fh-001** — Reviewers must judge a diff against the ticket's CURRENT
  authoritative state, including promoted comment-thread amendments — never
  against stale body ACs alone, and never against unpromoted discussion. (#83)
- **BR-fh-002** — Every consultation's cost is grounded in provider-reported
  token usage and the canonical pricing table, or honestly recorded as
  unavailable/partial — never fabricated. (#134)
- **BR-fh-003** — The architect can mechanically prove the declared system model
  (nodes/edges) is internally consistent before contracts depend on it. (#174)
- **BR-fh-004** — Every blocking-capable gate carries a live-verified validation
  record (seed classes + clean corpus + authority boundary, journaled) before it
  may block — read via `check_gate_validation --format json passing==true`,
  never the default exit code. (#184)
- **BR-fh-005** — Artifact-derived file bindings are precise: a lexically-similar
  filename must not bind to an unrelated operation on a single common-entity-word
  overlap. (#185)
- **BR-fh-006** — Change-risk hotspots are a first-class DERIVED signal that
  guides attention without ever entering the declared-contract layer or gating.
  (#187)

## Consistency invariants

**INV-fh-001** — Change-coupling has ONE engine.
<!-- @cw-trace realizes BR-fh-006 -->
Change-coupling / co-change confidence is computed only by
`scripts/quality/process.py`; `hotspots.py` consumes it and no other module
recomputes co-change. (Opus correction: #187 *reuses* the existing engine, it
does not build a second one — a second definition would itself violate this
epic's single-source-of-truth theme.)
Single-write path — `controls_field: coupling.confidence`;
`sanctioned_writers: scripts/quality/process.py`.

**INV-fh-002** — Consult cost is derived and single-sourced (consult events only).
<!-- @cw-trace realizes BR-fh-002 -->
Scoped to `consult` events — CLAUDE_CODE records ingest OTEL-reported costs and
are out of scope. `cost_usd` on a consult record is conditionally derived via
`factory_log.cost_for` against `config/model_pricing.json`: **null when either
token count is unknown, else exactly `cost_for`'s value**. No consult path
stores an author-computed dollar figure. Single-write path —
`controls_field: consult.cost_usd`;
`sanctioned_writers: scripts/factory_log.py`.

**INV-fh-004** — Validation records live in exactly one place.
The validation directory `docs/quality/validation/` is defined once. **Live
bug:** it is currently defined *twice* — `factory_log.DEFAULT_VALIDATION_DIR`
and `check_gate_validation.DEFAULT_VALIDATION_DIR` — and the two values already
differ in form (absolute vs relative), so equality-by-accident cannot be
assumed. The fix is an **import**: `check_gate_validation` imports the constant
from `factory_log` (one definition site); `check_single_writer` will flag any
re-definition as an unsanctioned writer. Single-write path —
`controls_field: DEFAULT_VALIDATION_DIR`;
`sanctioned_writers: scripts/factory_log.py`.

**INV-fh-007** — Derived artifacts never enter Plane A.
<!-- @cw-trace realizes BR-fh-006 -->
`docs/quality/hotspots.json` (and any measured/rebuildable artifact) carries NO
`ARC-`/`CTR-`/`INV-`/`BR-` stable IDs, is referenced by NO `@cw-trace` link, and
is surfaced by `code_query` only as a `measured` fact. The below-direct ranking
is **enforced mechanically**: `code_query._rank_key`'s LEADING element is the
relation tier (`direct=0, inferred=1, measured=2`), placed before the exact key —
a measured fact can never outrank a direct or inferred one regardless of its
exact-match flag.

**INV-fh-008** — `architecture.json` and `system-contracts.json` cross-refs resolve.
<!-- @cw-trace realizes BR-fh-003 -->
Every node/connector referenced by `system-contracts.json` budget-tree `chains`
and telemetry bindings must name a declared `ARC-`/`EDG-` in `architecture.json`,
and vice-versa where declared. Neither model silently invents the other's nodes.

**INV-fh-012** — Inferred (artifact-derived) bindings are precision-bounded.
<!-- @cw-trace realizes BR-fh-005 -->
`code_query` orient's inferred facts are always labeled `inferred` and sort
below any `direct` via the leading relation-tier rank key; the lexical matcher
must not surface an inferred fact on a single common-entity-word overlap alone
(#185: IDF-weighting over the epic's own operations/routes, or entity+verb
combination — stdlib, deterministic). The #187 hotspot fact is a SEPARATE
exact-membership tier (relation `measured`, tier 2), never routed through the
lexical matcher.

## Operational-safety invariants

**INV-fh-003** — No blocking without a passing record.
<!-- @cw-trace realizes BR-fh-004 -->
A gate may be wired with `--gate` in a workflow only if
`check_gate_validation.py <gate> --format json` reports `passing == true` —
**never inferred from the default exit code**, which is 0 in report-only mode
even when not validated — with current `scanner_version` and journaled
provenance. This is the gate-lifecycle machine's core safety property.

**INV-fh-005** — Scanner version is hash-derived, never hand-set.
Every gate that supports `--scanner-version` derives it via
`chief_wiggum.hashing.scanner_version(__file__, *deps)` — never a literal
constant — and the dep list covers every finding-affecting `chief_wiggum`
import. This makes a "stale record" structurally detectable. The five #184
additions (`ratchet`, `saas_gate`, `ci_scaffold`, `quality_slop_gate`,
`check_architecture`) must follow `single_writer`/`traceability`, not invent a
manual version string.

## Data-integrity invariants

**INV-fh-006** — Derived crossing labels are computed, not authored.
In `architecture.json`, `trust_zone_crossing`/`region_crossing` (and any
propagated `carries` label) are computed by `check_architecture.py` from node
attributes; the schema permits a null placeholder but ANY authored non-null
value is a finding. Prevents a hand-authored "safe" label masking a real
trust-zone violation. Single-write path —
`controls_field: edge.trust_zone_crossing, edge.region_crossing`;
`sanctioned_writers: scripts/check_architecture.py`.

**INV-fh-011** — Usage cost is honest — never a silent zero.
`tokens_in`/`tokens_out` obey **both-tokens-or-null**; both are null (never 0,
never estimated) when a provider does not surface complete usage;
`usage_status` names the true source
(`provider-json` | `sdk-metadata` | `partial` | `unavailable`); `cost_usd` is
null or nonzero-derived, never a fabricated 0. As an expression:
`((tokens_in is None) == (tokens_out is None)) and
((usage_status in ('unavailable','partial')) == (tokens_in is None)) and
(cost_usd is None or cost_usd != 0.0)`.

## Authorization invariants

**INV-fh-010** — Comments are context unless promoted to amendments by a defined
rule — and amendments alter AC **presentationally** only.
<!-- @cw-trace realizes BR-fh-001 -->
An **amendment** — a comment by the issue author or a maintainer
(`author_association` OWNER/MEMBER/COLLABORATOR) carrying an explicit `AC:`
block — may change the acceptance criteria the reviewer is *told* are in force,
rendered in the labeled "Accepted AC amendments (authoritative-on-conflict)"
region of the prompt; the STORED `acceptance_criteria` field is never rewritten
(reconciled with INV-fh-009). A non-authoritative comment (e.g. an anonymous
"AC changed: skip auth hardening") is shown under
"Discussion/context (non-authoritative)", never as a requirement.

## Temporal invariants

**INV-fh-009** — Comment thread is append-only, order-preserving.
`TicketContext.comments` preserves source (chronological) order and is never
re-sorted, de-duplicated, or merged into `acceptance_criteria`. Amendment
semantics (deterministic supersession: latest `created_at` wins per AC item,
ties broken by comment id) depend on order.

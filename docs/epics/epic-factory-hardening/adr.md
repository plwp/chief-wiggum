# ADR — Epic: Factory Hardening

Status: Accepted (architect stage). Context: this epic hardens Chief Wiggum's
own quality loop across six tickets (#83, #134, #174, #184, #185, #187). The two
consultations were deliberately lens-diverse — a soundness critic (codex) and a
repo-grounded analyst (opus) — and are reconciled here by union. Each decision
notes AI consensus/divergence.

> **No ui-spec.** This epic touches no frontend surface — every deliverable is a
> Python CLI, a JSON artifact schema, or a prompt-assembly path. `ui-spec.json`
> is intentionally omitted; the design-fidelity gate does not apply.

---

## ADR-fh-01 — Adopt an explicit authority / provenance lattice

**Decision.** Every fact the factory ingests is classified into one of four
authority classes, and no downstream decision may promote a lower class to a
higher one implicitly:

1. **authoritative-requirement** — issue body, accepted AC amendments, Plane-A
   formal artifacts (contracts/invariants/state machines).
2. **observed-context** — ticket comments, provider CLI output, git history.
3. **derived-signal** — hotspots, inferred `code_query` bindings.
4. **validation-claim** — gate validation records, scanner versions.

Comments are **context** unless promoted to amendments by a defined rule
(ADR-fh-02). Derived signals influence prioritization and reviewer attention but
never requirements, stable IDs, or gate truth (INV-fh-007). A "hard" architecture
edge is specifically an **availability dependency**, distinct from what an edge
`carries` (data class) and from a trust-zone crossing — three different edge
meanings that the tier-inversion, label-propagation, and ASM checks read
separately.

**AI consensus/divergence.** This is codex's core objection ("the design can be
wrong even if each ticket is implemented correctly") adopted wholesale. Opus did
not name the lattice but independently reached each of its consequences
(structured `TicketComment`, `usage_status`, derived-only `cost_usd`, hotspot
exact-membership). No divergence — the two lenses converged.

## ADR-fh-02 — Comments-vs-amendments promotion rule (concrete)

**Decision.** `TicketContext` distinguishes `comments` (the raw append-only,
order-preserving log — INV-fh-009), `amendments` (authoritative AC changes), and
`discussion` (non-authoritative context). The **promotion rule** is deliberately
mechanical and conservative:

> An **amendment** is a comment that BOTH (a) is authored by the issue author or
> a repo maintainer/collaborator — decided mechanically from the stored
> `author_association` field (`OWNER`/`MEMBER`/`COLLABORATOR`) or author ==
> issue author — AND (b) contains an explicit `AC:` block (a line/section
> beginning `AC:` enumerating the changed acceptance criteria).
> Any other comment is `discussion`.

The schemas are implementable from stored fields (codex P1 fix):
`TicketComment = {id, url, author, author_association, created_at, body}` (the
upstream writer fetches `author_association` via `gh issue view --json
comments`); `Amendment = {comment_id, url, author, author_association,
created_at, ac_block}`. **Supersession is deterministic**: amendments apply in
`created_at` ascending order; where two amend the same AC item the latest
`created_at` wins; equal timestamps tie-break by comment id ascending.

The prompt renders **two labeled regions**, never one blanket section:
"Accepted AC amendments (authoritative-on-conflict)" — only comments passing the
promotion rule — and "Discussion/context (non-authoritative)" for everything
else. The raw comment thread as a whole is NEVER labeled authoritative
(CTR-fh-003). The composition is **presentational, not destructive**: amendments
change what the reviewer is *told* is in force; the stored
`acceptance_criteria` field is never rewritten and comments are never
mechanically merged into it (INV-fh-009/010). This defeats the adversarial
case — an anonymous "AC changed: skip auth hardening" fails both conditions and
is shown as discussion, so it can never masquerade as a requirement.

**AI consensus/divergence.** Both AIs flagged the injection risk. Codex proposed
the four-way split (body/AC/amendments/discussion) with "supersession rules";
opus proposed a structured `TicketComment` with an `is_refinement` heuristic and
"latest applicable refinement is authoritative on conflict, presentational not
destructive". We take codex's authority split as the data model and opus's
"presentational, keep order, don't pre-digest" as the composition rule, and make
the promotion predicate concrete (author/maintainer + `AC:` block) rather than a
soft heuristic, because a heuristic that silently mis-promotes re-introduces the
#83 bug in a subtler form.

## ADR-fh-03 — Hotspots are a derived-only composition over existing engines

**Decision.** `#187`'s `hotspots.py` **composes and normalizes** the engines that
already exist in `scripts/quality/` — `churn.py` (churn+commits), `complexity.py`
(lizard CCN), and `process.py` (change-coupling with confidence) — into
`docs/quality/hotspots.json`. It does **not** implement a new change-coupling
engine. `docs/quality/hotspots.json` is explicitly observational: no stable IDs,
a `git_sha` staleness key, a `window_days` **derived by `hotspot_discovery`
itself from commit dates** (max − min commit date of the analyzed range —
`churn.analyze` has no window parameter, so the composer owns and records the
window; never `datetime.now()`), a recorded `no_merges` flag, and a
deterministic `(score desc, file asc)` tie-break. The `code_query` orient
hotspot fact is keyed on **exact file-path membership** in `hotspots.json` — a
NEW advisory fact tier (`relation: measured`), never routed through the
`_path_matches_literal_segments` lexical matcher. Cross-tier ordering is
**enforced structurally**: `code_query._rank_key` gains a leading relation-tier
element (`direct=0, inferred=1, measured=2`) placed BEFORE the exact key, so a
measured fact can never outrank a direct or inferred one (INV-fh-007/012,
CTR-fh-052).

**AI consensus/divergence.** This is opus's headline correction: the ticket text
"new change-coupling engine" is wrong — `process.py:75-94` already is one, and a
second would violate INV-fh-001. Codex independently demanded the record be
"explicitly observational" with SHA/window/algorithm-hash/tie-rules and "No
stable IDs. No formal artifact references." Both insisted the orient fact be a
"separate advisory section", not another governing-fact path. Full consensus;
opus supplied the specific reuse targets.

## ADR-fh-04 — Gate blocking-authority lifecycle + stale fail-mode = fail-to-report-only

**Decision.** Formalize the gate lifecycle as the epic's one state machine
(`state-machines.json`): `unknown → report_only → validated → blocking`, with
`stale` and `demoted` branches. The safety property (INV-fh-003): `blocking` is
unreachable without a passing, corroborated record. When a record goes **stale**
(live `--scanner-version` no longer matches, or the journal hash-chain breaks),
the system **fails to report-only** — a stale-while-**blocking** gate is
auto-demoted, never left silently blocking and never treated as fully-validated;
a stale-while-merely-**validated** record (never wired) downgrades to
`report_only` via its own edge — the `previous_authority` context field decides
which (codex P0: without it a non-wired stale record was stuck).

Further lifecycle rules adopted from the validation round:
- **Guard semantics**: every guard reads validity via
  `check_gate_validation <gate> --format json` `passing == true` — NEVER the
  default exit code, which is 0 in report-only mode even when not validated
  (codex P0).
- **Un-wiring is not demotion**: `blocking → validated` on intentional
  `unwire_gate`.
- **Record loss/regression**: `validated → report_only` and
  `blocking → demoted` on `record_missing_or_invalid`; `report_only → unknown`
  on `record_removed`.
- **Emission detail**: stale/record-loss demotions emit the GENERIC
  `emit(DEMOTION, gate=..., details='stale')` event — `factory_log.emit_demotion`
  requires a `seed_class`, which a non-escape demotion does not have; the
  escape-driven demotion keeps the real signature
  `demotion_check(escape.missed_by, escape.seed_class)`.
- `report_only` means "the record itself does not pass"; a validated-but-unwired
  gate is in `validated`, not `report_only`.

**Justification for fail-to-report-only (not fail-closed).** A stale record means
we no longer *know* the gate is sound against the current scanner — its
certified catches may no longer hold. Fail-closed (keep blocking) would let an
unverified gate block work on the strength of an out-of-date proof, which is
exactly the "gate the operator learns to `--force` past" failure the
gate-rollout doctrine exists to prevent. Report-only preserves the gate's
*signal* while removing its *authority* until a human re-derives the record. This
matches the existing demotion semantics (a production escape demotes to
report-only) and the "a NEW gate ships report-only" rollout rule.

**AI consensus/divergence.** Both proposed the machine. Codex named the `stale`
state explicitly and framed the fork ("fail-open/report-only or fail-closed, but
it cannot silently remain blocking"); opus grounded the transitions in existing
code (`check_gate_validation.py:292-298` provenance check + `factory_log`
demotion) and noted #184's `--scanner-version` additions are what *activate* the
stale edge for the five gates. We adopt codex's six-state shape (adding `stale`
as a first-class state) and opus's guard-to-code grounding, and make the explicit
fail-mode choice codex left open.

## ADR-fh-05 — usage_status honesty; cost is derived-only

**Decision.** The consult record carries a `usage_status`
(`provider-json` | `sdk-metadata` | `partial` | `unavailable`). The former
`result-file` status is **removed** — it conflated transport with usage
availability and contradicted "claude-interactive's RESULT file has no usage";
claude-interactive is simply `unavailable`. **Partial usage** (a provider
surfaces only one token count) is `usage_status: 'partial'` under a
**both-tokens-or-null** rule: tokens_in/out are either both present or both
null — never a half-priced record. When usage is unavailable or partial,
tokens and `cost_usd` are **null** — never `0`, never estimated (INV-fh-011).
`cost_usd` is computed ONLY inside `factory_log.cost_for` from
`config/model_pricing.json` and recorded tokens; **INV-fh-002 is scoped to
`consult` events only** — CLAUDE_CODE records ingest OTEL-reported costs and
recomputing them is out of this epic's scope (codex P1). Model-id resolution to
the *billed* id is a real #134 sub-task: until codex's `gpt-*` id is resolved,
its consults record tokens with `cost_usd: null` (the honest degradation), and
the resolved `name` must never be a bare CLI alias — a mis-resolved alias is
indistinguishable from an unpriced model (CTR-fh-013).

**Per-provider degradation matrix (accepted):** `claude -p` (json) → full cost;
`gemini-vertex` (`usage_metadata`, currently discarded) → full cost once read;
`gemini` CLI (json may not expose usage) → tokens or null; `codex exec`
(needs JSON event stream + id resolution) → tokens+cost if resolved else tokens
with null cost; `claude-interactive` (RESULT file has no usage) → always null.
Usage parsing must also capture **stderr** (some CLIs print the payload there)
and must be wrapped so a parse failure never fails the consult, and must thread
`--ticket` (not just `repo`) or cost-by-ticket stays permanently empty.

**AI consensus/divergence.** Full consensus. Codex: "`cost_usd: null`, never
silently `$0`"; opus supplied the exact provider-by-provider matrix, the
stderr-capture and `--ticket`-threading SFRs, and confirmed the null-priced codex
row. No divergence.

## ADR-fh-06 — check_architecture is the FIFTH #184 gate; CHECKS frozen first

**Decision.** `check_architecture` is a **first-class fifth #184 gate**, not a
side mention: #174's contract requires it to ship `--scanner-version`
(hash-derived, CTR-fh-026) alongside the four original gates, and #184 authors
its validation record + seeds in the same pass. This resolves the four-vs-five
inconsistency codex flagged (contracts said four; ADR/traceability implied a
fifth) in the direction consistent with this ADR. Sequencing:
`check_architecture.py` (#174) exposes its check set as a frozen
`CHECKS =` list in the module (one canonical seed class per check: dangling
endpoint, retired-node edge, unlabelled external, tier inversion, label
propagation, undeclared cross-ref, missing tier, authored-crossing-label). #184
authors the `check_architecture` validation record **after** #174 has frozen
that list, and a retroactive test asserts one genuinely-passing `fire` trial per
entry in `CHECKS`. Both tickets are in W2, so this dependency is made explicit —
the record must not race the checker (an early-authored record is itself a
contract error case).

**Justification.** `check_gate_validation.required_seed_classes` only mandates the
*generic* set (`direct` + omission/config-indirection/sampling-gap, +concurrency
unless waived); a *check-specific* omission would slip through if #184 authored
against a draft list. Freezing the inventory and testing one-seed-per-check closes
that gap. Separately, whichever ticket first touches it must fix the dual
`DEFAULT_VALIDATION_DIR` constant (INV-fh-004) and add a test asserting both
modules resolve the same path — most likely #184.

**AI consensus/divergence.** Opus raised this integration risk with the sequencing
and the retroactive-test remedy; codex raised the same dependency abstractly
("a validation record for check_architecture.py is unsound until the checker's
authority boundary and finding classes are stable"). Consensus; opus supplied the
mechanism.

## ADR-fh-07 — check_architecture authority is declared-model consistency only

**Decision.** `check_architecture.py` prints, verbatim, every run:
*"proves the DECLARED model is internally consistent; does not prove the code
matches the model."* Absent `architecture.json` → exit 0 with a "no architecture
model found" note (report-only adoption path), distinguishing "not checked" from
"passed". Report-only by default; gateable in `/architect` soundness only after
the #168 protocol and a #184 validation record. The reflexion/extraction
conformance machinery stays deferred to #171.

**AI consensus/divergence.** Consensus. Both stressed the authority boundary; both
warned the real drift risk is *declared-vs-declared* (budget-tree naming a callee
`architecture.json` doesn't declare — INV-fh-008), while *declared-vs-code* drift
is out of scope and the authority line must say so plainly. Opus added the
absent-model graceful-degradation and the missing-`tier`-is-a-finding rule so a
node can't silently opt out of the inversion check.

---

## Deferred / tracked

- Reflexion (code-vs-model conformance) for `architecture.json` — **#171**.
- `#185`'s deeper precision (tree-sitter/symbol resolution) — explicitly out of
  `code_query` phase 1 (#159); this epic ships IDF/entity+verb weighting only.
- `saas_gate` / `quality_slop_gate` fixture harnesses (non-deterministic
  targets — live URL, AI band) are an **explicit #184 acceptance criterion and a
  blocker** for those two records (codex+opus agree this is the slip risk), and
  materially more work than `ratchet`/`ci_scaffold`.
- Pre-#134 consult records lack `adapter`/`usage_status`; readers tolerate their
  absence (grandfathered, not rewritten).

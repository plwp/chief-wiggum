# Integration test specification — Dynamic DAG

Every case uses deterministic fixtures, injected clock/RNG, no ordinary-test
network, and asserts both result and journal evidence. “ACn” means the nth
checkbox in the named issue, in issue order.

## IT-dag-001 — Shared Git resource safety (#376)

Create two sibling worktrees in one temporary repository. Assert worker contract
and both portable/harness prompts forbid stash; a guarded stash attempt fails
with `SHARED_GIT_REF_UNSAFE`; WIP commits preserve both diffs; fan-out admission
rejects unproven repo-global-ref isolation. Covers the three proposed-fix items.

## IT-dag-002 — Contract and semantic fixture corpus (#384, AC1–AC8)

Validate every record/schema version; table-test every named illegal transition;
reject a schedulable cycle while accepting cycles made only of non-scheduling
relations; reject every automatic use of an approval-required operation; reject
terminal mutation and accept its compensating event; grep schemas/fixtures for
provider/model names; round-trip one historical dependency block through the
exact six-field wave oracle including exit codes; verify `docs/dag-contract.md`
documents three records, schedulable subset and closed reason codes.

## IT-dag-003 — Transactional journal and replay (#385, AC1–AC9)

Seeded mutation sequences compare incremental and replayed schedule/audit hashes
and ready sets; two processes race one base revision and exactly one commits;
stale bases are journaled; same-key/same-payload returns the original decision and
same-key/different-payload fails; snapshot continuation equals full replay and a
doctored snapshot fails closed; process-kill transaction tests cover partial tail
states; cycle proposal reports its path before application; frozen real-epic wave
projection matches all six fields and exits; embedded and sidecar resolver modes
store no hard-coded path.

## IT-dag-004 — Evidence and admission adversary matrix (#386, AC1–AC12)

Cases 1–7 reject automatic AC/contract edits, verifier weakening, approved intent
dependency removal, aggregate-window budget splitting, foreign/dangling evidence,
terminal/evidence rewrites and instruction-shaped inert prose. Case 8 drives every
collector/reason-code pair to its expected proposal/admission. Case 9 records loud
tracker/preflight degradation. Case 10 suppresses duplicates and turns A→B→A into
a liveness failure. Case 11 inspects journaled rejection detail. Case 12 queues a
privileged proposal and records exact human approval followed by revalidation and
successful admission (never an approval loop).

## IT-dag-005 — Continuous scheduler (#387, AC1–AC11)

With fake clock/workers/providers: admit a discovered edge while unrelated workers
continue; gate failure creates bounded retry lineage and blocks dependants only;
path/shared-resource collision serializes with replay-stable order; outage reroutes
or blocks only affected nodes; quiescence emits non-zero liveness failure and full
blocking set; replay preserves dispatch/ready/hash sequence; randomized incremental
ready sets equal full recomputation; expiry creates a retry and fences old output;
cross-node token substitution and output-after-expiry-before-reclaim are rejected;
kill at every dispatch-outbox boundary then reconcile without double-accepted work;
every blocked-safe node reaches wake, cancel or diagnosed liveness failure under
logical time; journal failure invokes `--static`, which matches both exact projection
and wave-barrier dispatch/gating/failure semantics; paired real-epic traces report
barrier-stall and ticket-outcome differences without requiring dynamic superiority.
This verifies INV-dag-010, INV-dag-023 and INV-dag-026.

## IT-dag-006 — Provider routing (#388, AC1–AC10)

Routine work stays cheapest-capable; two objective gate failures justify escalation
with evidence/cost delta; healthy objective signals ignore self-reported confidence;
outage follows configured fallback order then blocks; ceiling prevents escalation;
missing independent verifier fails closed; same pinned inputs reproduce selection
and tie-break; every decision contains chosen/alternatives/factors/trigger/cost/
latency; grep workflow/routing code for vendor/model literals; static routing matches
the current provider roster on a frozen real-ticket fixture. An end-to-end seam test
routes an external-preview capability decision through the real scheduler/adapter
interface to a simulated worker, verifies the selected provider and independence group
are honored, accounts usage, fences output, and invokes an independent verifier without
slug-specific logic.

## IT-dag-007 — Candidate fan-out and Git outbox (#389, AC1–AC9)

Correct candidate always beats seeded defect; provider identity swap preserves the
winner with journaled seed; verifier modification/provider-family collision fails;
second promotion loses CAS; all-fail creates no winner and bounded retry/failure;
preflight and mid-run budget ceilings prevent/stop fan-out with evidence; missing
stash/ref/port/db isolation refuses fan-out; replay reproduces winner/elimination/
superseded set; retained loser bundle contains diff, gates and cost. Crash Git outbox
at each boundary and prove one logical promotion plus idempotent expected-old-SHA ref
update. Attempt success alone cannot enqueue Git publication, and scorer inputs are
identity-blind. This verifies INV-dag-024.

## IT-dag-008 — Explainability, exports and controls (#390, AC1–AC9)

Every node state has stable-ID explanation; accepted/rejected decisions expose actor,
code, evidence, base and budget; router and “why provider” share the same decision
projection; unavailable/unscanned render explicitly; fixed revision exports are
byte-identical, schema-valid and GitHub-Mermaid-valid; revision diff equals intervening
operations; every control is a human mutation; pause/cancel waits for durable boundary
and releases/fences lease; seeded API-key strings, home paths and tool tokens are
absent from every format with redactor version/policy pinned.

## IT-dag-009 — Pre-registered experiment (#391, AC1–AC9)

Refuse a run whose ratchet-bound pre-registration does not predate it; validate each
manifest’s corpus, factorial cell, roster, seed, budget, verifier and environment;
content-address and ratchet-bind immutable raw tasks; require class/risk slices with N
and intervals including degenerate gaps; render all registered cost/quality cells;
fail reporting when negative/protocol-violation results are absent; record contamination
cutoff/exclusions/limits; execute static rollback and record go/hold/rollback; reject a
public claim missing N, interval or caveat. Missing/non-randomized cells suppress causal
language. The runner reproduces the preregistered blocked-randomization assignment log,
interleaves temporal blocks, rejects assignment drift, and suppresses causal language
when assignment or resolved-identity compliance fails. Execute at least one dynamic arm
end-to-end through the real scheduler/adapter seam with ratchet, usage and verifier
evidence. Run Ox only as external-preview-tier with context/token circuit breaker. This
verifies INV-dag-025.

## IT-dag-010 — Generic OpenRouter coding worker (#392, AC1–AC7)

Fake harness proves Responses-provider invocation and workspace-write isolation and
searches argv/prompt/log/metadata/environment for the seeded key; reject main checkout
before invocation; table-test missing key, unsafe helper boundary, unsupported tools,
timeout, nonzero exit, missing result and malformed stream to stable exclusive ERROR;
success atomically yields result/DONE/log/schema-valid metadata; retain the dated live
read-only probe with slug, Codex version, tool success, latency and usage; grep workflow
logic for the Ox slug; run existing prompt-only OpenRouter consult tests unchanged.

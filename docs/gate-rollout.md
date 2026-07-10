# Gate rollout: report-only before blocking

A **gate** is a script that can hard-fail a workflow (`/architect`, `/implement`,
`/implement-wave`, `/close-epic`) — traceability, single-writer, unresolved-markers,
ratchet, the SaaS NFR gate. A gate that is *noisy on real code is worse than no gate*:
it hard-fails a workflow on false positives, and the operator learns to `--force` past
it — which erodes trust in **every** gate.

This is not hypothetical. `check_single_writer.py` (the single-writer gate) shipped wired
as a blocker and false-positived heavily on the first real polyglot repo (comments, Go
`:=`, in-memory assignments, TS interface fields — see the precision fix in the git
history for `#93`). The lesson generalizes.

## The rule

**Every new hard-fail gate ships report-only first, and is validated on a real,
already-shipped repo before it is wired as a blocker.**

Concretely:

1. **Report-only is the default; blocking is an explicit opt-in.** The gate scripts
   already enforce this shape: they print their findings and **exit 0** unless you pass
   `--gate` (`check_single_writer.py`, `check_traceability.py`, `ratchet.py`) or the
   equivalent blocking flag (`saas_gate.py --gate`). Running a gate **without** its
   `--gate` flag is report-only mode — use it during bring-up.

2. **Prove precision on real code before promoting to blocking.** Run the new gate
   report-only against at least one real, already-shipped target repo and inspect the
   finding set. An acceptable false-positive rate is the bar for promotion — attach the
   dry-run findings to the change that wires the gate into a workflow with `--gate`.

3. **Log what a gate does NOT cover.** If a gate has known blind spots or residual false
   positives (e.g. the single-writer gate cannot, by regex alone, tell an audit-log
   `bson.M{"field": …}` from a provider `$set`), document them next to the gate and in
   its `--help`, and prefer precise metadata (`sanctioned_writers`, `--exclude`) over
   silently accepting noise.

## Why report-only is already the mechanism

None of the gate scripts fail on their own — a workflow *chooses* to make a gate
blocking by passing `--gate`. So "report-only" needs no new flag: it is what a gate does
when a workflow (or a human, during validation) runs it without `--gate`. When adding a
gate to a workflow, land the report-only invocation first (surface findings in the run
output), and only switch it to `--gate` once step 2 is satisfied.

## Checklist for adding a gate

- [ ] Gate exits 0 in its default (no-`--gate`) mode and prints findings.
- [ ] Dry-run against a real shipped repo; false-positive rate is acceptable.
- [ ] Known limitations documented in the script docstring/`--help` and its `docs/` page.
- [ ] Wired into the workflow report-only first; promoted to `--gate` in a follow-up that
      cites the dry-run.

## Ledger

Gates and their rollout status. A gate stays report-only until a dry-run on a real shipped
repo justifies promoting it to `--gate`.

| Gate | Script | Workflow | Status |
|------|--------|----------|--------|
| Traceability soundness/coverage | `check_traceability.py --gate` | `/architect`, `/close-epic` | blocking |
| Single-writer | `check_single_writer.py --gate` | `/architect`, `/close-epic` | blocking (after `#93` precision fix) |
| Unresolved markers | `check_unresolved.py` | `/implement-wave`, `/close-epic` | blocking |
| Ratchet | `ratchet.py check` | `/implement`, `/implement-wave`, `/close-epic` | blocking |
| SaaS NFR | `saas_gate.py --gate` | `/close-epic` (SaaS) | blocking |
| AI-slop signals (code survival + duplication) | `quality_slop_gate.py` | `/close-epic` | **report-only / new** (`--gate` exists, off by default; promote after a dry-run shows the bands don't false-positive on real shipped repos) |

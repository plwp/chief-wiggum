# Budget trees: typed NFR budgets with sound tail arithmetic

Chief Wiggum can check that a latency/throughput/spend budget decomposition is
internally consistent — mechanically, instead of trusting a prose diagram that
adds up percentiles like they were plain numbers. This is the first
system-layer gate (#164): a **budget tree**.

Pilot use case: a ~800ms mouth-to-ear voice-agent budget, decomposed into
endpointing / LLM TTFT / TTS TTFB / transport — metrics LiveKit/pipecat already
emit.

## Why not just sum the numbers?

**Percentiles do not sum.** `P(A > 300ms) = 5%` and `P(B > 300ms) = 5%` does
NOT imply `P(A+B > 700ms) <= 10%`, and it does NOT imply the reverse either —
summing p95s is neither sound nor complete. The correlated-tails
counterexample (from the issue, encoded as
`test_correlated_tails_counterexample_naive_passes_union_bound_flags` in
`tests/test_check_budget_tree.py`): two children each bounded at p95=300ms
against a 700ms parent — a naive `sum(bound) <= parent.bound` check
(300+300=600<=700) reports PASS and looks safe, while the same children's own
tail-probability budgets (`alpha=0.05` each) sum to `0.10`, twice the parent's
declared `alpha=0.05` — the union bound shows the parent's own tail guarantee
is oversubscribed, something the naive sum can never see.

## The construction

Each budget node is a tail-probability allocation: `P(node's observed value >
node.bound) <= node.alpha`. A child set is consistent with its parent under
the default **union-bound** arithmetic iff:

```
sum(child.alpha) <= parent.alpha
sum(child.bound) + parent.headroom <= parent.bound
```

This is coherent **without assuming independence** between children — it's a
true union bound, not a sum of the underlying random variables. A tree may
opt into `arithmetic: "naive"` (plain sum-of-bounds, ignoring alpha), but that
mode is **WARN-only and can never gate a workflow**, regardless of `--gate`.

Two structural preconditions keep the sound check sound:

- **Alphas must be declared.** In a union-bound tree, a non-leaf node without
  its own `alpha`, or any child/residual without `alpha`, is a **structure
  finding** — missing alphas must never silently degrade the union-bound
  check into a naive sum-of-bounds. (Naive trees don't use alpha, so they
  don't require it.)
- **Kind/unit homogeneity.** Every child and residual must carry the same
  `kind` and `unit` as its parent — a ms parent must not sum usd/tokens
  children. A mismatch is a **structure finding** and the meaningless mixed
  sums are skipped.

## Coverage: the residual bucket

Every node that declares `children` **must** also declare a `residual` child
— an explicit unaccounted-budget bucket. Omitting it is a structure finding.
This makes the accounting exhaustive: a budget tree can't silently drop the
gap between what's declared and what's allocated.

## Assumption references

Vendor-stage children can cite `asm_refs`: `{id: ASM-..., evidence, ref}`.
`evidence: sla-doc` or `live-probe` renders as **covered**; `evidence:
justified` renders as a **documented waiver** — a distinct, non-finding
status, not silently equal to "covered". Missing or invalid evidence is a
finding.

## Timeout monotonicity

Optional `chains: [{id, hops: [{caller, callee, timeout_ms}]}]` declare
cross-service timeout chains, ordered outermost to innermost. The check:
`hop[i].timeout_ms > hop[i+1].timeout_ms` for every adjacent pair — an outer
caller's timeout must exceed its nested callee's, else the callee can still be
in flight when the caller has already given up. Violations are **WARN-only**
(never gateable) and always carry a note that retries/hedging multiply
worst-case occupancy beyond what a single-hop timeout chain models.

## Two modes

- **static** (default): schema validation + well-formedness + arithmetic +
  monotonicity. `--gate` fails (exit 1) on **schema/structure/arithmetic
  findings only** — monotonicity and naive-arithmetic are advisory and never
  gate.
- **`--measured <file>`**: evaluate declared bounds against a k6 summary
  export (`{"metrics": {name: {...p95...}}}`) or a flat `{metric: {p95:
  ...}}` export. Every node gets a status:

  | Status | Meaning |
  | --- | --- |
  | `held` | an observation exists and satisfies the declared bound |
  | `breached` | an observation exists and **exceeds** the declared bound |
  | `no_observations` | a `telemetry_ref` is declared but the metric was never observed — missing from the export, or present with an explicit zero count. A measurement gap: **a finding, never a pass.** |
  | `unbound` | no `telemetry_ref` declared — the node is not bound to any metric. A spec gap, deliberately distinct from `no_observations`, and never a pass. |

  Measured mode is **evidence-only, permanently**: it never exits non-zero,
  even with `--gate` — environment variance means a measured latency claim
  should never hard-block CI (see `docs/gate-rollout.md`).

## Authority boundary

Every report — text or JSON — states exactly what was proven:

```
static:   "static mode proves budget-declaration consistency, not runtime latency"
measured: "measured mode reports observations from <source>; not a proof of runtime behaviour"
```

Neither mode ever claims to prove runtime behavior holds in production.

## Schema

`templates/formal-models/system-contracts-schema.json` — `BUD-` stable-ID
nodes: `{id, kind: latency|throughput|spend, unit, bound, alpha, headroom,
telemetry_ref, children, residual, asm_refs}`. Same tree arithmetic applies to
all three `kind`s.

The schema is **enforced, not just documentation**: the checker walks the
schema's `required`/`enum`/`pattern`/`minimum`/`maximum`/`additionalProperties`
keywords against the document (stdlib, no jsonschema dependency) and reports
violations as `schema`-category findings — gateable in static mode like any
other structure finding.

## The checker

```bash
# static mode: structure + union-bound arithmetic + monotonicity
python3 scripts/check_budget_tree.py docs/system/system-contracts.json --format json
python3 scripts/check_budget_tree.py docs/system/system-contracts.json --gate   # hard-fail on findings

# measured mode: evaluate against k6/telemetry export (never gates)
python3 scripts/check_budget_tree.py docs/system/system-contracts.json --measured k6-summary.json
```

Exit codes: `0` ok/report-only, `1` `--gate` violation (static mode only), `2`
usage error. Ships **report-only** per the gate-rollout doctrine
(`docs/gate-rollout.md`) — not yet wired as a blocker into `/architect` or
`/close-epic`.

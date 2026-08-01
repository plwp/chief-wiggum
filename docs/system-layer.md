# System layer: the declared architecture model (and what it doesn't prove)

Chief Wiggum can check that a *declared* system architecture is internally
consistent — mechanically, instead of trusting a hand-drawn C4 diagram nobody
re-derives once it drifts. This is `#174`'s contribution to the system layer
that `#164` (budget trees) and `#165` (infra drift) started: `docs/system/architecture.json`,
checked by `scripts/check_architecture.py`.

## The model: nodes and edges, C4-flavored

`architecture.json` (schema: `templates/formal-models/architecture-schema.json`)
declares two things:

- **Nodes** (`ARC-` stable IDs) — deployables (`service`/`worker`/`db`/`queue`/`bucket`/`cron`)
  and external/vendor dependencies (`external: true`, e.g. an LLM API, a TTS
  vendor, Stripe). Each node carries a `trust_zone`, an optional `region`, a
  `failure_domain`, a `criticality_tier` (`tier-1`/`tier-2`/`tier-3`), the
  telemetry bindings it `emits`, and a `status` (`active`/`deprecated`/`retired`).
- **Edges** (`EDG-` stable IDs) — connectors between declared nodes: `protocol`,
  `mode` (`sync`/`async`), `criticality` (`hard`/`soft`), `on_failure`, `carries`
  (data-class labels), `auth`, `timeout_ms`, `ordering`, `dlq`.

The worked example — a voice agent (`client → gateway → STT/LLM/TTS externals`,
each vendor carrying an `ASM-` assumption reference) — lives at
`templates/formal-models/examples/voice-agent-architecture.json`, alongside a
companion `voice-agent-system-contracts.json` for the cross-artifact check
below. Both check clean.

## Three edge meanings that are NOT the same thing

A single edge has three logically independent properties, read by three
different checks. Conflating any two of them is a common design-review bug
this model exists to catch mechanically:

1. **`criticality` (hard/soft)** — an AVAILABILITY dependency only: `hard`
   means the caller fails if the callee is down. A low-tier logging sink may
   `carries: [pii]` without being availability-critical; a low-tier auth
   provider may be `hard` (availability-critical) without carrying any
   payload at all.
2. **`carries`** — a data-class label, from the monotone lattice
   `public < internal < pii < secret < official-sensitive`. This is a
   classification of *what flows*, independent of whether the edge is a hard
   dependency.
3. **`trust_zone_crossing` / `region_crossing`** — whether the edge crosses a
   trust boundary or a data-residency boundary. These two fields are
   **COMPUTED ONLY**, never authored: the schema permits the `null`
   placeholder, but any authored non-null value is itself a finding
   (`INV-fh-006`) — a hand-written "safe" crossing label is exactly the kind
   of thing that could mask a real trust-zone violation.

## What `check_architecture.py` proves — and what it deliberately doesn't

Every report — text or JSON — states this verbatim, every run:

> proves the DECLARED model is internally consistent; does not prove the code
> matches the model

**Declaring** a system model is cheap. Only the **extraction/reflexion**
machinery — actually inspecting running code or infrastructure and comparing
it against the declaration — is expensive, and that work is *deliberately
deferred* to `#171`. This checker never reads source code, containers, or
infra state; it only reasons over `architecture.json` (and, optionally,
`system-contracts.json` for cross-artifact checks). A clean `check_architecture.py`
run tells you the *design* hangs together — it says nothing about whether the
*deployed system* still matches that design. Treat a clean report as "the
blueprint is sound", not as "the building matches the blueprint".

## The eight consistency checks (`CHECKS` — frozen per ADR-fh-06)

`check_architecture.CHECKS` is a **frozen tuple**, one canonical seed class
per consistency rule. It freezes BEFORE `#184` authors this gate's
validation record (a retroactive test asserts one genuinely-passing `fire`
trial per entry — closing the gap where a check-specific omission could slip
past `#184`'s only-generic `required_seed_classes` set). Adding a new rule
means adding a new tuple entry, never silently folding it into an existing
one.

| Check | What it catches |
| --- | --- |
| `dangling-endpoint` | An edge's `from`/`to` does not resolve to a declared node. |
| `retired-node-edge` | An ACTIVE edge still touches a `retired` node. |
| `unlabelled-external` | An `external: true` node reached by a `hard` edge has no `asm_refs`. |
| `tier-inversion` | A tier-1 node's hard-availability-dependency path reaches (directly, or by passing THROUGH) a lower-criticality node. |
| `label-propagation` | An edge `carries` a data class into a `trust_zone`/`region` its target forbids — declared-graph only, NO taint analysis. A valid `asm_refs` waiver on the same edge downgrades this to a documented waiver, not a silent pass. |
| `undeclared-cross-ref` | `system-contracts.json` budget-tree `chains`/`telemetry_ref`s name an `ARC-`/`EDG-`/binding this model never declared (`INV-fh-008`). |
| `missing-tier` | A node has no `criticality_tier` — reported as a FINDING, never a silently-skipped node (else a node could opt itself out of the tier-inversion check by omission). |
| `authored-crossing-label` | `trust_zone_crossing`/`region_crossing` was authored non-null instead of left for the checker to derive (`INV-fh-006`). |

## Cross-artifact consistency with `system-contracts.json` (`INV-fh-008`)

Every node/connector `system-contracts.json`'s budget-tree `chains` and
telemetry bindings reference must name a declared `ARC-`/`EDG-` in
`architecture.json`, and vice versa where declared — neither model may
silently invent the other's nodes. Pass the budget-tree doc with
`--system-contracts`:

```bash
python3 scripts/check_architecture.py docs/system/architecture.json \
  --system-contracts docs/system/system-contracts.json --format json
```

Without `--system-contracts`, this leg is reported `not_checked` — **never**
conflated with "passed". The same distinction applies to the model itself:
an absent `architecture.json` exits `0` with a "no architecture model found"
note, so `/architect` can adopt this incrementally without retroactively
failing every existing product.

## Report-only by default; exit-code semantics

```bash
python3 scripts/check_architecture.py docs/system/architecture.json --format text
python3 scripts/check_architecture.py docs/system/architecture.json --gate   # hard-fail on findings
python3 scripts/check_architecture.py --scanner-version                     # hash-derived version, no other action
```

- Exit `0`: report-only default (findings printed, never blocks) — this
  includes a malformed/unparseable `architecture.json`: a parse or schema
  error is a FINDING, not a usage error.
- Exit `1`: `--gate` was passed AND findings exist.
- Exit `2`: reserved for genuine USAGE errors (bad flags, an unreadable
  `--schema` path). Never for a property of the model being checked.

Per `docs/gate-rollout.md`, `check_architecture.py` ships report-only and is
**not yet wired as a blocker** into `/architect` or `/close-epic` — per
ADR-fh-07, gating requires the `#168` gate-validation protocol plus a passing
`#184` validation record, which in turn requires this checker's `CHECKS`
inventory to have frozen first (ADR-fh-06). `/architect` runs it for
visibility at design time only.

## `--scanner-version`

Hash-derived (`chief_wiggum.hashing.scanner_version`) over this module's
source plus every `chief_wiggum` dependency whose logic affects findings —
never a hand-set constant (`INV-fh-005`). This is what makes
`check_architecture` the fifth `#184` gate whose validation record can be
mechanically staleness-checked once that record exists.

## Deferred: reflexion / conformance (`#171`)

The declared-vs-declared consistency this checker proves is a different,
cheaper question than declared-vs-**code**. Whether the code actually
implements the edges and nodes this file declares — via static extraction
(imports, RPC clients, queue producers/consumers) or dynamic reflexion
(observed call graphs vs. declared graph) — is `#171`'s deliberately deferred
scope. Nothing in `check_architecture.py` reads source, containers, or infra
state; building that extraction/comparison machinery is real, separate work,
and the authority line above exists specifically so a clean report is never
mistaken for that stronger guarantee.

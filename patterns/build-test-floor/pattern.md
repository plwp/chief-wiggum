# Pattern: Language-Agnostic Build/Test Floor

- **Category:** gate
- **Trust class:** the floor definition is a protected path (the pre-condition every deploy/merge stands on)
- **Status:** specified (spec complete; `scaffold/` not yet built — see [chief-wiggum#135](https://github.com/plwp/chief-wiggum/issues/135))

## What it is

The **pre-condition gate** everything else stands on: one command that builds,
tests, and lints the whole repo — every language in it — and that a human runs
locally byte-for-byte the same as CI runs it. "Green locally" *predicts* "green in
CI" because they are the **same command**, not two drifting definitions of "passing."
Nothing merges or deploys without it.

Why a pattern and not "just add a CI workflow": the value is in the *invariants* that
make the floor trustworthy — the local mirror (so the feedback loop is fast and CI
holds no surprises), fail-closed-on-missing-tool (so a skipped step can't read as a
pass), polyglot coverage (so the Go half isn't gated while the TS half rots), and
the deploy pre-condition ([`deployment-release`](../deployment-release) stands on
this). Miss the mirror and developers debug in CI; miss fail-closed and a broken
gate silently greenlights.

## When to apply

Every repo. It's the floor `deployment-release` and the improvement loop both
require. The only variation is the **trigger policy** (see zero-cost-until-opt-in) —
whether CI runs on every push or on manual dispatch to save minutes on a
cost-sensitive private repo — but the *local mirror* is non-negotiable regardless.

## Mechanism — generic components

- **One command, run identically local and in CI.** A single entrypoint
  (`make lint && make test`, a `pre-merge-check` script) is the whole gate; CI's job
  is to *run that same command*, not to re-specify the checks. This is what makes
  "passes locally" mean something.
- **Auto-detecting / polyglot.** The floor covers **every** language present (set up
  each toolchain, run each language's build+lint+test), so a multi-language repo
  can't have one half gated and the other unchecked.
- **Fail-closed — no silent skip.** Any step failing, or a required tool being
  missing, **fails the floor** — it never degrades to "skipped, therefore green." A
  gate that can silently no-op is worse than no gate.
- **Pre-condition for merge and deploy.** CI runs the floor on every pull request /
  pre-merge; no deploy proceeds without it (the floor is `deployment-release`'s
  first step). The floor is a *protected path*: workers can't weaken it to get their
  change through.
- **Zero-cost-until-opt-in (optional).** For a cost-sensitive private repo, CI can
  trigger on **manual dispatch** (`workflow_dispatch`) instead of every push to save
  Actions minutes — the local mirror still gives every developer the full gate for
  free, so the floor holds even when auto-CI is throttled.

## Invariant cluster

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-BTF-001` | One entrypoint is the whole gate, and CI runs *that same command* — so "green locally" predicts "green in CI". | chief-wiggum `Makefile` (`lint`/`test`) == `.github/workflows/ci.yml` (`run: make lint` / `make test`) |
| `INV-BTF-002` | Polyglot: every language in the repo is set up and gated (build + lint + test), not just one. | chief-wiggum `ci.yml` (sets up Python **and** Node; `make lint` = ruff + py_compile + the CW gates) |
| `INV-BTF-003` | Fail-closed: any step failing or a required tool missing fails the floor — never a silent skip. | `make` step-failure semantics; each gate exits non-zero on violation |
| `INV-BTF-004` | Pre-condition for merge/deploy: CI runs the floor on every PR; no deploy proceeds without it. | chief-wiggum `ci.yml` (`on: pull_request`); `deployment-release` `INV-DRL` floor-gated |
| `INV-BTF-005` | *(optional)* Zero-cost-until-opt-in: CI triggers on manual dispatch for cost-sensitive private repos; the local mirror still gives the full gate for free. | dogeared-coach `ci.yml` (`workflow_dispatch`-only to save private-repo Actions minutes) |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `floor_cmd` | yes | The single local entrypoint CI also runs (`make lint && make test`, `pre-merge-check`). |
| `languages` | yes | Toolchains set up + gated (each gets build + lint + test). |
| `ci_trigger` | no (default `on-push-and-pr`) | `on-push-and-pr` or `manual-dispatch` (zero-cost-until-opt-in). |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `local_ci_divergence` | down | changes green locally but red in CI, or vice versa (target 0 — same command). |
| `silent_skip_incidents` | down | floor steps that no-op'd instead of failing (target 0). |
| `unfloored_merges` | down | merges/deploys that bypassed the floor (target 0). |

## Relationship to other patterns

- **`deployment-release`** — the floor is that pattern's explicit pre-condition
  (`floor_cmd`); no stage/promote runs without it.
- **`improvement-loop`** — the floor is the deterministic pass/fail the ratchet
  high-water mark is built on; the loop can't fix-forward without a trustworthy floor.
- **The factory's own [`check_cw_standards`](../../scripts/check_cw_standards.py)** —
  an instance of this pattern's "gates are tested / no silent skip" discipline turned
  on CW itself.

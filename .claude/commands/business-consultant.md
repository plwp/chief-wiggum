# Business Consultant — Unit Economics & Pricing-Model Fit

Produces a product's **unit economics + pricing-model recommendation** from what
Chief Wiggum already knows about it: its adopted registry patterns
(`docs/patterns/adopted.json` — in particular `tiered-subscription`'s bound
`matrix` caps), its stack profile's cost tiers, and a cost-inputs document
(operator-authoritative, or a documented illustrative fallback). See
`docs/patterns-registry.md#the-cost-axis-is-first-class` and
chief-wiggum#122.

**Scope (rollout steps 1+2 only):** the mechanical deriver + the `docs/pricing.md`
output contract. Two things are explicitly **not** built here, and are marked as
such in the rendered output rather than faked:

- **Market-comparable pricing** (rollout step 3 — a live lookup of what
  comparable products charge) is an `UNRESOLVED:` marker in the rendered
  report, gated by `scripts/check_unresolved.py`. Never fabricate a competitor
  price to fill this in.
- **Wiring the output into monetization patterns' `success_metrics`** (rollout
  step 4, e.g. `mrr`/`revenue_leak` on `tiered-subscription`) is a follow-up.

`docs/pricing.md` is a **protected path** once a product ships it — pricing is a
protected, admin-gated surface the same way `tiered-subscription`'s entitlement
logic is (see the pattern's `trust_class`). Treat a re-run that changes the
recommendation as something a human reviews, not an autonomous auto-apply.

## When to use

- Once a product has adopted `tiered-subscription` (its `matrix` is what bounds
  worst-case per-tenant cost) and a stack profile, to sanity-check the pricing
  before/alongside launch.
- Re-run whenever the cost-inputs change (a vendor repriced, a new cost tier
  went active) or the tier matrix changes (a plan's caps moved).

## Usage

```
/business-consultant <owner/repo> [--cost-inputs <path>] [--stack <id>] [--out <path>] [--marketplace] [--dry-run]
```

## Parameters

- `owner/repo`: Target GitHub repository (resolved + cloned via `repo.py`), or use `--repo <path>` for a local checkout.
- `--cost-inputs <path>`: An operator-authoritative cost-inputs.json (`templates/cost-inputs-schema.json`). Omit to fall back to the stack's illustrative seed (`patterns/stacks/<id>/cost-inputs.illustrative.json`) — the fallback is always loudly caveated in the output, never presented as a quote.
- `--stack <id>`: Stack profile id (default `gcp-serverless-saas`).
- `--out <path>`: Override the output path (default `<target>/docs/pricing.md`).
- `--marketplace`: Declare a take-rate/marketplace revenue model. Not inferred automatically — no registry pattern currently signals it.
- `--dry-run`: Derive and print without writing `docs/pricing.md`.

## Autonomy

Run to completion; this is an analysis skill. The one judgement call is whether
the target has adopted `tiered-subscription` at all — if not, the report still
renders (cost shape + pricing-model fit still hold) but says plainly that
per-tier unit economics need the pattern adopted first. Never invent matrix caps
or a market price to fill a gap; an honest "can't compute this yet" beats a
fabricated number.

---

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 2: Check the inputs are there

```bash
python3 "$CW_HOME/scripts/apply_pattern.py" --target-dir "$TARGET_REPO" --list-adopted
```

If `tiered-subscription` isn't adopted, note it — the run will still produce a
cost-shape + pricing-model-fit section, but unit economics per tier will be
empty until the pattern is applied (`/apply-pattern <owner/repo> --pattern
tiered-subscription ...`).

Check whether the target repo has its own `docs/cost-inputs.json`. With no
`--cost-inputs` flag the deriver **auto-uses** `<target>/docs/cost-inputs.json`
when it exists, and only falls back to the stack's illustrative seed when it
doesn't — so you rarely need to pass the flag. Whenever illustrative rates are
used (the seed, or any meter marked `provenance: "illustrative"`, however the
file was supplied), the loud caveat is surfaced in the report — it rides on the
data, not the code path.

### Step 3: Run the deriver

```bash
python3 "$CW_HOME/scripts/business_consultant.py" --repo "$TARGET_REPO"
# auto-uses $TARGET_REPO/docs/cost-inputs.json if present, else the illustrative seed;
# pass --cost-inputs <path> only to point at a non-default location
```

This derives, purely mechanically (no AI consultation, no network calls):

1. **Cost shape** — flat nut (flat_monthly + the active stack cost-tier's fixed
   add-on) + per-tenant variable cost; names the single largest **uncapped**
   meter and the first fixed **step-jump**.
2. **Unit economics per tier** — worst-case (matrix cap × meter rate) + typical
   (a documented fraction of worst-case) per tier, flagging any tier priced
   **underwater** (price below worst-case cost — most commonly the free tier,
   since a free tier's worst-case cost isn't automatically near-zero). A tier
   with an **unlimited (`-1`) cap on a metered line** is reported as
   **unbounded worst-case** (uncomputable, never a safe-looking $0 / 100% margin
   / finite break-even) — a single heavy tenant can cost arbitrarily much. A
   declared meter with **no cap field** in a tier's matrix is surfaced as
   `no cap declared`, never silently dropped.
3. **Break-even** — paying tenants of each tier needed to cover the flat nut,
   plus gross margin at typical usage; **unbounded** for any tier whose
   worst-case is uncapped.
4. **Market-comparable floor** — an explicit `UNRESOLVED:` marker (rollout step
   3, not built).
5. **Pricing-model fit** — a model *family* recommendation
   (`patterns/pricing-models/`), not a specific price.

### Step 4: Verify the unresolved seam is gated

```bash
python3 "$CW_HOME/scripts/check_unresolved.py" "$TARGET_REPO/docs/pricing.md"
```

Confirm it reports the market-comparable-floor marker — that's expected and
correct until rollout step 3 ships; it means dependent work can't silently treat
that section as resolved.

### Step 5: Report

Summarize for the user: the flat nut, the named uncapped meter + step-jump, any
underwater tier, break-even counts, and the recommended pricing-model family +
its `never` list (e.g. "usage-based-or-subscription; never lifetime-deal"). Point
at `docs/pricing.md` in the target repo and flag plainly if the illustrative seed
was used (numbers are unverified — recommend the operator supply a real
`docs/cost-inputs.json`).

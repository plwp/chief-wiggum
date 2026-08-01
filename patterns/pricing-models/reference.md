# Reference: Pricing Models That Worked

- **Category:** knowledge base (not an installable registry pattern)
- **Status:** reference — consumed by `/business-consultant`, not by `/apply-pattern`

## What this is (and isn't)

This is a captured, reusable **cost-shape → pricing-model decision table**, plus
the handful of pricing tactics that keep re-earning their keep across CW-built
products — so `/business-consultant` doesn't re-derive "what pricing model fits a
product like this" from scratch every time, the same reason the patterns registry
exists at all (see `docs/patterns-registry.md`).

It deliberately does **not** live in `patterns/registry.json` and ships no
`manifest.json` invariant cluster: it isn't a contract pack `/apply-pattern`
installs into a target repo, and `scripts/check_patterns.py` correctly does not
scan it. It's read by `scripts/business_consultant.py` (via
[`models.json`](models.json)) as a **lookup table**, the same role a stack
profile's `bindings/` plays for pattern↔vendor recipes.

## The load-bearing rule

**Recurring infra cost implies recurring price.** If serving a tenant costs the
provider a metered vendor bill that keeps charging every month the tenant stays
active (video storage/delivery, database rows, email sends, compute), a one-time
price guarantees the provider eventually pays more to serve a long-lived customer
than that customer ever paid. A lifetime deal on a product with per-tenant
recurring costs is a bet the customer *churns quickly enough* — the opposite of
what "lifetime" is supposed to promise. This is the rule
[`business_consultant.py`](../../scripts/business_consultant.py) enforces
mechanically: any cost shape with a nonzero per-tenant meter recommends a
recurring model family and explicitly rules out `lifetime-deal` /
`one-time-purchase`.

## The decision table

| Cost shape | Signal | Model family | Never |
|--|--|--|--|
| **flat-cost** | no metered per-tenant variable cost (or every meter is $0) | subscription-or-seat | — |
| **per-unit-recurring** | at least one nonzero per-tenant meter that recurs monthly | usage-based-or-subscription | lifetime-deal, one-time-purchase |
| **marketplace** | cost *and* revenue scale with transaction volume, not subscriber count | take-rate | flat-per-seat-only |

The machine-readable form (`model_family`, `rationale`, `never`, `notes` per row)
is [`models.json`](models.json)'s `cost_shape_to_model` array — the exact
structure `/business-consultant`'s pricing-fit step looks up. Classifying a
product's own cost shape (flat vs per-unit-recurring vs marketplace) is the
deriver's job, from its `cost-inputs.json` + adopted patterns; this table only
answers "given that shape, which model family fits."

## Pricing tactics (apply regardless of cost shape)

Captured in `models.json`'s `tactics` array; each also carries a `guardrail` — the
non-obvious way the tactic goes wrong if applied carelessly:

- **Founding-member grandfathering** — early customers keep their original
  price/terms as list price rises later. Guardrail: grandfathering must never let
  the *cost* underneath them silently rise uncompensated (see
  `tiered-subscription`'s non-destructive-degradation invariant, `INV-TSB-005`,
  for the general shape of this principle).
- **Annual ≈ 2 months free** — annual plans priced at roughly 10x monthly rather
  than a steeper discount. Guardrail: don't offer this on a cost shape with an
  *uncapped* meter (`capped_by: null`) unless that usage is metered and billed
  separately — prepaying flat against unbounded variable cost is the lifetime-deal
  risk one billing cycle at a time.
- **Free tier as a bounded loss leader** — a free tier is a deliberate, capped
  acquisition/activation cost, never an unbounded sink. Guardrail: the free
  tier's `tiered-subscription` `matrix` entry must keep every metered field
  tightly capped (never `-1`/unlimited) — the same worst-case-cost check the
  deriver runs on paid tiers applies to the free tier too.

## How `/business-consultant` consults this table

1. Classifies the product's cost shape from its cost-inputs + adopted
   `tiered-subscription` matrix (mechanical — see the skill's Step 3).
2. Looks up that shape's row in `models.json`'s `cost_shape_to_model`.
3. Emits the **model family** (a family, not a specific price point — an actual
   number needs market-comparable data, the step-3 live-lookup seam tracked in
   chief-wiggum#122) plus its `rationale` and `never` list, and surfaces any
   `tactics` whose `applies_when` matches.

## Provenance

Distilled from the same worked example the registry's cost axis cites
(`docs/patterns-registry.md#the-cost-axis-is-first-class`): a shipped production
SaaS's hand-written `docs/pricing.md`, genericized — no app name, no real vendor
quote, no customer data.

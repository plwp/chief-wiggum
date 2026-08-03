# Pattern: Presale (Honest Pre-Order)

- **Category:** validation-experiment
- **Trust class:** end-user-signal-driven — the datum is a real charge, the strongest signal an end user can send
- **Status:** specified (with a stampable `scaffold/`)
- **Depends on:** nothing (converts best against an audience, e.g. [`landing-page-smoke-test`](../landing-page-smoke-test) signups)
- **Feeds:** the bet's evidence-strength floor — this is the pattern that earns `building`

## What it is

A checkout on a product that does not exist yet, framed **honestly**: the page
says the product is not built, states the price and the expected delivery window
*before* any payment step, and takes a real charge (or authorized deposit)
through a real payment provider. This is the only experiment class that produces
**strength 5 (money)** evidence — the commitment class the evidence-strength
floor on `bet.py transition <id> building` requires (Blank: "purchase orders,
not enthusiasm").

The line this pattern refuses to cross, stated up front: **money means money,
and honesty means honesty.** A simulated charge or a fake door at the pay button
is click evidence at best and destroys the strength-5 claim (INV-PRE-004); a
checkout that hides the product's non-existence is fraud, not an experiment
(INV-PRE-003). The refund promise is part of the experiment's cost: killing or
pivoting the bet refunds every pre-order, and the outstanding obligation counts
as wind-down cost in the harvest check (INV-PRE-005).

## When to apply

- A demand assumption is validated at click/time strength and the bet needs
  commitment-class evidence before `building`.
- Price and delivery window can be stated honestly, and refunds can be honored
  promptly if the bet dies.
- An audience exists to convert (smoke-test signups, interview pipeline).

## Mechanism — invariant cluster

- **INV-PRE-001 — pre-registered conversion bar.** The presale binds to an
  `assumption.py` test card at evidence strength 5; the threshold block is
  hashed into the portfolio journal before the checkout goes live, and the
  verdict is recorded only against the original hash. *(Realized in-repo:
  `scripts/assumption.py`.)*
- **INV-PRE-002 — per-cohort rate, never a revenue counter.** Visitor→preorder
  conversion per cohort; cumulative preorders/gross revenue are rejected by the
  vanity-metric lint. *(Realized in-repo: `scripts/assumption.py`.)*
- **INV-PRE-003 — honest framing.** Not-built-yet, price, and delivery window
  are stated before the payment step. *(Design-derived.)*
- **INV-PRE-004 — money means money.** The datum is a real charge or authorized
  deposit; anything simulated demotes the evidence to click class.
  *(Design-derived.)*
- **INV-PRE-005 — refund-on-kill.** Kill/pivot before delivery refunds every
  pre-order per the stated policy; the obligation is a wind-down cost in the
  harvest check. *(Design-derived.)*
- **INV-PRE-006 — visual design is chosen, not converged.** Same clause as
  `landing-page-smoke-test` INV-LPS-006 — a presale page is equally
  outward-facing: the stamped scaffold is a STRUCTURE only, the visual design
  MUST be presented as ≥6 deliberately distinct rendered variants grounded in
  a current-craft reference no more than 90 days stale (`docs/design-taste.md`,
  chief-wiggum#250), with a human pick recorded before the page ships
  (chief-wiggum#249). *(Design-derived — a human checkpoint, not a lintable
  property; no gate script exists or is planned for this invariant.)*

## Scaffold

`scaffold/` stamps into the target repo (parameters bound by `/apply-pattern`):

- `experiments/presale/index.html` — honest pre-order page: `{{product_name}}`,
  the not-built-yet framing block, `{{price_usd}}`, `{{delivery_window}}`, the
  refund policy, and a checkout button.
- `experiments/presale/checkout.js` — checkout stub: posts a checkout-session
  request to `{{checkout_endpoint}}` and states, in-file, that it must be wired
  to a **real** payment provider before launch — do not fake a charge.
- `experiments/presale/README.md` — the binding note: pre-register the strength-5
  card before launch; refund-on-kill wiring; verdict against the original hash.

## Grounding

INV-PRE-001/002 cite the in-repo validation engine (`scripts/assumption.py`).
INV-PRE-003..005 are **design-derived** — flagged per the #139 allowance until
the first grounded use (the §8 dogfood discipline: one real bet through
card → run → verdict).

## Success metrics

Per-cohort rates only: `visitor_to_preorder_rate_pct` (primary),
`preorder_refund_rate_pct` (buyer's remorse erodes the strength-5 claim),
`cost_per_preorder_usd`.

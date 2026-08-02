# Pattern: Landing-Page Smoke Test

- **Category:** validation-experiment
- **Trust class:** end-user-signal-driven — the page and its measured conversion are the signal; nothing here is asserted, only measured
- **Status:** specified (with a stampable `scaffold/`)
- **Depends on:** nothing
- **Feeds:** [`presale`](../presale) — smoke-test signups are the audience a presale converts

## What it is

The cheapest universal demand test in the literature: a static page that states a
value proposition honestly and asks for a signup. The factory **stamps the
experiment** — page + instrumentation stub — the same way `/apply-pattern` stamps
any other scaffold (#135), and the experiment binds to the validation engine
(`scripts/assumption.py`, chief-wiggum#236): the success bar is a **pre-registered
test card** whose threshold block was hashed into the portfolio journal *before*
the page went live, and the verdict may only be recorded against that original
hash. This is the Camuffo-RCT mechanism applied to the first dollar-free test a
bet runs.

What it can and cannot prove, stated up front: a smoke test produces **strength
2 (click) evidence** — it can cheaply *falsify* a demand assumption ("nobody even
leaves an email") but can never *validate* willingness to pay. The evidence-strength
floor on `bet.py transition <id> building` will not accept it as commitment-class
evidence; graduating to strength 5 is the [`presale`](../presale) pattern's job.

## When to apply

- A bet is `probing|validating` and its demand assumption has nothing behind it
  but opinions (strength 1).
- You want the cheapest test that produces a real, pre-registered per-cohort rate.
- The signup audience will seed the follow-up experiment (presale, interviews).

## Mechanism — invariant cluster

- **INV-LPS-001 — pre-registered success bar.** The experiment binds to an
  `assumption.py` test card; the threshold block is content-hashed into the
  portfolio journal at card creation, and the verdict is recorded only against
  the original hash. *(Realized in-repo: `scripts/assumption.py`.)*
- **INV-LPS-002 — per-cohort rate, never a counter.** The success criterion is
  visitor→signup conversion per cohort; cumulative signup totals are rejected by
  the vanity-metric lint — they falsify nothing. *(Realized in-repo:
  `scripts/assumption.py`.)*
- **INV-LPS-003 — honest framing.** The page describes what the product *will*
  do; it never claims the product exists today. No fake login, no fabricated
  testimonials, no invented usage numbers. *(Design-derived.)*
- **INV-LPS-004 — instrumented denominator.** Visits AND signups are recorded per
  cohort, so the rate has a real denominator. *(Design-derived.)*
- **INV-LPS-005 — evidence honesty + PII floor.** A signup is strength-2 evidence
  and a stored contact is PII: minimal storage, product-follow-up use only,
  deletable. *(Design-derived.)*

## Scaffold

`scaffold/` stamps into the target repo (parameters bound by `/apply-pattern`):

- `experiments/landing-page-smoke-test/index.html` — framework-light static page:
  `{{product_name}}`, `{{value_prop}}`, an email form with `{{cta_label}}`, and an
  honest "not built yet" framing block.
- `experiments/landing-page-smoke-test/capture.js` — instrumentation stub: records
  `visit` and `signup` events per cohort against `{{signup_endpoint}}`, with the
  wiring TODO stated in the file (this is a scaffold, not a product).
- `experiments/landing-page-smoke-test/README.md` — the binding note: pre-register
  the test card BEFORE launch, run traffic, record the verdict against the
  original hash.

## Grounding

INV-LPS-001/002 cite the in-repo validation engine (`scripts/assumption.py`) that
enforces them mechanically. INV-LPS-003..005 are **design-derived** — this exact
stamped experiment has not yet run against a shipped bet; the manifest flags them
per the #139 allowance, to be re-grounded after first real use (the §8 dogfood
discipline).

## Success metrics

Per-cohort rates only: `visitor_to_signup_rate_pct` (primary),
`cost_per_signup_usd`, `signup_to_reply_rate_pct` (separates real interest from
drive-by emails).

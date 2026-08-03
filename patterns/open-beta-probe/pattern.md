# Pattern: Open-Beta Probe

- **Category:** validation-experiment
- **Trust class:** end-user-signal-driven — a returning user doing real work in the
  product is the signal; nothing here is asserted, only measured
- **Status:** specified (with a stampable `scaffold/`)
- **Depends on:** nothing (converts best against an audience, e.g.
  [`landing-page-smoke-test`](../landing-page-smoke-test) signups, or an existing
  community)
- **Feeds:** the bet's evidence-strength floor at strength 3–4 — stronger than a smoke
  test, cheaper than a full presale

## What it is

**Open beta is the new waitlist** (chief-wiggum#256). A landing-page smoke test buys
strength-2 (click) evidence — it was canonical because it was cheap relative to
building, not because the evidence was good. Two things changed that calculus: build
cost for software this size has collapsed, so the cost argument for a fake door mostly
evaporated; and the signal itself degraded — when a waitlist page costs an afternoon and
everyone has one, a signup means materially less than it used to.

This pattern is a **working, deliberately-scoped product put in real hands** via an
existing community, metered by **signup codes**: trackable, revocable, and creating a
felt scarcity a public form does not. A returning user who does real work in the
product is categorically stronger evidence than an email address — strength 3–4
(time/reputation-class) on the evidence ladder, versus a smoke test's strength 2.

The line this pattern refuses to cross, stated up front: **a bad beta is only the
better instrument when it is survivable.** The pattern requires a declared
**blast radius** and refuses to stamp without one (INV-OBP-004): a bet whose beta
touches money, wages, tax, health, published records, or third-party systems
(**unbounded**) must run in **compute-only / draft-only mode** — it may compute and
present the artifact for human review, but may not commit the irreversible act. A
bounded blast radius (no irreversible external effect) may run the beta live.

## When to apply

- A bet needs commitment-class-adjacent evidence (time/reputation, strength 3–4)
  cheaper than a full presale, and the product can genuinely be built to a working,
  scoped state before the test.
- An existing community or channel exists to launch into (signup codes are the
  metering mechanism, not the discovery mechanism — see
  [`landing-page-smoke-test`](../landing-page-smoke-test) for the narrower "message/
  positioning only" or "no community yet" case this pattern does NOT fit).
- The operator can honestly answer the blast-radius question (below) and, if
  unbounded, accept compute-only/draft-only scope for the beta.

## Mechanism — invariant cluster

- **INV-OBP-001 — pre-registered success bar.** The experiment binds to an
  `assumption.py` test card at evidence strength 3–4; the threshold block is
  content-hashed into the portfolio journal before the beta opens, and the verdict is
  recorded only against the original hash. *(Realized in-repo: `scripts/assumption.py`.)*
- **INV-OBP-002 — per-cohort rate, never a raw signup counter.** The success criterion
  is a per-cohort **returning-user / real-work-done rate** among redeemed codes —
  cumulative redemption counts are rejected by the vanity-metric lint the same way a
  smoke test's cumulative signups are. *(Realized in-repo: `scripts/assumption.py`.)*
- **INV-OBP-003 — signup-code metering.** Every beta participant enters via a unique,
  trackable, revocable signup code — codes are the attribution and scarcity mechanism a
  public form cannot provide; code issuance, redemption, and revocation are all
  recorded per cohort. *(Design-derived.)*
- **INV-OBP-004 — blast-radius declaration is mandatory; unbounded forces
  compute-only/draft-only mode.** The pattern refuses to stamp without a declared
  blast radius (`bounded` | `unbounded`). `bounded` (no irreversible external effect)
  may run live; `unbounded` (touches money, wages, tax, health, published records, or
  third-party systems) MUST run compute-only/draft-only — the beta computes and
  presents an artifact for human review and cannot itself commit the irreversible act.
  *(Design-derived.)*
- **INV-OBP-005 — reputation is single-use.** A given community can be launched to
  once per bet; a beta shipped before it works burns the audience the bet depends on.
  This is a **cost**, priced into the envelope (not an externality) — the bet's
  affordable-loss envelope should reflect it. *(Design-derived.)*
- **INV-OBP-006 — honest evidence-strength floor.** A code redemption alone is
  strength 2 (click-equivalent); the strength-3/4 claim requires a RETURNING user who
  did real work in the product, never a bare signup or single visit. *(Design-derived.)*

## Scaffold

`scaffold/` stamps into the target repo (parameters bound by `/apply-pattern`):

- `experiments/open-beta-probe/blast_radius.json` — the declared blast-radius record:
  `{{blast_radius}}` (`bounded`|`unbounded`) and the resulting `{{mode}}`
  (`live`|`compute-only`); stamping is REFUSED without `blast_radius` bound (a required
  parameter with no default — INV-OBP-004).
- `experiments/open-beta-probe/signup_codes.py` — a stub script: generate, redeem, and
  revoke per-cohort signup codes; wiring TODO stated in-file (this is a scaffold, not a
  product).
- `experiments/open-beta-probe/README.md` — the binding note: declare the blast radius
  BEFORE requesting codes; pre-register the test card at strength 3–4; the sequencing
  rule (below) — push motions wait for validated assumptions.

## Sequencing rule (restated from §2.4/§8, chief-wiggum#256 decision 4)

*Push marketing comes only after assumptions are fully tested.* This already existed as
the paid-spend unlock (§4: no paid-acquisition spend before PMF-class evidence);
decision 4 strengthens it to cover **all push motions** — cold outreach, paid ads,
launch campaigns — not just paid ads (Startup Genome's premature-scaling finding, by
name). Pull motions (content, community presence, answering questions where the pain is
already posted) remain available throughout, including before this pattern's beta opens.

## Grounding

INV-OBP-001/002 cite the in-repo validation engine (`scripts/assumption.py`) that
enforces them mechanically, the same way `landing-page-smoke-test`/`presale` do.
INV-OBP-003..006 are **design-derived** — this exact stamped experiment has not yet run
against a shipped bet; flagged per the #139 allowance, to be re-grounded after first
real use (the §8 dogfood discipline).

## Success metrics

Per-cohort rates only: `code_redemption_rate_pct` (redeemed / issued codes per cohort),
`returning_user_rate_pct` (primary — the strength-3/4 evidence itself: redeemed codes
whose holder returned and did real work), `cost_per_redeemed_code_usd`.

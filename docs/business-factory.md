# Business Factory — Literature → Mechanizable Artifacts

**Status: research map.** This document is the literature-sweep deliverable for the "business
factory" direction: extend Chief Wiggum from a software factory into a machine for taking
**small business bets** — under ~$1,000 to prove, ~3 months to a kill decision — with the
commercialization side mechanized the way CW mechanizes code quality: typed artifacts, state
machines, invariants, and gate scripts. It maps the entrepreneurship / MBA literature onto CW
artifact classes and names candidate capability tracks. Nothing here is committed work; the
intended next step is a steer on §8, then `/seed`-style issue derivation.

Framing: an organization is an information system whose payload is money. The literature turns
out to already contain typed schemas for the static structure, computable value-flow graphs,
state machines for the transactional core, and feedback-loop graphs for dynamics — formalized
three separate times (Osterwalder's 2004 ontology, e3-value, REA/ISO 15944-4) and never joined
up or connected to built software. Connecting these schemas to code via contracts and
traceability is the genuinely novel seam CW can occupy.

---

## 1. Why this shape: the load-bearing evidence

Four findings anchor the whole design. They are worth internalizing before any artifact schema,
because they say *which parts of the machine do the work*.

1. **Pre-registered falsifiability is RCT-validated.** Camuffo, Cordova, Gambardella & Spina
   (*Management Science* 2020; replicated across 4 RCTs / 759 firms, *SMJ* 2024) randomized
   nascent startups into "scientific method" training — articulate falsifiable hypotheses,
   define valid metrics, **set explicit quantitative decision thresholds before testing**.
   Treated firms terminated bad ideas more, pivoted more decisively, and earned roughly an
   order of magnitude more revenue. The treatment condition is, almost verbatim, a CW contract
   pack applied to a business plan. Corollary: a working validation gate **increases** kill
   rates — "more bad plans die faster" is the gate succeeding, and the UX must present it that
   way.

2. **Self-enforcement fails; binding rules and fresh eyes work.** The escalation-of-commitment
   literature (Staw 1976; Simonson & Staw 1992; Boulding, Morgan & Staelin, *JMR* 1997) is
   experimentally brutal: founders escalate on failing bets precisely because they started
   them; **better dashboards do not fix it** (executives given clearer negative data still
   escalated); self-specified, self-enforced kill rules were largely ineffective. What worked:
   *binding* predetermined decision rules, and swapping in a decision maker with no sunk
   costs. CW's hashed-goalposts + gate-script + fresh-context-quorum idioms are not
   conveniences here — they are the treatment condition from the experiments. Adner &
   Levinthal (2004) add the failure mode to design against: "real options" degenerate into
   escalation when the abandonment condition gets retroactively redefined — which is exactly
   why kill criteria must be stable-ID'd and content-hashed like contracts.

3. **Stage models are dead; the consistency invariant survives.** Levie & Lichtenstein (2010)
   reviewed 104 published stage models (1962–2006): no consensus on stage count, no evidence
   firms traverse stages linearly; firms stay small by choice, regress, renew. What survives
   in Greiner, Churchill & Lewis, Adizes, *and* the Startup Genome data is one idea:
   **practice must match evidence** — what you do (spend, hire, build, systematize) must match
   what the market has proven (customer-response metrics). Startup Genome operationalized it:
   *actual stage* computed from customer response only, *behavioral stage* per dimension from
   what the venture is doing; premature scaling = behavioral ahead of actual, and it was the
   dominant correlate of failure in their corpus (with survey-data caveats). Design
   consequence: **stage is a derived label computed from evidence, never a self-declared field
   that unlocks behavior**, and the detectors are inconsistency checks — the same
   information-flow discipline as CW's single-writer gate.

4. **The metric small bets actually move is loss-per-miss, not win rate.** Wiltbank et al.
   (*JBV* 2009, 121 angels / 1,038 investments): investors emphasizing non-predictive control
   had significantly fewer failures with **no reduction in successes** — affordable-loss
   discipline makes misses cheaper at the same win count. So portfolio accounting should score
   the loss distribution and kill hygiene, not per-bet outcomes. Base rates agree the misses
   will dominate: >54% of *listed, Stripe-verified* Indie Hackers products earn $0; ~5% exceed
   $100k/yr; surviving indie SaaS typically grows 1–9% month-over-month. Two corollaries:
   the portfolio needs shot count, and **a 3-month box can only validate demand existence,
   never growth rate** — kill criteria must be evidence-shaped (paid conversions,
   commitment-bearing signals), not MRR-target-shaped.

A caution that spans all four: effectuation's causal claims are contested (Arend et al. 2015 —
thin, expert-derived, success-biased evidence base). Build affordable loss as a
**loss-bounding safety property** (defensible on decision theory alone: the worst case is
computable, the expected value is not), never as a success recipe.

---

## 2. The mechanizable core — five convergent primitives

These recur independently across the academic and practitioner streams; cross-source
convergence is the selection criterion for what deserves to be a CW primitive.

### 2.1 The envelope (affordable loss, released in tranches)

Per-bet typed loss caps, human-set, script-enforced, raisable only by a journaled human act.

- Schema (Dew et al. 2009 supply the field list): `{cash_cap, time_cap_hours,
  calendar_cap_days, attention_cap, reputation_exposure}`. The **ability** side is computable
  from a means inventory (current slack, zero-based, near-term — envelopes justified by
  projected future revenue are a lintable violation, the business analogue of building on an
  `UNRESOLVED:` fact); the **willingness** side is human-entered; `envelope =
  min(ability_derived, willingness)`.
- Gompers (1995) staging: the cap is **stepped** — `tranches: [{amount, unlock_milestone_id}]`;
  invariant: cumulative spend ≤ cumulative unlocked tranches. Spend-sequencing (McGrath 1999):
  a bet in `probe` state may not incur exercise-scale costs.
- The ratchet idiom applies verbatim: workers never touch the envelope; raises are journaled
  in the hash chain.
- **`liability_exposure` addendum (chief-wiggum#277)**: Dew et al.'s field list has no
  liability/exposure dimension, so an uncapped contractual indemnity records identically to
  no exposure at all. Added as an enumerated field —
  `capped_at(<amount>) | insured(<policy>, responds=yes|unverified|no) | uncapped_entity |
  uncapped_personal` — never free text. Its total absence is a lintable finding; a STATED
  value, including an uncapped one, is never itself a finding: the operator takes uncapped
  risk deliberately as a competitive edge (§9.4 addendum, "terms as an attack surface"), and
  the point of this field is to make that choice explicit, sized and counted, never to block
  it. Insurability is a separate fact from insurance: `responds` defaults to `unverified` and
  only a human answer moves it (a common PI-policy exclusion is contractually-assumed
  liability, so holding a policy does not establish that it responds). The portfolio-level
  concurrency count over every non-exited bet is the check that actually matters — one
  uncapped exposure is a considered bet, several concurrently is a portfolio that cannot
  survive one bad event.

### 2.2 The assumptions graph (traceability transplanted to business objects)

Discovery-driven planning (McGrath & MacMillan 1995) + Strategyzer test cards (Bland &
Osterwalder 2019) + Ries's leap-of-faith assumptions are one structure:

- **Assumption ledger**: stable IDs (`ASM-001…`), status
  `{untested, testing, validated, falsified}`, generated from a premortem (Klein 2007 — the
  seeded-defect discipline applied to the plan itself) and from every cell of the financial
  model. This two-segment `ASM-NNN` shape is a deliberately separate namespace from
  `chief_wiggum.trace_ids`'s three-segment `ASM` stable-ID kind (`ASM-slug-NNN`) used at the
  epic/system layer — the two share the `ASM-` prefix but are structurally disjoint grammars,
  scoped to different artifacts (`bets/<bet-id>/assumptions.json` vs `docs/epics/**`), and are
  never resolved against each other (chief-wiggum#294).
- **Hypothesis grammar**: Savoia's XYZ template — "at least X% of Y will Z" — makes
  un-falsifiable phrasing syntactically impossible. A falsifiability linter is a parser.
- **Pre-registered test cards**: `{asm_id, method, metric, threshold, cost, evidence_strength,
  result, verdict}` — the threshold is **hashed at card creation**; a verdict may only be
  filled against the original threshold. This is the failing-test-first discipline, and it is
  the specific mechanism the Camuffo RCT validated.
- **The gate is `check_traceability.py`'s exact shape**: every financial-model cell → ASM;
  every ASM → ≥1 milestone/test card; every milestone → ≥1 ASM. Orphans, uncovered
  assumptions, dangling links — same graph algorithm, new node types.
- **Fermi viability gate** (Maurya; same math as DDP's reverse income statement): given
  `{minimum success criterion, price, churn, funnel conversions, TAM}`, compute required
  throughput and hard-fail arithmetic impossibilities **on day zero, before any spend** — the
  cheapest failing test in the entire literature.

### 2.3 The bet state machine (derived labels, plural terminals)

- States (synthesis of Blank, Maurya, Cooper, Kahl):
  `proposed → probing → validating → building → scaling`, with `kill_pending` reachable from
  anywhere and gate verdicts in Cooper's vocabulary — `go | kill | hold | recycle` — rather
  than binary pass/fail (matching CW's existing report-only/gate/demotion philosophy).
- **Terminal states are plural and legitimate**: `killed(harvested) | parked |
  lifestyle(hold) | sold`. Churchill & Lewis's Success-Disengagement legitimizes staying
  small; Acquire.com data makes `sold` plannable (micro-SaaS trades at ~3.9× median
  seller-discretionary earnings, ~81–90 days to liquidity) — so the kill decision first passes
  a **harvest check**: if estimated sale value exceeds the cost of listing, "kill" becomes
  "sell."
- **States-and-dates** (Duke 2022) supplies the timeout semantics every other source lacks:
  every kill criterion is `{state: {metric, comparator, threshold}, date}` — a criterion
  missing either field is malformed (a lintable soundness rule). Every milestone gets a date,
  so `stalled` is a detectable state, not a silent one.
- **Pivot is a transition, not an edit** (closes Adner & Levinthal's loophole): a pivot closes
  the old bet record — its criteria evaluated honestly against the old thesis — and opens a
  successor bet with fresh criteria and a fresh envelope. Bland's dependency rule rides along:
  a pivot re-opens previously-validated assumptions that depended on the changed element.
- Transition guards are evidence-typed (see 2.4): e.g. `probing → building` requires ≥1
  validated ASM at commitment-class evidence strength; Blank's validation exit is "purchase
  orders, not enthusiasm."

### 2.4 Evidence typing and the disinterested killer

- **Evidence-strength ladder** (Fitzpatrick, Bland, Savoia converge exactly):
  `opinion < click < time < reputation < money`. An evidence linter rejects "validated" claims
  supported only by compliments/hypotheticals; interviews alone can never exceed weak
  confidence; commitment currencies (scheduled time, staked reputation, paid money) are the
  only strong evidence. Vanity-metric lint: cumulative/gross counters are banned as success
  criteria (Ries — they falsify nothing); required form is per-cohort rates.
- **Kill review runs on fresh context**: per Boulding et al., the continue case is argued to a
  fresh-context quorum given **only** the pre-registered criteria and measured results — never
  the bet's accumulated working context. Invariant: kill-review agents must not inherit the
  bet's session history. Final authority stays human; overrides are possible, costly, and
  journaled.

### 2.5 Portfolio accounting (process, not outcome)

- A machine-tracked ledger is the answer to the stated cognitive-load constraint: per bet —
  state, spend vs envelope, days to next dated criterion, currently-riskiest untested
  assumption, mark-to-market harvest value. Nothing lives in the operator's head.
- Invariants: **bets-in-flight cap** (attention is the binding resource, and most of a bet's
  true cost); loss-distribution check (median/max loss per dead bet under thresholds —
  the Wiltbank metric); bet-size-creep detector (envelopes trending up while kill rate trends
  down = prediction-mode drift); minimum shot count vs encoded base-rate priors.
- **Retrospectives grade process, not outcome** (Simonson & Staw: process accountability
  de-escalates; outcome accountability doesn't): was the envelope respected, were criteria
  evaluated honestly, was the kill executed on trigger. Clean kills are celebrated in the
  report format itself. An `abandoned` transition is blocked until a harvest retrospective is
  committed — `/close-epic`'s shape.
- Every encoded prior carries an `evidence_grade` — the base-rate data is self-selected and
  survivorship-biased in both directions, and weak data must not masquerade as knowledge
  (the `TBD:/UNRESOLVED:` honesty discipline).

---

## 3. Layer map: literature → CW artifact class → existing seam

| Layer | Donor formalism | Typed artifact | State machine / graph | Script-computable gate | Existing CW seam it extends |
|---|---|---|---|---|---|
| **Portfolio** | Effectuation (Sarasvathy, Dew), real options (McGrath), VC staging (Gompers) | means inventory; per-bet envelope + tranches; portfolio ledger with priors (`evidence_grade`) | option lifecycle: premium → strike → exercise/abandon | spend ≤ unlocked tranches; bets-in-flight cap; loss-distribution; bet-size creep | ratchet hash chain (journaled raises); `budget-trees` spend budgets; `factory_log` cost-vs-value precedent |
| **Bet lifecycle** | Blank 4 Steps, Maurya traction roadmap, Cooper stage-gate, Duke states-and-dates, C&L / dynamic-states | bet record; kill criteria (hashed); harvest retrospective | `proposed → probing → validating → building → scaling` + `kill_pending`; terminals `{killed, parked, lifestyle, sold}`; pivot = successor bet | states-and-dates soundness lint; dated-criterion cron evaluation; evidence-typed transition guards; harvest check (~3.9× SDE) | `state-machines.md` formalism; `/close-epic` retrospectives; stale-while-blocking auto-demotion shape |
| **Plan schema** | Osterwalder 2004 BMO (+ his abandoned 2004 description-logic constraints), Lean Canvas, VPC, e3-value, Zott & Amit | `business-model.json` with BMO enums, every field carrying a validation status; value-network graph (actors, value objects, interfaces) | e3-value dependency paths (AND/OR, atomic interfaces); DEMO transaction pattern for customer-facing ops | revenue-reachability (every VP → segment → revenue stream); pain↔reliever bipartite completeness; **per-actor NPV > 0** (the multi-sided-model kill check); sensitivity sweep | `/seed` Step 4.5 (pattern + stack selection); `docs/domain-context.md` ground-truth discipline; `/design`'s stamp-a-binding-json-contract shape |
| **Validation** | DDP, Bland test cards + 44-experiment library, Savoia XYZ, Mom Test, Camuffo RCT | assumption ledger (`ASM-` IDs); test cards (hashed thresholds); interview-evidence records with commitment currencies | per-hypothesis lifecycle with pivot back-edge re-opening dependents | falsifiability (XYZ) parser; pre-registration hash; traceability graph (cell→ASM→milestone→card); Fermi viability; evidence-strength floor; premature-scaling detector | `check_traceability.py` (same algorithm); `check_unresolved.py` markers; **experiment library = a patterns-registry for validation CW can stamp** (landing-page smoke, fake-door, presale, concierge) |
| **Money runtime** | REA (ISO 15944-4), DEMO, iPricing (arXiv 2503.21444), SBIFT 7-dim pricing taxonomy, value metrics (ProfitWell) | Stripe events typed as REA events with duality links; pricing-as-structured-data `{scope, base, influence, formula, temporal, discrimination, dynamic}`; declared value metric | commitment → fulfillment; plan up/downgrade lattice; MRR movement per subscription: `new → expansion \| contraction \| churned \| reactivation` | duality completeness (unpaired give/take = leakage); derived-balance reconciliation; no dominated plan / monotone tier limits; **pricing artifact ↔ code enforcement conformance** (`@cw-trace` over feature flags & limit middleware); value metric must name an instrumented single-writer counter | `/business-consultant` deriver + `cost-inputs-schema.json`; `tiered-subscription` pattern; single-writer gate; #229 `platform-cost-observability` (its `spend_vs_model_variance` *is* the actuals-vs-model loop) |
| **Growth & GTM** | Growth loops (Balfour), Casadesus-Masanell choice graphs, Bullseye (Weinberg & Mares), GTM motion economics, Walling stair-step | `loop {steps[], conversion[], cycle_time, reinvestment}`; channel-experiment records over the fixed 19-channel enum; GTM motion record | Bullseye loop: `brainstormed → ranked → testing(≤3) → focused \| rejected`; choice→consequence causal graph | loop gain K from instrumented events; every loop edge maps to a tracked event in code; motion-fit decision table (ACV < $5k + sales-led = fail; headcount 0 + sales-led = fail); exactly-one-focused-channel; step-1 stair-step gates (one channel, ecosystem distribution) | `referral-invite-loop` #139 (*is* a growth loop; its declared `k_factor` and `reward_cost_per_attributed_signup` — the repo's nearest CAC figure — are computed by nothing today); `engagement-instrumentation` pattern |
| **Unit-economics invariants** | Skok SaaS Metrics 2.0, ChartMogul/SaaS Capital micro-scale benchmarks, Van Westendorp (1976), Sean Ellis PMF / Superhuman engine, Monetizing Innovation | `unit-economics.json` (ARPA, GM%, CAC, churn, expansion — Stripe + expense-ledger derivable); WTP evidence records; fixed-schema surveys | (rides on the MRR movement machine above) | the **micro-scale invariant set** (§4); Van Westendorp: 4 fixed questions → deterministic curve intersections → `price ∈ [floor, ceiling]`; PMF gate: ≥40% "very disappointed", n ≥ 40, **segment definition hashed** (no gerrymandering); every metric carries `scale_applicability: micro\|growth\|venture` and a small-n credible interval | `/business-consultant` unit-economics sections; `check_unresolved` market-comparable marker; gate-validation protocol (survey pipelines are *fixed schema → deterministic scoring → threshold* — the same certifiable shape) |

### §4 The micro-scale invariant set (the gates that actually bind under $10k MRR)

Venture-calibrated thresholds silently applied to micro-scale data are the literature's biggest
trap (T2D3 and Rule-of-40 are venture return requirements, not health checks; LTV is
statistically meaningless below ~100–200 customers because lifetime = 1/churn extrapolates a
noisy small sample to infinity). The set that is actually valid at CW's target scale:

1. `net_mrr_churn ≤ 2%/mo` (Skok), reported as a credible interval below ~200 customers
2. `cac_payback ≤ 3–6 months` (bootstrapped-tightened: no venture capital funds the SaaS
   cash-flow trough; this replaces LTV:CAC as the operative check at small n)
3. `blended_cac < first_year_gross_profit_per_customer` (solvency without lifetime
   extrapolation)
4. `pmf_score ≥ 40% (n ≥ 40)` before unlocking any paid-acquisition spend
5. `chosen_price ∈ [Van Westendorp floor, ceiling]` with WTP evidence artifacts on file
   (refuse to scaffold a paid product with zero WTP evidence — "price before product" made
   mechanical)
6. GTM motion-fit decision table satisfied
7. Exactly one channel in `focused` state, measured channel-CAC ≤ target
8. `projected_mrr_ceiling = new_mrr_rate / churn_rate ≥ founder_target` (steady-state
   arithmetic: at 5%/mo churn you replace ~46% of your base yearly — the ceiling is computable)
9. Cash-trough projection stays above reserves ("default alive" at personal scale)

---

## 5. What stays human (the checkpoint list)

Every source quarantines the same judgments, and every formalism's job is to force them into
typed, versioned, blockable slots rather than leave them implicit:

- Setting **willingness-to-lose** (the envelope's human half) and the minimum success criterion
- **Inventing assumptions** and choosing leading-indicator metrics (the RCT needed human
  mentors for theory-building; hypothesis *generation* did not mechanize)
- Designing non-leading experiments; conducting interviews; reading ambiguous evidence
- Whether a pain is real; which actor should own an activity; WTP and the pricing point
- The **pivot-vs-kill call** and the pivot direction; the sell-vs-scale fork
- **Overriding a triggered kill criterion** — must remain possible, costly, and journaled

---

## 6. Design cautions

1. **Anti-theater rule**: every business artifact either gates a spend decision or it does not
   exist. The failure mode is beautiful canvases that gate nothing — `/business-consultant`
   got this right by staying a deterministic deriver; the business layer extends that
   discipline or it is MBA cosplay.
2. **Gate-rollout discipline applies unchanged**: every business gate ships report-only,
   exits 0 by default, gets a `docs/quality/validation/<gate>.json` record with seeded-defect
   trials and an authority boundary before `--gate`, carries a hash-derived
   `--scanner-version`, and **skips-not-fails on missing inputs** (cost/revenue inputs will
   often be absent — adopt the complexity-snapshot "skipped, never a regression, never a
   crash" contract). Vendor rates and survey responses are non-deterministic inputs — pin to
   fixtures for clean-corpus runs.
3. **Expect the gates to kill most plans** — design the reporting so a high kill rate reads as
   the factory working (false-positive reduction), and so clean kills are visibly credited.
4. **Attention is the scarcest resource.** The bets-in-flight cap is probably the most
   important invariant in the system; the $1k cash cap is secondary to it.
5. **Encode base rates with `evidence_grade`**, and let no plan claim success probability far
   above prior without cited evidence.
6. **Reconcile with `docs/feature-value-discovery.md`** rather than duplicating it: its four
   parked generative directions (opportunity discovery, adoption telemetry, business-value
   pricing = #122, opportunity-carrying improvement loop) are the *per-product* half of this
   layer; the business factory adds the *per-bet and portfolio* half above them. Its
   trigger-signal discipline ("build a direction when its trigger appears") applies to §8.

---

## 7. Existing seams and known gaps (repo inventory)

Seams this design builds on rather than re-creating:

- **`/business-consultant`** (#122): deterministic unit-economics + pricing-model-fit deriver
  with honest three-state worst-case handling and `--format json` — the nucleus of the
  invariants layer.
- **Patterns registry `success_metrics`**: every pattern already declares business metrics
  with goal directions; monetization/conversion metric changes already route to blocking
  admin approval. `referral-invite-loop` declares `k_factor` and
  `reward_cost_per_attributed_signup` — the natural join to an acquisition-cost layer.
- **#229 `platform-cost-observability`** (in flight): whole-bill spend attribution with
  `spend_vs_model_variance` — the measurement half of the actuals-vs-model feedback loop.
- **`/seed` Step 4.5** (pattern + stack-tier selection) is the natural insertion point for a
  plan-stage; **`/design`** is the structural template (product-level, once, human-choice
  checkpoint, mechanical extraction into a binding `.json`, later fidelity gate).
- **`check_traceability.py` / `check_unresolved.py` / ratchet hash chain / gate-validation
  protocol / single-writer gate**: the enforcement machinery transplants to business objects
  with new node types, not new algorithms.

Gaps found during the sweep (small, independently fixable):

- `docs/pricing.md` is *claimed* protected in the `/business-consultant` command prose but is
  registered in no `protected_paths` — an unenforced claim.
- `/business-consultant` has no portable workflow copy under
  `skills/chief-wiggum/references/workflows/` (all 14 other skills do).
- `check_patterns.py` does not enforce the `success_metrics` requirement that
  `docs/patterns-registry.md` asserts ("every pattern must declare them").
- Declared pattern metrics (`k_factor`, `reward_cost_per_attributed_signup`, `mrr`, …) are
  computed by nothing — declared-but-unmeasured is exactly the state the anti-theater rule
  forbids at the plan layer.

---

## 8. Capability tracks (steered 2026-08-02, seeded as issues)

Approved order: **G → A → H → C → D now; B, E, F parked with explicit triggers.** Track A is
the spine; H closes the distribution gap the steer identified as the binding constraint
(default loop otherwise: build → no user movement → kill); C is the highest-leverage novel
gate; F closes the loop with real money data. The grounding discipline: dogfood one real bet
through A+H+C before promoting anything to `--gate`.

- **H. Channel engine** (#241) — the distribution gap is a genuine operator skills gap, so CW
  bridges it as a factory capability, in three legs: the Bullseye loop mechanized
  (channel-experiment records over the fixed 19-channel enum, ≤3 testing, exactly one
  `focused`, measured channel-CAC; referral/WOM joins via the stamped `referral-invite-loop`
  pattern, honestly framed as a multiplier needing baseline flow); buy-not-build on
  sales/marketing platforms (Meta/Google Ads behind the paid-spend unlock gate, OpenAI-class
  asset generation, ESP/CRM/SEO tooling — spend as ordinary cost-inputs) with
  revenue-triggered capability graduation M0 founder+platforms → M1 specialist → M2 agency;
  and a rep-cadence invariant (Mom-Test conversations tracked like spend, process-scored
  retros, *Traction* 50% rule). Companion amendments on #235/#237: a **distribution-fairness
  precondition** — a demand criterion firing without attempted distribution downgrades `kill`
  to `recycle`, so the operator's gap can't masquerade as market rejection.

- **A. Bet ledger + envelope + kill criteria** (#235) — the portfolio spine. Bet records in a
  dedicated portfolio repo, typed envelopes with tranches, states-and-dates kill criteria
  hashed into a ratchet-format journal, dated-criterion evaluation, bets-in-flight cap,
  harvest-gated terminal transitions. (Primitives 2.1, 2.3, 2.5.)
- **B. `/plan-bet` product stage** (#238, parked — triggers when A+C have run against ≥1 real
  bet) — `/design`-shaped: typed business-model artifact (BMO-enum fields, validation
  statuses), assumption ledger generated from premortem + financial-model cells, Fermi
  viability gate at authoring time, human checkpoint.
- **C. Validation-experiment patterns + pre-registration gate** (#236) — assumption ledger,
  test cards with hashed thresholds, falsifiability (XYZ) linter, evidence-strength floor on
  state transitions; first stamped experiment patterns: `landing-page-smoke-test` and
  `presale`. This is the RCT-validated mechanism.
- **D. Kill-review quorum** (#237) — a consult role whose evaluators receive only a generated
  brief of pre-registered criteria + measured results (fresh context enforced as an
  invariant), producing a `go/kill/hold/recycle` verdict ahead of the human decision.
- **E. Instruments as scripts** (#239, parked — per-instrument triggers: pricing need, ≥40
  eligible users, first interview-driven ASM) — Van Westendorp, Sean Ellis PMF (hashed segment
  definition), Mom Test evidence linter. Each certifiable under the gate-validation protocol.
- **F. Money-runtime invariants** (#240, parked — triggers on first monetized bet + #229
  landing) — MRR movement state machine over Stripe events, micro-scale invariant set (§4),
  REA duality/leakage check, pricing-artifact ↔ code conformance gate; also computes the first
  declared pattern `success_metrics` end-to-end.
- **G. Hygiene fixes** (#234) — register `docs/pricing.md` as protected; portable
  `/business-consultant` skill copy; linter enforcement of `success_metrics`. (Metric
  computation moved to F, where the money data lives.)

### Bet ledger usage (Track A, `scripts/bet.py`, #235)

The portfolio spine, shipped. Bets live in a dedicated private portfolio repo — default
`~/.chief-wiggum/portfolio`, overridable via `--portfolio-dir` or `CHIEF_WIGGUM_PORTFOLIO`,
git-initialized on first use:

```
<portfolio>/
├── journal.jsonl          # append-only hash chain (ratchet.py format — never hand-edit)
├── means.json             # means inventory (templates/means-schema.json), optional
└── bets/<bet-id>/
    ├── bet.json           # templates/bet-schema.json (envelope embedded — a goalpost)
    ├── kill-criteria.json # templates/kill-criteria-schema.json (a goalpost)
    ├── ledger.jsonl       # append-only spend/time/rep entries
    ├── channels.json      # optional channel-experiment records (#241)
    └── retrospective.md   # required non-trivially before `killed`
```

```bash
python3 scripts/bet.py create <bet-id> --title ... --envelope env.json --criteria kc.json
python3 scripts/bet.py spend <bet-id> --amount-usd 120 --hours 6   # or --rep (distribution rep)
python3 scripts/bet.py evaluate <bet-id> --results measured.json   # + distribution-attempted status
python3 scripts/bet.py transition <bet-id> probing                 # kill_pending / terminals / pivot
python3 scripts/bet.py transition <bet-id> killed --successor <id> --envelope ... --criteria ...  # pivot
python3 scripts/bet.py rebaseline <bet-id> --envelope new.json --reason "..."  # ONLY goalpost mutation path
python3 scripts/bet.py portfolio                                   # summary + invariants
```

The envelope and kill criteria are content-hashed into `journal.jsonl` at create; `rebaseline`
journals old → new hashes with a required `--reason`; every read verifies the chain and fails
closed (exit 4) on tamper. Gate checks — all report-only by default per `docs/gate-rollout.md`,
blocking only with `--gate`, and no workflow passes `--gate` until a `validation/bet-gates.json`
record exists: states-and-dates soundness lint; cumulative spend ≤ cumulative unlocked tranches;
dated-criterion evaluation (triggered → journaled `kill-proposed`, spend blocked pending the
journaled human accept/override); bets-in-flight cap (probing|validating|building, default 2);
bet-selection lint at create (means.json sales/marketing novice + no `focused` channel + no
ecosystem channel/owned audience in the acquisition plan); goalpost-integrity hash check.
`killed` is hard-blocked until `retrospective.md` exists non-trivially and the harvest check ran
(3.9 × TTM SDP vs wind-down cost; absent inputs → `skipped`, reported, never a silent block).
Missing optional inputs (`means.json`, `channels.json`) report `skipped`/`unattempted` — never a
crash, never silently omitted.

### Channel engine (Track H, `scripts/channel.py`, #241)

Bullseye mechanized over the bet ledger. The honest boundary, stated up front: **tooling cannot
close a doing-gap** — the irreducible core of marketing is the operator talking to people,
posting, selling. The channel engine makes those attempts cheap, scheduled, and legible
(generates everything up to the conversation), never substitutes for them; the rep-cadence
check exists to catch the displacement failure mode of building marketing tools instead of
doing marketing.

Channel-experiment records live in `bets/<bet-id>/channels.json`
(`templates/channel-experiment-schema.json`) over the **fixed 19-channel enum** (Weinberg &
Mares, *Traction*); every mutation is journaled into the portfolio hash chain. Per-channel
Bullseye states: `brainstormed → ranked → testing (≤3 concurrent) → focused | rejected`, with
`focused → testing` re-entry on saturation (journaled, `--reason` required). `brainstorm`
seeds all 19 (forces the unfashionable ones); ranking is human.

```bash
python3 scripts/channel.py brainstorm <bet-id>                  # seed all 19 channels
python3 scripts/channel.py rank <bet-id> ch1 ch2 ch3            # human ranking, recorded
python3 scripts/channel.py test <bet-id> <channel> --hypothesis "..." --budget-usd 50 --duration-days 14
python3 scripts/channel.py record <bet-id> <channel> --customers-acquired 4 --measured-cac 12 --verdict "..."
python3 scripts/channel.py focus <bet-id> <channel>             # exactly one focused
python3 scripts/channel.py reject <bet-id> <channel> --verdict "..."
python3 scripts/channel.py status <bet-id>                      # records + every gate check
```

Gate checks — all report-only by default per `docs/gate-rollout.md`, blocking only with
`--gate`:

- **experiment completeness**: a record claiming results without a measured CAC AND
  customers-acquired is invalid; a **referral experiment with no recorded baseline input
  flow is invalid the same way** — referral/WOM (`viral-marketing`) is a multiplier on an
  existing acquisition stream, never a source (Balfour: sustained K > 1 is rare). A viral
  record may carry the stamped `referral-invite-loop` pattern's declared metrics
  (`k_factor`, `invite_accept_rate`, `reward_cost_per_attributed_signup` — accepted as the
  experiment's measured CAC); this is a validation rule on the record, not an integration.
- **exactly-one-focused-channel** (micro-scale invariant #7).
- **channel-CAC ≤ target CAC** join against the bet's `target_cac_usd`
  (`bet.py create --target-cac`); no target declared → `skipped`, never a finding.
- **≤3 concurrent testing**; **zero-headcount filter** — sales-led channels
  (`sales`, `business-development`) flagged while testing/focused at headcount 0.

**Buy-not-build platforms**: CW integrates with and exports to existing sales/marketing
platforms — Meta Ads, Google Ads, OpenAI-class asset generation (AI distribution surfaces
like the GPT store count under `existing-platforms`), ESP/CRM/social/SEO/directory tooling —
and never rebuilds them. Platform subscriptions and ad spend are ordinary cost-inputs line
items (`flat_monthly`/meters in `templates/cost-inputs-schema.json`) counting against the bet
envelope. **Paid channels carry the paid-spend unlock gate** (§4 invariant 4): no
paid-acquisition spend before `pmf_score ≥ 40% (n ≥ 40)` or equivalent commitment-class
evidence, and viability requires LTV above the channel's CAC floor — at micro price points
paid ads are frequently underwater and the experiment verdict must say so. The stamped asset
layer (launch checklists per `templates/launch-checklist-schema.json` — Levels'
platform×timing×assets shape — copy scaffolds, Mom-Test scripts, prospect briefs, follow-up
sequences) is per-bet artifacts, not a pattern category. Marketing capability graduates
M0 founder+platforms → M1 focused-channel specialist → M2 agency/hire on the revenue-triggered
formulas in `templates/marketing-tiers.md`; graduation is a journaled human decision.

**Rep cadence + the *Traction* 50% rule** (ledger-side, surfaced in `bet.py evaluate`'s
distribution block and `channel.py status`): while a bet is probing|validating, ≥N Mom-Test
conversations per trailing week counted from the ledger's rep entries (default 3, per-bet
`create --cadence`, journaled) — missed cadence is a report-only finding feeding the #237
kill review as *distribution-not-attempted* evidence (a demand-kill with skipped reps
downgrades to `recycle`). Hours may carry `spend --tag product|traction`; traction share
below 0.5 while probing|validating is a finding — untagged hours never are (a finding must
come from data, not its absence).

**Process-scored retros for sales attempts** (Simonson & Staw): the retrospective grades
whether the reps happened and were run to protocol — Mom Test question forms used,
commitment asks made — **never conversion outcomes**. Early reps convert badly; that is
tuition, not signal about the product, and the scoring must not punish it.

### Validation experiments (Track C, `scripts/assumption.py`, #236)

The RCT-validated mechanism (Camuffo et al. 2020; SMJ 2024 replication, 759 firms):
pre-registered falsifiable hypotheses with quantitative decision thresholds measurably
improve killing bad ideas. Shipped as a third importing sibling of `bet.py` (the
`channel.py` placement precedent): assumptions in `bets/<bet-id>/assumptions.json`
(`templates/assumptions-schema.json` — stable `ASM-NNN` ids, status
`untested|testing|validated|falsified`, source `premortem|financial_model|canvas`,
`depends_on_element` tag), test cards in `test-cards.json`
(`templates/test-cards-schema.json`); every mutation journaled into the portfolio hash
chain (fail-closed, exit 4 on tamper).

```bash
python3 scripts/assumption.py add <bet-id> --statement "at least 5% of AU dog trainers … will …" \
    --source premortem --element customer-segment      # XYZ falsifiability lint
python3 scripts/assumption.py card <bet-id> --asm ASM-001 --method landing-page-smoke-test \
    --metric visitor_to_signup_rate_pct --comparator ">=" --value 5 --sample-min 100 \
    --evidence-strength 2                              # threshold block HASHED at creation
python3 scripts/assumption.py verdict <bet-id> TC-001 --result 7.5 --sample-n 140 \
    --verdict validated                                # only against the ORIGINAL hash
python3 scripts/assumption.py rebaseline <bet-id> TC-001 --value 3 --reason "..."  # only threshold change
python3 scripts/assumption.py check <bet-id>           # ASM↔card traceability + all lints
```

Gate checks — all report-only by default per `docs/gate-rollout.md`, blocking only with
`--gate`, same dogfood bar as the bet-ledger gates (one real ASM through card → run →
verdict before any workflow passes `--gate`):

- **Falsifiability (XYZ) lint**: hypotheses must parse to Savoia's grammar — "at least
  X% of Y will Z", numeric X, concrete Y (not "people"/"users"), measurable Z (opinion
  verbs rejected). The linter is a parser, so un-falsifiable phrasing is syntactically
  impossible.
- **Vanity-metric lint**: cumulative/gross counters (`total_*`, `lifetime_*`) rejected
  as success criteria — they falsify nothing (Ries); per-cohort rates required.
- **Pre-registration**: the threshold block (`{asm_id, metric, comparator, value,
  sample_min}`) is content-hashed into the journal at card creation; a verdict is
  validated against the original hash (a hand-lowered bar is refused), a verdict
  contradicting its own comparator is flagged, `rebaseline` is the only sanctioned
  change (journaled old→new hash, `--reason` required), and a card that was never
  journaled is itself a finding.
- **ASM↔card traceability** (`check` — `check_traceability.py`'s exact shape, new node
  types): uncovered assumptions, dangling cards, and a `validated` status with no card
  verdict behind it (the omission evasion) all fail.
- **Evidence-strength floor** (`bet.py transition <id> building`): the enum is FIXED —
  1 opinion, 2 click, 3 time, 4 reputation, 5 money — and entering `building` requires
  ≥1 validated ASM at effective strength ≥4 (Blank: purchase orders, not enthusiasm).
  Interview-class methods cap effective strength at 1 regardless of declared value or
  count (the Mom Test floor). No assumptions.json → `skipped`, never a silent block.

**Pivot dependency rule** (Bland): `bet.py transition <id> killed --successor <id2>
--changed-elements <element,…>` carries the assumption ledger to the successor and
re-opens (`validated → untested`) every ASM whose `depends_on_element` matches a
changed element — journaled as `asm-reopen`. Test cards do **not** carry: evidence was
pre-registered against the old thesis, so coverage and strength must be re-established
(carried validation can never unlock `building` by itself).

**Stamped experiment patterns** (the distinctive CW move — the factory stamps the
experiment, not just tracks it, same #135 discipline): registry category
`validation-experiment`, trust class `end-user-signal-driven`, both with stampable
scaffolds via `/apply-pattern` — `landing-page-smoke-test` (honest static page +
signup-capture instrumentation stub; strength 2–3; the cheapest universal demand test)
and `presale` (honest pre-order checkout on an unbuilt product — not-built-yet, price,
and delivery window stated before the payment step; a REAL charge is the datum;
refund-on-kill counted as wind-down cost; the only strength-5 producer). Their
pre-registration/vanity-lint invariants are grounded in `assumption.py`; the page-level
invariants are design-derived until first grounded use, flagged per the #139 allowance.
Fake-door and concierge are named follow-ups, not shipped.

### Kill-review quorum (Track D, `bet.py kill-brief` / `kill-review`, #237)

The disinterested killer of §2.4 mechanized (Boulding et al. 1997: clearer negative data
does not stop escalation — a decision maker with **no sunk costs** does). At a kill
checkpoint the continue case is argued to a fresh-context quorum that sees ONLY a
generated brief — never the bet's accumulated working context; the missing context is
the feature, preserved as a lintable invariant.

```bash
python3 scripts/bet.py evaluate <bet-id> --results r.json   # criterion fires → recommends kill-review
python3 scripts/bet.py kill-brief <bet-id>                  # render the brief (journal-backed values only)
python3 scripts/bet.py kill-review <bet-id>                 # brief → quorum → verdicts journaled
```

- **Consult role** `kill-review` (`config/providers.json`): required `codex` + `opus`
  (the claude tool pinned to the opus model via the provider entry's `model` field —
  per-provider default model, #237); optional `claude-interactive` at the standard
  `optional_timeout_seconds: 300`. Bounded charters per the lenses-not-personas
  convention (`config/lenses.json`): `evidence-sufficiency` (is the evidence enough to
  continue), `steelman-the-kill` (argue the kill — checking the distribution-attempt
  table FIRST, per the fairness amendment), `is-recycle-better` (does a pivot beat both).
- **The brief is generated, not written** (`kill-brief`): hashed kill criteria verbatim
  (cited to the create/rebaseline journal record), measured values sourced from the
  journaled `kill-proposed` evaluation rows, envelope status (spend vs tranches from the
  ledger), the open-assumption evidence table (`assumptions.json` when present), and the
  **distribution-attempt table** — channel experiments run, exposure delivered,
  rep-cadence adherence, with `unattempted` stated explicitly when no evidence exists.
  Any value the generator cannot source is `UNRESOLVED:`, never prose. **Brief purity is
  a generator self-check, not an operator gate**: every measured value must cite a
  journal record id or a `bets/<id>/` artifact file, and thesis prose must not appear —
  a violating brief is REFUSED (exit 1) unconditionally, like the retrospective guard;
  it takes no `--gate` and gets no gate-ledger row of its own.
- **Verdict schema**: `{verdict: go|kill|hold|recycle, confidence, reasons[],
  cheapest_disconfirming_test?}` in a fenced JSON block; a `hold` must name the test
  that would settle it. Malformed provider output is flagged and carried as a
  `malformed` entry — tolerated, never a crash (report-only finding, gate-ledger row).
- **Distribution-fairness rule** (#241 amendment): a demand-shaped criterion
  (direction=`has`, or explicit `demand_shaped`) that fired while distribution is
  unattempted cannot produce `kill` — a parsed kill verdict is mechanically downgraded
  to `recycle` with a finding naming the cheapest untried exposure (founder reps at $0
  before untried Bullseye channels). Zero exposure → zero signups is evidence of no
  marketing, not no demand.
- **Ordering invariant**: the human reads the quorum verdicts BEFORE the accept/override
  instructions (the fresh verdict anchors the decision, per the de-escalation
  literature); verdicts + brief hash are journaled as a `kill-review` event, and the
  decision itself stays a journaled human act (`transition <id> kill_pending` or
  `--override-kill --reason`).
- **Trigger points**: a fired criterion at `evaluate` *recommends* `kill-review`; a
  human may convene it ad hoc; nothing runs the quorum automatically — it is a
  kill-decision instrument, not a recurring review board.

---

## 9. Portfolio theses and targeting doctrine (logged 2026-08-02)

Operator theses from the steer discussion, grounded by two dedicated research passes (ecosystem
exit base rates; historical moat-collapse strategy mining). Doctrine here means **targeting
heuristics for bet selection** — every claim below is an assumption at bet level, to be
pre-registered and tested per §2, never treated as established fact. The unifying premise:
AI collapsed the cost of building software, so the *product* moat is gone economy-wide; the
question each thesis answers is where an agile solo player capitalizes on that collapse.

### 9.1 Ecosystem-wedge thesis

**Statement**: build a micro-SaaS filling a gap inside an existing SaaS ecosystem, reach the
host's customers through its marketplace, exit six figures. Decomposed and researched:

- **Distribution-structural — MIXED.** Marketplaces demonstrably deliver installs with zero
  audience, but the median listed Shopify app earns <$1k MRR, 54% of Stripe-verified indie
  products earn $0, and 500–800 new apps land monthly. A marketplace is a channel, not a moat.
- **Gaps-discoverable — MIXED.** Gaps verifiably persist, but platform-entry research (Wen &
  Zhu; Foerderer) shows hosts absorb *popular + simple* gaps — durable gaps are complex,
  niche, low-glamour, which caps growth. Complaint-corpus mining (public feature boards,
  reviews) makes gap discovery mechanizable.
- **Revenue math — leaning supported, Shopify only.** A $100–150k financial exit at the
  ~3.9× SDE marketplace median requires ~$2.5–4k MRR sustained (top-decile for listed apps);
  **sub-$1k-MRR assets clear at ~1.7× ARR, not 3.9× SDE**. The good multiple assumes the bar
  is cleared with growth intact.
- **Acquired-not-copied — SUPPORTED for Shopify/WordPress, UNSUPPORTED for
  Slack/monday/Chrome/HubSpot** (thin or absent buyer benches). Hosts buy tiny-team apps that
  control roadmap-core surfaces (Shopify/Checkout Blocks — a solo dev; Atlassian's repeated
  purchases of its own vendors); they copy popular-undifferentiated ones.

**Shortlist for an AU solo founder**: Shopify (every leg evidenced; 0% rev share to $1M;
seven funded consolidators), Atlassian Forge (home market, 100% of revenue to $1M lifetime
from 2026, host-buys-vendors precedent — but thin exit auction and 15→25% Connect take-rate
ratchet shows policy risk), WordPress as eyes-open fallback (deep buyer bench; PHP; worst
governance risk post-ACF-seizure). Landlord risk is real everywhere: Chrome deleted its
payment rails outright.

### 9.2 Neglected-incumbent thesis

**Statement**: businesses sitting on milked incumbent products (often PE-owned, R&D cut,
prices raised) are displaceable with modest effort. Mechanics:

- If the product is bad and customers stay, **the moat is switching cost, not product** — so
  the grease targets switching cost: migration importers, parallel-run modes, concierge
  onboarding from the incumbent's export format. Migration tooling is squarely AI's sweet
  spot and stampable per-target.
- **Timing = incumbent-inflicted switching events.** Price hikes, EOLs, license rug-pulls,
  PE-acquisition integrations force customers to re-evaluate anyway; the moat is momentarily
  down and the pre-positioned named alternative harvests the exodus (Broadcom/VMware→Proxmox,
  Unity→Godot, HashiCorp→OpenTofu). PE roll-ups manufacture these events on a schedule.
- **Grievance radar** (candidate capability, trigger-bound): monitor PE acquisitions of
  vertical SaaS, EOL/pricing-page changes, license alterations, review-sentiment collapse,
  "X alternative" search volume. Alert = bet-opportunity trigger with the window attached.
- **Evidence discipline**: hatred is opinion-class. "Everyone complains, nobody leaves" can
  mean the switching cost is stronger than the grievance, or the vertical won't pay. The
  pre-registered test is migration commitments (money/time currency), never sentiment.

### 9.3 The 100–1000-employee band

Not SAP/Salesforce — the mid-size SaaS band is the soft target: coordination cost is
superlinear (roadmap committees, change-review sludge) while the problems they sit on became
trivial; roadmap capture by their largest accounts permanently outranks the long tail;
**sales-led economics forbid the counter-move** (they cannot profitably chase a $20 self-serve
product down-market without wrecking quota structures — the judo lock, satisfied by
construction); many are PE-owned and in margin-extraction mode.

- **Pillage the edges, not the core** (the core still owns brand, integrations, an SEO
  fortress): the public feature-request backlog, the SMB tier their motion can't serve,
  unbuilt integrations, AU-geography/data-residency.
- **Their feature-request board is a ranked, voted, timestamped backlog of demand they
  publicly ignore** — high-vote, years-old, unshipped requests are demand evidence with
  provenance. Backlog mining composes with the grievance radar: radar finds the sludged
  incumbent, backlog mining finds the wedge.
- **Exit-leg bonus**: this band routinely makes $100k–$2M tuck-in acquisitions at VP-level
  approval — the most liquid buyer class for exactly these assets. Displacement pressure and
  acquisition appetite come from the same org chart.
- **The check**: "unshipped for five years" must be explained by organizational friction
  (opportunity), not absent willingness to pay (votes are free) — distinguished by test
  cards in commitment currency before product code is written.

### 9.4 Undercutting doctrine (historical moat-collapse mining)

Nine episodes mined (generics post-patent-cliff, PC clones, May Day brokerages + zero-commission
wave, budget airlines, open source & the strip-mining fight, Craigslist unbundling, Zoom vs
neglected WebEx, Atlassian's no-sales-force pricing, hard discounters/private label) plus the
theory layer (Christensen's conservation of attractive profits, judo strategy, modularity).
Ranked transferable mechanisms:

1. **Attack where the incumbent's margin/comp structure forbids response** (Dell vs dealer
   channels, Schwab vs commissioned brokers, Southwest vs hubs — every incumbent hybrid clone
   died: Continental Lite burned $1.2B). SaaS form: a $200/seat feature line can't be repriced
   at $20 without repricing the renewal book — the installed base is a hostage. **Screen: only
   attack standalone revenue lines**, never features the incumbent could give away to defend a
   bigger bundle — bundling-at-marginal-cost-zero (Teams vs Slack/Zoom, IE vs Netscape) is the
   one incumbent counter with a near-perfect win record.
2. **Price below the procurement threshold** (Atlassian's $10 licenses): the buyer becomes a
   team lead with a credit card and the incumbent's sales machine has no meeting to compete
   in. A distribution asymmetry disguised as a price. Accept the mid-market ceiling — for a
   solo player the ceiling is the point.
3. **Neglect arbitrage** (Zoom vs Cisco-owned WebEx; every Craigslist category): incumbent
   neglect is the only moat-gap that doesn't fight back on day one. Viability screen from the
   Craigslist-unbundling record: a trust problem worth paying for, a transaction to embed in,
   and sufficient frequency or ticket size — most unbundlers died of low frequency.
4. **Position now at the layer profit re-pools into — never plan to keep product margin.**
   The record is unanimous: profit never survives in the commoditized layer. It re-pooled in
   (a) custody of the customer relationship and balances (Schwab's net interest; airline
   loyalty programs worth more than the airlines), (b) the distribution/aggregation chokepoint
   (PBMs take 64% of generic revenue — the makers who won the disruption lost the profit),
   (c) operations/assurance on the free thing (Red Hat, MongoDB Atlas), (d) the adjacent
   proprietary bottleneck (Intel/Microsoft), (e) first-party workflow data. **AI-era
   analogues: integration position in the customer's stack (re-integration risk is the
   customer's switching cost even when rebuild cost → 0), accumulated workflow data,
   niche distribution/trust, compliance/assurance, operated service. The code itself appears
   nowhere on the historical list — plan as if your code is already worth zero.**
5. **First-filer timing** (generics' Paragraph IV; Valkey forking Redis in weeks): when a moat
   has a date — license change, EOL, price hike, a model release that trivializes a feature —
   the first credible alternative captures a brief 60–80%-of-incumbent-pricing window before
   pile-in. Expect the "authorized generic" counter: the incumbent ships its own lite/free
   version the week you launch and cuts the window's value ~half; price the window accordingly.
6. **Cost-structure fundamentalism**: undercut only from a genuinely different activity system
   (no sales force, no payroll, no legacy tax), never from thinner margins — and never
   straddle (bolting enterprise SLAs onto a $20 product is Continental-Lite-ing yourself).
   Note honestly: the AI cost advantage is shared by every other AI-era entrant — which is the
   generics condition, and exactly why element 4 (adjacency) decides who keeps the profit.
7. **Quality of first touch as the wedge when price can't be** (Zoom's one-click join):
   incumbents already have free tiers, so the wedge is the first five minutes — "installed
   and useful inside the existing stack within 10 minutes" is the integration-product
   equivalent.
8. **Capital structure as strategy**: a solo operator can hold price points a VC-backed
   competitor structurally can't (they must raise prices into their valuation; you don't).
9. **Terms as an attack surface** (chief-wiggum#277 addendum): risk appetite, liability
   acceptance and indemnity posture can be undercut on exactly like price. A mid-size
   competitor's legal function vetoes an uncapped indemnity as a matter of policy; a solo
   operator can accept it deliberately and win work the incumbent is structurally unable to
   bid — judo with **terms** rather than price as the lever. Sizing caveat: this is a real
   mechanism, not a free one — an edge you can only play once (because one bad event ends
   the portfolio) is a bet, not an edge, and it must be sized, recorded and counted like any
   other (the affordable-loss envelope's `liability_exposure` field and the portfolio-level
   uncapped-concurrency count exist for exactly this — never as a blocker on taking the risk).

**Multi-host neutrality** (from the steer): integrating with several competing hosts defeats
both the landlord (can't be evicted from rivals) and the bundle (no host can bundle across
competitors) — `provider-neutral-adapter` promoted to business strategy, and the most
acquirable position (any host buys to lock the capability). Off-marketplace integration does
NOT escape platform risk — APIs are also revocable (Twitter, Reddit); assess per host whether
the API is a product (Stripe, Shopify, HubSpot) or a moat, before building.

### 9.5 Standing screens for bet selection

The doctrine compresses to checkable screens — candidate fields/lints for `bet.json`'s
acquisition-plan and thesis blocks as Tracks H/C ground:

1. **Judo-lock test**: is the attacked feature a standalone revenue line the incumbent cannot
   reprice or bundle away without self-harm? (If bundleable at zero: do not attack.)
2. **Procurement-threshold pricing**: is the price under the buyer's no-approval limit?
3. **Adjacency plan**: which re-pooling layer does this bet own at exit (integration position /
   workflow data / distribution / compliance / operations)? A bet with none planned is the
   generics trap — flagged.
4. **Timing field**: is there a dated moat-expiry event (EOL, price hike, license change)?
   First-filer windows are events, not vibes.
5. **Unbundling viability** (for neglect plays): trust problem worth paying for + embedded
   transaction + frequency/ticket size.
6. **Displacement ratio**: incumbent revenue at risk per customer won (strategic-pressure
   proxy; underwrite on the financial floor regardless).
7. **Landlord assessment** (for ecosystem plays): API-as-product vs API-as-moat; marketplace
   ToS on competing with paid features; host's Sherlock history.

### 9.6 Low-cap micro-SaaS: the ceiling is a filter, not a moat (logged 2026-08-03)

**Statement as posed**: deliberately target markets whose total achievable revenue is so low
(say A$50–300k/yr at absolute maximum) that no funded competitor, platform incumbent or
acquirer would bother attacking. The smallness itself is the defence; the operator harvests
cash flow from a portfolio of such products.

Grounded by a **distribution-divergence sweep** rather than a literature pass: the same
self-contained brief put to five frontier non-Western models (`divergence` role, chief-wiggum#272)
plus a Mandarin A/B on two of them. Method note — these are model opinions, not sources. They
are used here the way `name_candidates.py` uses a dictionary: as an entropy source that escapes
our own priors. Numeric claims below carry their status; **nothing model-asserted may gate a
spend decision until independently verified.**

#### 9.6.1 What the ceiling actually does

Unanimous across all five distributions, and it is a correction rather than a refutation: **the
ceiling deters capital, not peers.** The rigorous form is Sutton's *Sunk Costs and Market
Structure* — where the sunk cost of entry is high, markets support few firms; where it is low,
they fragment. AI collapsed the sunk cost of building software, so the equilibrium number of
firms in any given niche rises and the rent per firm falls. The ceiling repels exactly one
entrant class, the capital-intensive one, and does nothing to the class that is actually
growing: solo operators for whom A$80k/yr is a good outcome. Kimi's formulation is the one to
keep — *the ceiling protects against sharks; it does nothing about piranhas, and you are a
piranha.*

Two corollaries the original thesis hides:

- **The ceiling caps the operator too.** There is no war chest to defend with, no capital to
  buy a quiet period, and — per §9.1's exit research — no meaningful sale.
- **The observable competitive future already exists.** Shopify apps, WordPress plugins and
  Chrome extensions are post-cost-collapse ecosystems. Rent there accrues to review stacks,
  search position and integrations — never to features.

So the thesis survives **as a selection filter** (it is necessary: without it you are crushed
by funded attackers) and dies **as a moat** (it is not sufficient: you are crushed by peers
instead). Treat "low ceiling" as a screen a candidate must pass, never as the answer to "what
defends this?".

#### 9.6.2 The moats that do work at this scale

Ranked by how well they survived cross-model challenge:

1. **System-of-record switching cost.** Records the buyer is legally required to retain, or
   years of accumulated operational history. Peers can clone features overnight; they cannot
   clone the customer's accumulated records.
2. **Channel saturation as a cornered resource.** In a 3,000-buyer market there is one
   association, one trade show, one Facebook group, a dozen keywords. The channel does not
   scale, so a second entrant *cannot buy in* — there is no inventory to purchase. This is the
   one moat that is genuinely *caused by* smallness rather than merely coexisting with it.
3. **Jurisdiction quarantine.** AU-legislated workflows are invisible to a US-centric indie
   swarm building for a market 10× larger. Convergent across four of five models. **Note the
   tension**: quarantine and the Hidden-Champions expansion clause (the same niche across 15
   countries multiplies the ceiling while staying under every radar) are mutually exclusive.
   Pick one per bet, deliberately.
4. **Maintained-compliance annuity.** Award rules, NDIS price guides, BAS/GST changes — an
   annuity of *labour*. An AI factory generates the content once; it does not want to service
   correctness for years.
5. **Suite bundling within one niche.** Own five tools used by the *same* buyer and a
   competitor must replicate the suite, not a weekend project. This is the only fleet shape
   that is a moat rather than a distraction — and it needs real interconnection, not a loose
   collection.

**This reframes the fleet.** A fleet of unrelated products divides attention and multiplies
distribution learning (which is per-market, not per-product — each new niche resets trust,
copy and channel knowledge to zero). A fleet aimed at one buyer pool amortises the channel and
compounds the moat. If we run a fleet at all, it should be depth-first.

#### 9.6.3 Fleet mechanics: attention is the budget

Per-product steady-state load, model estimates converging on the same band: support 0.5–3
hrs/wk, maintenance 0.5–1, integration babysitting 0–1.5 per live integration, plus a
context-switch floor of ~0.5 that does not shrink with product size. **~2 hrs/wk median,
1–4.5 range.** Against 15 hrs/wk minus marketing the current bet minus factory ops, the
honest fleet ceiling is **3–6 products, not 10–15** — and the most pessimistic model put the
realistic year-one number at **one**.

The empirical case against the broad portfolio is stronger than the modelling: Walling's
stair-step — the canonical teacher of this strategy — treats the small-product portfolio as a
*stepping stone to one bigger product*, not an end state, and 37signals deliberately killed or
sold everything but one product in 2014 with ~50 staff because attention dilution was binding.
*(Both model-asserted; the 37signals consolidation is well known but the framing should be
checked before it gates anything.)*

Structural choices that raise the ceiling, convergent across models: annual prepaid billing
only; technically literate desk-based buyers (trades and clinics generate support hell);
one shared stack template across the fleet — same auth, billing, deploy, monitoring; near-zero
integrations (every integration is a liability with a pulse); **no on-call products**; price
floor around A$50–100/mo.

Two of these are direct factory capabilities and are the strongest argument that *our* fleet
economics might differ from the documented failures: the shared fleet skeleton (centralised
identity, billing, logging, alerting built *before* product three, not after), and
factory-automated fleet maintenance — dependency updates, test runs, tier-1 support triage.

**Known failure mode**, named independently by every model: the zombie fleet. Products plateau,
maintenance eats marketing time, updates stop, churn quietly outpaces sales, and eighteen
months later the fleet is a graveyard producing a trickle of revenue and a pile of obligation.
The Mandarin DeepSeek run put the operator-level version of this more sharply than the English
one: *the ultimate risk is not money — it is looking at a perfectly-functioning product earning
A$300/month and experiencing it as a burden rather than a moat.*

#### 9.6.4 Australian corrections

The US corpus is systematically wrong for this operator in ways worth encoding:

- **TAM haircut.** A niche capping at A$300k/yr in the US often caps at **A$30–80k/yr** in
  Australia (~26M people vs ~330M, lower per-capita small-business density in many verticals).
  Bets modelled on US benchmarks are overstated 5–10×. *(Model-asserted; the population ratio
  is fact, the vertical-density claim is not verified.)*
- **Price ceiling.** A$49/mo is asserted as roughly the empirical AU B2B ceiling where the US
  equivalent is US$99. *(Unverified — but it interacts with the A$50–100/mo price floor above,
  and if both are true the viable window is narrow. Worth a real check.)*
- **The advisor channel.** AU SMB software purchases route through bookkeepers, BAS agents and
  accountants far more than the US corpus grasps — the direct analogue of Germany's
  *Steuerberater* channel and DATEV. Convergent across three models, and it matches the
  accountant-channel multiplier already recorded in the accrualflow dossier.
- **Enumerable buyers are trivially satisfiable here.** ABN Lookup API, the NDIS provider
  register, state licence registers, AHPRA. This makes §9.6.5's screen 1 cheap to automate.
- **Cold B2B email** is asserted legal under the Spam Act's inferred-consent provision for
  conspicuously published business addresses relevant to the offer, with unsubscribe. **This
  is a legal claim and must be verified against the actual Act before any outreach runs.**
- **R&D Tax Incentive** (~43.5% refundable for small companies) is raised as a way to stretch
  a A$5k envelope, with the caveat that software claims are actively scrutinised. Unverified;
  specialist advice required before it is counted in any envelope.

#### 9.6.5 Screens and terminal states this adds

Candidate additions to the §9.5 standing screens, in priority order. Consistent with
`docs/gate-rollout.md` these ship report-only and are validated before blocking.

**Resolved (chief-wiggum#275)**: screens 8–13 are now mechanized as `bet.json` lints
(`scripts/bet.py`'s `low_cap_screen_findings` — `enumerable_buyers_findings`,
`support_hazard_findings`, `structural_retention_findings`, `channel_existence_findings`,
`dark_matter_demand_findings`, `opportunity_cost_findings`), read from a new optional
`low_cap_screens` block (`templates/bet-schema.json`) passed at `bet.py create
--low-cap-screens <file>`. All six are tagged `screen:` (`NEVER_GATES_PREFIXES`) and so
never gate, even under `--gate`, until validated against a real candidate set — same
posture as #274's `capacity:` checks. Screens 8, 11 and 12 need genuinely external data
(a public-source buyer headcount, channel research, keyword/CPC or forum data) that CW
cannot produce itself; an absent field for any of the six is UNRESOLVED (cannot run),
never a silent pass. Screen 13's opportunity-cost benchmark reads the operator's
contracting rate from a new `means.json` field, `contracting_rate_usd_per_hour` — the
field the ledger lacked before this ticket.

8. **Enumerable buyers.** Can a spreadsheet of ≥500 named prospects be produced from public
   sources in one afternoon? No list, no distribution, no business. Also an upper bound:
   >5,000 records is a dead zone — too big for personal outbound, too small for paid.
9. **Support-obligation hazard.** Reject real-time, critical-path or regulatory-deadline
   functionality. A solo operator cannot carry an on-call obligation. **This is the same
   conclusion the rabbitry kill reached from the liability direction (#260), arrived at
   independently from the attention direction — treat the convergence as corroboration.**
10. **Structural retention.** The product stores records the buyer must retain ≥5 years, or
    runs a weekly-recurring workflow. Satisfaction is not retention.
11. **Channel existence.** At least one of: sponsorable association newsletter under $500, a
    browsed app marketplace, a trade show under 5,000 attendees, a >2k-member group.
12. **Dark-matter demand.** 10–300 searches/mo across a 10–20 keyword cluster with nonzero
    CPC, **or** ≥3 findable "what software do you use for X" threads. Note this supersedes the
    cruder "reject if search volume is high" screen two models proposed: volume alone is
    ambiguous, volume-with-commercial-intent is the signal.
13. **Opportunity-cost benchmark.** Every low-cap bet is judged against the operator's
    contracting day rate for the same hours, not against zero and not against venture
    outcomes. This is the counterfactual the startup corpus never forces, and the ledger
    currently has no field for it.

**Conflict to resolve, not paper over**: one model's screen says a high-vote public feature
request means the platform will build it natively — *reject*. §9.3 says exactly the opposite,
mining high-vote years-unshipped items as ranked demand evidence. Both are right about half of
it: vote count alone is ambiguous, and the discriminator is *why* it is unshipped. §9.3's
existing "friction-not-economics" check is that discriminator and should be stated as the
resolution rather than left implicit.

**Terminal states.** The Mandarin run surfaced a terminal the ledger does not model: a small
vertical vendor's normal ending is not a sale but **hold, extract cash, then shut down or hand
the product to a loyal customer** — routine among Japanese and Chinese sub-10-person vertical
vendors, and erased by the Silicon-Valley exit narrative. `bet.py`'s terminals were
`killed | parked | lifestyle | sold`; `lifestyle` absorbed this case but said nothing about the
wind-down. **Resolved (chief-wiggum#274)**: `wound_down` is now a distinct terminal, reachable
like every other terminal and carrying none of `killed`'s retrospective/harvest-check
discipline — a planned graceful shutdown is not the same event as a kill.

**Ledger gap found while checking this** (verified in code, not model-asserted):
`bet.py`'s `IN_FLIGHT` is `{probing, validating, building}` and `TERMINALS` includes
`lifestyle`. A live, revenue-producing product therefore consumes **zero** in-flight slots
while consuming operator hours forever — so a fleet of five lifestyle products leaves the cap
reporting "room for two more bets" with 100% of the attention budget already spent. The
`--max-in-flight` cap counts *bets being worked*, but §9.6.3 says the binding resource is
*total attention including live products*. That is the exact arithmetic behind the zombie-fleet
failure mode, and the ledger could not see it.

**Resolved (chief-wiggum#274)**: `bet.py` now MEASURES `ongoing_load_hours_per_week` per
`lifestyle` bet from the ledger's trailing hours entries (never a typed-in guess) and computes
remaining capacity as `means.hours_per_week − Σ(ongoing_load of every lifestyle bet) −
reserve_hours_per_week` — a second, independent bound alongside `--max-in-flight` (whichever
binds first is visible), plus an addition rule (only start product *n+1* once the most
recently added live product has run below its target load for two consecutive weekly
periods) and an attention kill criterion (load above 2h/wk while MRR is under $2k flags a
kill-or-redesign candidate). All three are new reinterpretations of the existing cap and so
report-only-forever until validated against a real portfolio, never wired to `--gate`
(docs/gate-rollout.md).

---

## 10. Sources (condensed)

**Portfolio / small bets**: Sarasvathy 2001 (*AMR*) & 2008; Dew, Sarasvathy, Read & Wiltbank
2009 (*SEJ*, affordable loss); Wiltbank et al. 2009 (*JBV*, angel outcomes); Arend et al. 2015
(*AMR*, critique); McGrath 1999 (*AMR*, real options & failure); McGrath & MacMillan 1995
(*HBR*, discovery-driven planning); Adner & Levinthal 2004 (*AMR*, boundary conditions);
Gompers 1995 (*J. Finance*, staging); Cooper 1990 (stage-gate); Sims 2011 (*Little Bets*);
Klein 2007 (*HBR*, premortem); Staw 1976; Simonson & Staw 1992; Boulding, Morgan & Staelin
1997 (*JMR*); Duke 2022 (*Quit*).

**Validation**: Ries 2011/2017 (innovation accounting, three-level); Blank 2005 / Blank & Dorf
2012; Maurya 2012/2016/2022 (Lean Canvas, Fermi/MSC, 90-day cycles); Fitzpatrick 2013 (*Mom
Test*); Bland & Osterwalder 2019 (*Testing Business Ideas*, test cards, 44-experiment library);
Savoia 2019 (XYZ grammar, pretotyping); Camuffo et al. 2020 (*Mgmt Science* RCT) + 2024 (*SMJ*
replication); Ellis PMF survey; Vohra/Superhuman PMF engine (First Round 2018); Toma & Gons
2021.

**Formalization**: Osterwalder 2004 PhD (Business Model Ontology) + Osterwalder, Parent &
Pigneur 2004 (CEUR-125, description-logic constraints); Gordijn & Akkermans 2003/2004
(e3-value); McCarthy 1982 + Geerts & McCarthy (REA, ISO 15944-4); Dietz 2006/2020 (DEMO);
Casadesus-Masanell & Ricart 2010/2011 (choice graphs); Balfour/Reforge 2018 (growth loops);
Zott & Amit 2010; Iveroth et al. 2013 (SBIFT) + Laatikainen et al. 2013 (cloud pricing 7-dim);
García-Fernández et al. 2025 (arXiv:2503.21444, iPricing).

**Lifecycle / practice**: Greiner 1972/1998; Churchill & Lewis 1983; Adizes 1988; Levie &
Lichtenstein 2010 (stage-model critique); Marmer et al. 2011 (Startup Genome, premature
scaling); Walling 2010/2015/2023 (stair-step); Levels 2018 (*MAKE*); Kahl 2020/2021; MicroConf
State of Independent SaaS 2020–2024; Scraping Fish Indie Hackers revenue analysis 2022;
Acquire.com multiples reports 2024–2026.

**Metrics / pricing / GTM**: Skok, SaaS Metrics 2.0 + SaaS Economics (forEntrepreneurs);
Lenny Rachitsky churn benchmarks; ChartMogul benchmark & retention reports; KeyBanc private
SaaS surveys; SaaS Capital bootstrapped benchmarks; Van Westendorp 1976; Ramanujam & Tacke
2016 (*Monetizing Innovation*); ProfitWell/Campbell value-metrics corpus; OpenView usage-based
pricing reports; Weinberg & Mares 2014 (*Traction*, Bullseye); Agrawal 2015 (T2D3, flagged
venture-only); Feld 2015 (Rule of 40, flagged venture-only).

Full per-source detail (mechanism extraction, mechanizability calls, links) lives in the six
research reports produced during the sweep; this document is their synthesis.

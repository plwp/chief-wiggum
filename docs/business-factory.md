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

### 2.2 The assumptions graph (traceability transplanted to business objects)

Discovery-driven planning (McGrath & MacMillan 1995) + Strategyzer test cards (Bland &
Osterwalder 2019) + Ries's leap-of-faith assumptions are one structure:

- **Assumption ledger**: stable IDs (`ASM-001…`), status
  `{untested, testing, validated, falsified}`, generated from a premortem (Klein 2007 — the
  seeded-defect discipline applied to the plan itself) and from every cell of the financial
  model.
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

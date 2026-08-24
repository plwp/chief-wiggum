# Pre-Bet Assessment — Aged Care Comparison Site / Aggregator (AU)

**Status: pre-bet research memo.** No bet record exists yet. Nothing here is a commitment, and
every claim below is an *assumption at bet level* per `docs/business-factory.md` §9 — to be
pre-registered and tested, never treated as established fact. When a bet id is created this
memo's contents become `competitor_sweep` + `finding` records in the portfolio repo
(`bets/<bet-id>/`), which is where it belongs long-term; it is staged here because the
portfolio repo is private and separate.

- **Date of sweep**: 2026-08-24 (revised same day after the geography was confirmed)
- **Market**: **Australia**, confirmed by the operator. Everything below is AU-specific and
  assumes the Commonwealth-regulated system — the analysis does not transfer to UK/NZ/CA, whose
  funding gates and referral pathways differ structurally.
- **Signal tier** (§9.0): **Tier C — public signal**, at the extreme end. Convergence-risk flag
  set. See §7.

---

## 1. Verdict

**Do not build the general-purpose consumer aged care comparison site.** Not because the
business model is unsound — the unit economics are genuinely good, better than I expected going
in (§4) — but because in the Australian system specifically, the customer is captured by
somebody else before they ever reach a search box.

The three findings that decide it:

1. **This is not an underserved market. It is a six-incumbent market**, one of whom has 25 years
   of compounding SEO and 1.6M annual unique visitors, and another of whom is the Commonwealth
   giving the same service away free by statute, alongside a funded free human navigator
   workforce (§2).
2. **The Australian funnel is gated, and the gate is already owned.** No one enters
   government-funded residential aged care without a mandatory assessment, and the highest-intent
   entry point — hospital discharge — is a fast-tracked clinical pathway that at least one
   incumbent has already institutionalised as a registered-hospital referral pipeline (§3). A
   comparison website competes for the residue of families who self-navigate, arriving *after*
   the shortlist has usually been handed to them.
3. **The data moat is exactly zero.** Star Ratings, provider registries and — since 1 January
   2026 — provider prices are all mandatory public government publications, refreshed quarterly
   (§6). Anyone can have the same database by Friday.

Applying `docs/business-factory.md` §9.4 mechanism 4 ("plan as if your code is already worth
zero"): the layer profit re-pools into here is *custody of the referral relationship* and
*compliance/assurance on the provider side*. A comparison website is neither.

**Where I would actually look instead**: provider-side compliance tooling (§8.1) — with an honest
caveat about that payer's own solvency.

---

## 2. Competitor sweep

Run per §9.0's mandate: quoted, named, not summarised as "no direct competitors found". That
summarising is exactly how a live twin was missed on the first real bet.

| Name | Model | Scale claim (their words) |
|---|---|---|
| [Aged Care Guide](https://www.agedcareguide.com.au/) (DPS Publishing) | Directory + paid provider profiles/banners; print + digital | "more than 25 years"; "Australia's most comprehensive and trusted aged care directory and referral platform"; "annual unique visitors number of over 1.6 million" |
| [Aged Care Decisions](https://agedcaredecisions.com.au/) | Commissioned placement matching **+ registered-hospital referral pipeline** | "Australia's largest home care provider and residential aged care comparison service"; "Hundreds of thousands of Australian families"; backed by Fortitude Investment Partners |
| [CareAbout](https://www.careabout.com.au/compare-aged-care-providers) | Commissioned placement matching, home-care led | "Australia's leading Home Care Placement service"; "helped over 130,000 families" |
| [Compare Aged Care](https://compareagedcare.org.au/) | Not-for-profit referral | "independent, not-for-profit support service"; "always free for Senior Australians and their families" |
| [Aged Care Choices](https://agedcarechoices.com.au/) | Home care comparison + switching | "find and compare home care providers or switch seamlessly" |
| [My Aged Care — Find a provider](https://www.myagedcare.gov.au/find-a-provider) | **Commonwealth, free, authoritative** | The statutory front door. Carries Star Ratings, and since Jan 2026 provider prices |
| [Care finder program](https://www.health.gov.au/our-work/care-finder-program) | **Government-funded free human navigators** | Face-to-face help; created as the Royal Commission's answer to commissioned brokers |

Plus adjacent entrants — Carevo, Homage, Mable, Aged Care Online, Support Sorted — all publishing
comparison//"top 10 provider" content against the same keywords.

**The structural problem is the last two rows.** Under §9.4 mechanism 1, the one incumbent counter
with a near-perfect win record is *bundling at marginal cost zero* (Teams vs Slack, IE vs
Netscape). Here the Commonwealth already gives away the authoritative dataset, the comparison tool,
*and* free human navigators, funded by appropriation rather than margin. That is the
zero-marginal-cost bundle pointed directly at this product, and it cannot be out-competed on price
because its price is already zero and its data is the source of record.

---

## 3. The Australian pathway — the finding that decides it

This is the section that only exists because the geography was confirmed. It is specific to the
Commonwealth system and is, on my read, the strongest argument in the memo.

**Access is gated by a mandatory government assessment.** An aged care needs assessment is "the
gateway to be able to access government subsidised home care or residential aged care" — "without
this assessment, you cannot access government-funded residential aged care." Since 9 December 2024
the former ACAT/RAS workforces have been consolidated into a **Single Assessment System**
(assessments now ACNA rather than ACAT). Every single customer, without exception, passes through
a government assessor before they can transact.

**The highest-intent entry point is the hospital, not the internet.** Around 2,500 patients
nationally are medically cleared for discharge but stuck in hospital waiting for an aged care bed.
For those people the pathway is clinical and fast-tracked:

- "The hospital social worker or discharge planner is often your main point of contact and
  coordinates meetings, explains care pathways and arranges referrals."
- The discharge planner "can request an aged care assessment through My Aged Care under the Single
  Assessment System, and requesting it on the ward allows the hospital to seek high-urgency
  approval."
- "Hospital-based referrals are managed directly by state and territory government teams and are
  often fast-tracked."

**And the incumbent has already institutionalised that channel.** Aged Care Decisions runs a
dedicated health-professionals programme: "Once a hospital is registered, clinicians, social
workers and discharge planners can easily refer cases to the Aged Care Placement Team in order to
have their discharge into residential aged care expedited."

That is a B2B2C relationship channel — hospital by hospital, social worker by social worker — and
it sits **upstream of every search query**. By the time a family Googles "compare aged care homes",
a discharge planner has frequently already handed them a shortlist, under time pressure, from a
service the hospital has an existing referral arrangement with.

The consequence for a comparison website: it is not competing for the market. It is competing for
the self-navigating residue, on the one channel (organic search) where a 25-year-old domain and a
statutory government site are strongest. **This is a distribution problem that no amount of product
quality solves**, and per CLAUDE.md's channel doctrine, tooling cannot close a doing-gap — the
irreducible core here is a person building relationships with hospital discharge teams.

---

## 4. The money — and why the model is sounder than it looks

Worth stating plainly because my first read was wrong: **the commission model is economically
excellent for the provider**, which is why six incumbents sustain on it.

Placement services charge providers a fee only on successful admission. The stated industry
benchmark is a "Placement Introduction Fee ... equivalent to approximately 8 days of average bed
day revenue".

Bed-day revenue (StewartBrown Aged Care Financial Performance Survey, Sept 2025 quarter):

| Component | $/bed day |
|---|---|
| Direct care (AN-ACC, supplements, other recurrent) | 311.04 |
| Everyday living incl. hotelling supplement | 85.88 |
| Accommodation | 44.28 |
| **Total** | **441.20** |

So a placement fee ≈ **8 × $441.20 ≈ $3,530**, payable only on admission, typically with a ~60%
short-stay rebate if the resident leaves within 21 days. Providers are barred from passing the fee
on to families.

`UNRESOLVED:` average length of stay in permanent residential care — I did not verify this figure
and it is load-bearing. But if it is even ~2 years, lifetime revenue per resident is roughly $322k,
making a $3,530 acquisition cost **around 1.1% of lifetime revenue**. An 8-day payback is not a
grudging expense; it is one of the cheapest customer-acquisition channels available to any
Australian business.

### Correction to my own first draft: occupancy is tighter than I said

My first pass used 89.9–94% occupancy and inferred ~22,000 vacant places. Confirming the Australian
focus turned up better figures, and they cut against that estimate:

> Overall occupancy reached **95.2%**, up from 94.2% a year earlier, with StewartBrown forecasting
> occupancy will remain **above 96% over the next five to 10 years** as Australia's ageing
> population grows and new bed supply remains limited.

At 95.2% of 224,493 operational places, national vacancy is closer to **~10,800 beds, not ~22,000**
— and structurally shrinking, with only ~800 new beds added against ~10,600/yr required. So the
bear case I dismissed earlier is partially right after all, in a specific way: **the pool of
inventory a placement service can be paid to fill is halving, and the forecast says it keeps
tightening for a decade.** A lead-generation business whose addressable inventory has a
ten-year downward trend is not a business to start today.

The nuance that survives: a loss-making home at 95% occupancy still has a strong incentive to fill
the last 5%, because marginal revenue is ~$441/day against near-zero marginal cost. The wallet is
open. It is just attached to a shrinking pool, and already harvested by people who got there first.

### Market context

| Metric | Value |
|---|---|
| Residential aged care market size (2025) | **$38.7bn**, +10.7% in 2025, 6.5% CAGR 2021–26 |
| Whole aged care sector (FY24-25, govt + co-contributions) | ~**AUD 44bn** |
| Providers / services | 3,000+ providers, 9,100+ services |
| Operational residential places | 224,493 |
| Permanent admissions FY2024-25 | 66,739 |
| People in permanent residential care | 196,313 (+3.4% on FY24) |
| Occupancy | 95.2%, forecast >96% for 5–10 years |
| Home care recipients (2024) | 273,000+ (+10%) |
| Support at Home waitlist (Sept 2025) | 121,000+ |
| New beds added vs needed | ~800 vs ~10,600/yr required |
| Patients bed-blocked in hospital (late 2025) | ~2,500 |

A large and growing TAM. Irrelevant when the constraint is channel access rather than demand.

---

## 5. Fermi viability (day-zero arithmetic, per §2.2)

Target: a modest solo-operator outcome of **$120k/yr gross**. (`UNRESOLVED:` operator has not
stated a minimum success criterion — this is my placeholder and the whole calculation moves with
it.)

```
$120,000 / $3,000 per placement (conservative)   =  40 placements/yr  ≈  3.3/month
At 15% qualified-enquiry → placement conversion  ≈ 270 enquiries/yr   ≈ 22/month
```

`UNRESOLVED:` the 15% conversion rate is my estimate, not a measured figure from any incumbent.

22 qualified enquiries a month is not an absurd traffic requirement. **Traffic is not the binding
constraint.** Three things are, in order of severity:

1. **Channel access (§3).** The highest-intent demand never reaches open search. Winning it means
   hospital-by-hospital relationship building against an incumbent with an existing registered
   referral programme — a field sales motion, not a website launch.
2. **Provider fee agreements must exist before the first dollar.** You cannot invoice a home you
   have no agreement with, and the enquiries you receive will be geographically scattered, so you
   need panel coverage across every region you accept enquiries from. Revenue is strictly zero
   until a signed panel exists. This is the cold-start problem, and it is free to test (§10).
3. **Every enquiry is a once-in-a-lifetime, emotionally acute event.** No repeat purchase, no
   in-household word of mouth, no retention curve, no expansion revenue. Per §9.4 mechanism 3's
   viability screen for unbundling plays — "a trust problem worth paying for, a transaction to
   embed in, and sufficient frequency or ticket size" — this passes on trust and ticket size and
   **fails hard on frequency**. Most unbundlers died of low frequency.

Note also that every incumbent runs human advisers, because families deciding this are in crisis
and want a person. The comparison interface is the commodity layer; the advisory relationship is
where the money sits. The business you would actually be starting is a small call centre with a
field sales function attached — not a software product.

`UNRESOLVED:` cost-per-click on Australian aged care keywords. I did not measure it. It decides the
paid-acquisition leg and costs an hour in a keyword planner.

---

## 6. The data layer has been nationalised

This is the finding that most changes the picture versus building this idea two years ago.

- **Star Ratings** are published quarterly as a public service-level data extract via AIHW GEN and
  health.gov.au, continuously since May 2023 (latest: August 2026). Overall rating plus four
  sub-categories — Residents' Experience, Compliance, Staffing, Quality Measures.
- **From 1 January 2026, providers must publish their most common service prices** on both My Aged
  Care and their own website, reviewed every two months with written notice via the Service and
  Support Portal.
- **The government publishes a National Summary of Support at Home Prices each quarter**, showing
  median and range of prices charged.
- Find a provider already lets families compare prices across providers.

So the government has already built and mandated the price-transparency comparison. A private
"compare aged care prices" product is now a thinner wrapper around a free official dataset than it
would have been in 2024 — and the wrapper adds no information the source lacks.

---

## 7. Convergence risk (§9.0)

This idea is **Tier C at the extreme**. "Aged care is impossible for families to navigate" is
arguably the single most publicly-documented pain in Australian social policy — it had a Royal
Commission. Every founder, and every model those founders prompt, reaches this conclusion from the
same public corpus. Six incumbents is the evidence that the convergence already happened, years
ago.

Per §9.0 this does not automatically screen the bet out — contested is sometimes the right call,
and neglect arbitrage thrives in contested markets. But it does mean any wedge must survive the
question the doctrine demands of convergent options: *why do we win a race that every competitor's
model can also see?* I could not construct an honest answer for the general comparison site.

Worth noting what the *non*-convergent signal here would be: the hospital discharge channel (§3) is
a Tier A/B relationship signal, invisible to public-corpus mining. It is the part of this market a
competitor's model does *not* hand them — but reaching it is a sales motion, not a build.

---

## 8. Where the opportunity actually is

### 8.1 Provider-side compliance tooling — the strongest candidate, with a caveat

The regulatory calendar has manufactured recurring, dated, legislated chores across 3,000+
providers, and unlike families they are repeat customers:

- Price publication on My Aged Care **plus** own website, **reviewed every two months** from
  1 Jan 2026 — a permanent recurring compliance treadmill with an explicit cadence.
- The Department is "actively monitoring providers ... and will engage with relevant providers who
  are non-compliant with My Aged Care pricing transparency requirements."
- Star Ratings from the May 2026 update require homes to meet **both** legislated care-minutes
  targets (total and registered-nurse) to score 3+ stars on Staffing — a metric with direct
  commercial consequence that operators now actively manage.
- Aged Care Act 2024 + Aged Care Rules 2025 in force since 1 Nov 2025; new registration
  categories, obligations, agreements.

This sits exactly where §9.4 mechanism 4 says profit re-pools — compliance and assurance — rather
than in the commoditised comparison layer.

**The caveat, stated because it is the same trap as §4.** This payer is itself insolvent at the
margin. Operating deficit averages **$1.04M per home** for the six months to 31 Dec 2025; the
sector ran **-$9.80/bed day** in 1H FY26 against a +$1.56 surplus a year earlier; and "up to 75
percent of aged care facilities are expected to lose money" absent funding reform. Loss-making
buyers have acute pain *and* constrained budgets — which means low price points, immediate
demonstrable ROI, and a long sales cycle through finance. That is a real business, but it is not an
easy one, and anyone pitching it should price accordingly rather than assuming a $38.7bn sector
implies $38.7bn of software budget.

Geography matters for targeting here. By Modified Monash category, losses per bed day run: **MM1
metro -$8.17, MM2 regional -$9.55, MM3 large rural towns -$8.08** (the best-performing category,
and still loss-making). Inner and outer regional facilities are named as most at risk. There is no
category with spare cash; MM3 and MM1 are the least-bad starting points.

### 8.2 Conflict-free fee-for-service advisory

The commission model's structural weakness is that advisers only recommend providers who pay. That
is the core criticism of the archetype: in the US, A Place for Mom drew a Senate Special Committee
on Aging probe (Sen. Casey, June 2024) for "misleading older adults and their families by claiming
that it is an unbiased and no-cost recommendation and referral service", and settled a TCPA class
action for $6M. Fee-based brokers in Australia already market against this: charging clients
directly "removes the conflict of interest that comes with commission-based models."

Real positioning, genuinely differentiated. But it is a human services business with a headcount
ceiling, competing with a free government care-finder workforce, and still subject to the §3
channel problem.

### 8.3 Narrow segment verticals

Incumbents are generalist. Dementia-specific, CALD/language-specific, LGBTI+, or single-region
plays are defensible in a way the national generalist site is not — and a single-region play is the
only version of the original idea where the §3 channel problem is tractable, because "every
discharge planner in one city" is a finite, walkable list. Shrinks the market substantially, which
for a solo operator may be the point (§9.4 mechanism 2: "accept the ceiling — for a solo player the
ceiling is the point").

---

## 9. Regulatory tail risk on the commission model

If the bet is taken anyway, this is the risk to price in. Commissions in this sector are under
sustained, active scrutiny:

- Placement services were examined at the **Royal Commission into Aged Care Quality and Safety**;
  concerns centred on placement professionals "recommending providers offering the highest
  incentive payments rather than the facility most suitable for their client."
- The government's response was to fund **care finders** — free, non-commissioned navigators.
  Providers argued they "should be employed by the government, as is the case in the United
  Kingdom, to prevent the care finder's role from being compromised."
- Conflict-of-interest duties for supporters now sit under the **Aged Care Act 2024**.
- **The NDIS explicitly bans commissions and kickbacks** — a direct domestic precedent for how
  this could be regulated in aged care.
- **ABC, 4 Dec 2025**: home care providers pressuring equipment suppliers for "monetary return"
  and commissions, with refusers "blacklisted or no longer included on an 'exclusive list'",
  triggered by the Support at Home care-management fee cap being halved from 20% to a pooled 10%
  from 1 Nov 2025. Commission flows in this sector are live news, in the wrong way.

A business whose entire revenue line is a commission the regulator is actively looking at is
carrying a policy risk that cannot be hedged. The §3 channel makes this sharper, not softer: a
commissioned broker embedded in public-hospital discharge planning is precisely the arrangement a
future inquiry would look at first.

---

## 10. Cheapest disconfirming tests

Per §2.2, pre-registered, XYZ-grammar, thresholds set before running. All three are under $1,000
and inside three months. Run them in order; the first is free and attacks the binding constraint.

**ASM-001 — the channel test (attacks §3, the binding constraint, costs $0)**
> At least 25% of hospital discharge planners / aged care needs assessors contacted in one
> Australian metro region will agree to a 20-minute conversation about how they currently shortlist
> residential providers, and at least half of those will name an existing placement service they
> already refer to.

Method: 20 approaches to discharge social workers across 3–4 hospitals. Metric:
`meeting_accept_rate_pct`, `>= 25`, sample_min 20; secondary `incumbent_named_rate_pct`.
Evidence strength 4 (reputation/commitment). **Run this before anything else.** If most already
refer to Aged Care Decisions or similar, the channel is closed and the rest is academic.

**ASM-002 — the panel test ($0)**
> At least 30% of residential aged care homes contacted with a current vacancy in one MM1 or MM3
> region will agree in principle to a placement-fee agreement with a new, unbranded entrant within
> 30 days of first contact.

Method: 20 direct approaches. Metric: `agreements_in_principle_rate_pct`, `>= 30`, sample_min 20.
Evidence strength 4. No panel, no revenue, at any traffic volume.

**ASM-003 — the pivot test for §8.1 (costs $0, and is the more interesting one)**
> At least 20% of Support at Home providers contacted will confirm they currently update My Aged
> Care prices manually on the two-monthly cycle, and will name a monthly price they would pay to
> automate it.

Method: 20 Mom Test conversations, no product. Metric: `named_willingness_to_pay_rate_pct`,
`>= 20`, sample_min 20. Evidence strength 4. This tests §8.1 for the same zero dollars and is, on
my read, the higher-expected-value experiment.

The paid-search test from the first draft has been **demoted**. Measuring cost-per-enquiry on open
search answers a question that only matters if §3 resolves favourably; spending on it first would
be optimising the channel that carries the least intent.

---

## 11. What I did not verify

Stated explicitly rather than left implied, per the unknowns discipline:

- `UNRESOLVED:` average length of stay in permanent residential care — load-bearing for the
  provider-ROI argument in §4.
- `UNRESOLVED:` actual enquiry→placement conversion rates at any incumbent (§5 uses my estimate).
- `UNRESOLVED:` what share of residential admissions originate from hospital discharge versus
  community self-referral. This is now **the most important open number in the memo** — it sizes
  §3's channel-capture argument directly, and I could not find it. AIHW GEN may hold it.
- `UNRESOLVED:` how many hospitals are registered with Aged Care Decisions' health-professional
  programme, and whether such arrangements are exclusive.
- `UNRESOLVED:` cost-per-click / cost-per-lead on AU aged care keywords (§5).
- `UNRESOLVED:` revenue, headcount or profitability of any incumbent — PitchBook/Crunchbase
  profiles exist but returned no financials.
- `UNRESOLVED:` whether the Aged Care Rules 2025 impose any specific disclosure or prohibition on
  third-party placement commissions. Searches surfaced conflict-of-interest duties for *supporters
  and care finders* but nothing directly governing commercial placement brokers. This should be
  read in the Rules themselves before any commitment.
- Three sources were unreachable from this environment (blocked by the egress proxy):
  `agedcaredecisions.com.au`, `carevo.com.au`, `hellocare.com.au`. Their fee-schedule,
  health-professional-programme and kickback-controversy detail is reported here second-hand via
  search result text, not read at source, and should be confirmed directly.

---

## Sources

- [CareAbout — Compare Aged Care Providers](https://www.careabout.com.au/compare-aged-care-providers) · [ACAT assessment guide](https://www.careabout.com.au/aged-care/assessment)
- [Aged Care Decisions](https://agedcaredecisions.com.au/) · [Partner providers](https://agedcaredecisions.com.au/partnerproviders/) · [For health professionals](https://agedcaredecisions.com.au/for-health-professionals/) · [Hospital discharge to aged care](https://agedcaredecisions.com.au/hospital-discharge-to-aged-care/)
- [Aged Care Guide](https://www.agedcareguide.com.au/) · [About](https://www.agedcareguide.com.au/about) · [Advertise](https://www.agedcareguide.com.au/advertise)
- [Compare Aged Care](https://compareagedcare.org.au/) · [Aged Care Choices](https://agedcarechoices.com.au/)
- [My Aged Care — Find a provider](https://www.myagedcare.gov.au/find-a-provider) · [Support at Home](https://www.myagedcare.gov.au/support-at-home) · [Support at Home pricing changes](https://www.myagedcare.gov.au/news-and-updates/support-home-pricing-changes) · [Help from a care finder](https://www.myagedcare.gov.au/help-care-finder)
- [Star Ratings — how they work](https://www.health.gov.au/our-work/star-ratings-for-residential-aged-care/how-star-ratings-works) · [Quarterly data extracts](https://www.health.gov.au/resources/collections/star-ratings-quarterly-data-extracts) · [AIHW GEN, Aug 2026 extract](https://www.gen-agedcaredata.gov.au/resources/access-data/2026/august/star-ratings-quarterly-data-extract-august-2026)
- [Support to leave hospital — fact sheet (Dept. of Health)](https://www.health.gov.au/sites/default/files/2024-11/support-to-leave-hospital-fact-sheet.pdf) · [Consumer protections for Support at Home prices](https://www.health.gov.au/sites/default/files/2025-10/consumer-protections-for-support-at-home-prices-fact-sheet-for-providers_0.pdf) · [Prices for Support at Home participants](https://www.health.gov.au/our-work/support-at-home/charging-for-support-at-home-services/prices-for-support-at-home-participants)
- [StewartBrown — Aged Care Financial Performance Survey, Sept 2025](https://www.stewartbrown.com.au/images/documents/StewartBrown_-_Aged_Care_Financial_Performance_Survey_Report_September_2025.pdf) · [Dec 2025 analysis](https://stewartbrown.com.au/aged-care-articles/stewartbrown-aged-care-financial-performance-survey-analysis-report-december-2025) · [Support at Home Pricing and Margin Analysis, Feb 2026](https://stewartbrown.com.au/images/documents/StewartBrown_-_Support_at_Home_Pricing_and_Margin_Analysis_February%202026.pdf)
- [Australian Ageing Agenda — Resi care profit falls as occupancy hits 95 per cent](https://www.australianageingagenda.com.au/facility-operations/resi-care-profit-falls-as-occupancy-hits-95-per-cent/) · [Inside Ageing — More homes fall into the red despite rising occupancy](https://insideageing.com.au/more-aged-care-homes-fall-into-the-red-despite-rising-occupancy-stewartbrown-warns/)
- [AIHW GEN — Admissions into aged care](https://www.gen-agedcaredata.gov.au/topics/admissions-into-aged-care) · [People using aged care](https://www.gen-agedcaredata.gov.au/topics/people-using-aged-care)
- [IBISWorld — Aged Care Residential Services in Australia, market size](https://www.ibisworld.com/australia/market-size/aged-care-residential-services/5531/) · [KPMG — Aged care sector analysis 2026](https://kpmg.com/au/en/insights/industry/aged-care-market-analysis.html) · [Productivity Commission — RoGS 2026](https://www.pc.gov.au/ongoing/report-on-government-services/community-services/aged-care-services/)
- [Care finder program](https://www.health.gov.au/our-work/care-finder-program) · [Conflicts of interest policy](https://www.health.gov.au/resources/publications/conflicts-of-interest-policy)
- [MinterEllison — Aged Care Act 2024 now in force](https://www.minterellison.com/articles/commencement-of-the-new-aged-care-act) · [Aged Care Act 2024, Federal Register of Legislation](https://www.legislation.gov.au/C2024A00104/latest/text)
- [ABC — Aged home care package changes set off cash conflict between providers and suppliers (4 Dec 2025)](https://www.abc.net.au/news/2025-12-04/home-care-package-kickbacks-row-between-providers-suppliers/106095962) · [Royal commission hears kickbacks offered to secure home care clients](https://www.abc.net.au/news/2019-03-20/home-care-business-offered-kickbacks-for-clients-after-sanction/10915638) · [Refunds but no price caps (19 May 2026)](https://www.abc.net.au/news/2026-05-19/refunds-but-no-price-caps-for-ripped-off-older-australians/106697598)
- [AASP — Placement Services in the Spotlight at the Royal Commission](https://www.aasp.org.au/2020/11/06/placement-services-in-the-spotlight-at-the-royal-commission/) · [Hellocare — Care finders receiving kickbacks for aged care placements](https://hellocare.com.au/care-finders-receive-kickbacks-aged-care-placements/)
- [McKnight's — Sen. Casey calls out A Place for Mom](https://www.mcknightsseniorliving.com/news/sen-casey-calls-out-a-place-for-mom-over-potentially-deceptive-business-practices/) · [NBC — Senate announces probe of A Place for Mom](https://www.nbcnews.com/news/us-news/senate-announces-probe-place-for-mom-referral-service-rcna157282) · [Forbes — 'Free' for-profit senior services referrals: buyer beware](https://www.forbes.com/sites/howardgleckman/2024/07/03/free-for-profit-senior-services-referrals-buyer-beware/)
- [The Senior — Problems with My Aged Care portal revealed in new report](https://www.thesenior.com.au/story/9126726/flawed-problems-with-my-aged-care-portal-revealed-in-new-report/) · [The Conversation / UQ — Australians wait 12 months for aged care](https://theconversation.com/australians-wait-12-months-for-aged-care-and-the-latest-budget-funding-is-unlikely-to-change-that-282960)

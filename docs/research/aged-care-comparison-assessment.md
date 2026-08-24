# Pre-Bet Assessment — Aged Care Comparison Site / Aggregator (AU)

**Status: pre-bet research memo.** No bet record exists yet. Nothing here is a commitment, and
every claim below is an *assumption at bet level* per `docs/business-factory.md` §9 — to be
pre-registered and tested, never treated as established fact. When a bet id is created this
memo's contents become `competitor_sweep` + `finding` records in the portfolio repo
(`bets/<bet-id>/`), which is where it belongs long-term; it is staged here because the
portfolio repo is private and separate.

- **Date of sweep**: 2026-08-24
- **Signal tier** (§9.0): **Tier C — public signal**, at the extreme end. Convergence-risk flag
  set. See §6.
- **Market assumed**: Australia. (`UNRESOLVED:` operator has not stated a geography — the whole
  memo is AU-specific and would need re-running for UK/NZ/CA.)

---

## 1. Verdict

**Do not build the general-purpose consumer aged care comparison site.** Not because the
business model is unsound — the unit economics are genuinely good, better than I expected going
in (§3) — but because every scarce asset the model needs is already owned by somebody else, and
none of the assets a new entrant can create are scarce.

The three findings that decide it:

1. **This is not an underserved market. It is a six-incumbent market**, one of whom has 25 years
   of compounding SEO and 1.6M annual unique visitors, and another of whom is the federal
   government giving the same service away free by policy (§2).
2. **The product is a call centre, not a website.** Every incumbent runs human advisers, because
   families making this decision are in acute crisis and want a person. The comparison interface
   is the commodity layer; the advisory relationship is where the money actually sits (§4).
3. **The data moat is exactly zero.** Star Ratings, provider registries and — since 1 January
   2026 — provider prices are all mandatory public government publications, refreshed quarterly
   (§5). Anyone can have the same database by Friday.

Applying `docs/business-factory.md` §9.4 mechanism 4 ("plan as if your code is already worth
zero"): the layer profit re-pools into here is *custody of the family relationship* and
*compliance/assurance on the provider side*. A comparison website is neither.

**Where I would actually look instead**: provider-side compliance tooling (§7.1). The payer has
acute, dated, legislated pain and — unlike families — is a repeat customer.

---

## 2. Competitor sweep

Run per §9.0's mandate: quoted, named, not summarised as "no direct competitors found". That
summarising is exactly how a live twin was missed on the first real bet.

| Name | Model | Scale claim (their words) |
|---|---|---|
| [Aged Care Guide](https://www.agedcareguide.com.au/) (DPS Publishing) | Directory + paid provider profiles/banners; print + digital | "more than 25 years"; "Australia's most comprehensive and trusted aged care directory and referral platform"; "annual unique visitors number of over 1.6 million" |
| [Aged Care Decisions](https://agedcaredecisions.com.au/) | Commissioned placement matching | "Australia's largest home care provider and residential aged care comparison service"; "Hundreds of thousands of Australian families"; backed by Fortitude Investment Partners |
| [CareAbout](https://www.careabout.com.au/compare-aged-care-providers) | Commissioned placement matching, home-care led | "Australia's leading Home Care Placement service"; "helped over 130,000 families" |
| [Compare Aged Care](https://compareagedcare.org.au/) | Not-for-profit referral | "independent, not-for-profit support service"; "always free for Senior Australians and their families" |
| [Aged Care Choices](https://agedcarechoices.com.au/) | Home care comparison + switching | "find and compare home care providers or switch seamlessly" |
| [My Aged Care — Find a provider](https://www.myagedcare.gov.au/find-a-provider) | **Federal government, free, authoritative** | The statutory front door. Carries Star Ratings, and since Jan 2026 provider prices |
| [Care finder program](https://www.health.gov.au/our-work/care-finder-program) | **Government-funded free human navigators** | Face-to-face help; created as the Royal Commission's answer to commissioned brokers |

Plus adjacent entrants — Carevo, Homage, Mable, Aged Care Online, Support Sorted — all publishing
comparison//"top 10 provider" content against the same keywords.

**The structural problem is the last two rows.** Under §9.4 mechanism 1, the one incumbent counter
with a near-perfect win record is *bundling at marginal cost zero* (Teams vs Slack, IE vs
Netscape). Here the government already gives away the authoritative dataset, the comparison tool,
*and* free human navigators, funded by appropriation rather than margin. That is the
zero-marginal-cost bundle pointed directly at this product, and it cannot be out-competed on price
because its price is already zero and its data is the source of record.

---

## 3. The money — and why the model is sounder than it looks

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

Now the provider's side of it. `UNRESOLVED:` average length of stay in permanent residential care
— I did not verify this figure and it is load-bearing. But if it is even ~2 years, lifetime
revenue per resident is roughly $322k, making a $3,530 acquisition cost **around 1.1% of lifetime
revenue**. Against that, an 8-day payback is not a grudging expense; it is one of the cheapest
customer-acquisition channels available to any Australian business.

This matters because it kills the obvious bear case. I expected to argue "occupancy is 89.9–94%
and 61% of homes run at a loss, so nobody will pay for leads." That argument is weak: a
loss-making home at 90% occupancy has an *unusually strong* incentive to fill the last 10%,
because marginal revenue per bed-day is ~$441 against near-zero marginal cost. Roughly 22,000
places sit vacant nationally (224,493 operational places at ~90%), though StewartBrown notes many
are unusable due to staffing shortages or refurbishment.

**Conclusion: the wallet is real and open. It is simply already being harvested by people who got
there first.** The constraint is competitive, not economic.

### Demand-side context

| Metric | Value |
|---|---|
| Providers / services | 3,000+ providers, 9,100+ services |
| Operational residential places | 224,493 |
| Permanent admissions FY2024-25 | 66,739 |
| People in permanent residential care | 196,313 (+3.4% on FY24) |
| Home care recipients (2024) | 273,000+ (+10%) |
| Support at Home waitlist (Sept 2025) | 121,000+ |
| New beds added vs needed | ~800 vs ~10,600/yr required |
| Patients bed-blocked in hospital (late 2025) | ~2,500 |

---

## 4. Fermi viability (day-zero arithmetic, per §2.2)

Target: a modest solo-operator outcome of **$120k/yr gross**. (`UNRESOLVED:` operator has not
stated a minimum success criterion — this is my placeholder and the whole calculation moves with
it.)

```
$120,000 / $3,000 per placement (conservative)   =  40 placements/yr  ≈  3.3/month
At 15% qualified-enquiry → placement conversion  ≈ 270 enquiries/yr   ≈ 22/month
```

`UNRESOLVED:` the 15% conversion rate is my estimate, not a measured figure from any incumbent.

22 qualified enquiries a month is not an absurd traffic requirement. **Traffic is not the binding
constraint.** Two things are:

1. **Provider fee agreements must exist before the first dollar.** You cannot invoice a home you
   have no agreement with, and the enquiries you receive will be geographically scattered, so you
   need panel coverage across every region you accept enquiries from. Revenue is strictly zero
   until a signed panel exists. This is the cold-start problem, and it is the thing to test first
   because it is free to test (§8).
2. **Every enquiry is a once-in-a-lifetime, emotionally acute event.** There is no repeat
   purchase, no in-household word of mouth, no retention curve, no expansion revenue. Per §9.4
   mechanism 3's viability screen for unbundling plays — "a trust problem worth paying for, a
   transaction to embed in, and sufficient frequency or ticket size" — this passes on trust and
   ticket size and **fails hard on frequency**. Most unbundlers died of low frequency. Every
   customer must be bought fresh, forever.

Which turns the whole thing into a customer-acquisition-cost race against a 25-year-old domain
with 1.6M annual visitors and a government site with statutory authority.

`UNRESOLVED:` cost-per-click on Australian aged care keywords. I did not measure it. It is the
single number that decides the paid-acquisition leg and should be checked before anything else is
built — it costs nothing but an hour in a keyword planner.

---

## 5. The data layer has been nationalised

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

## 6. Convergence risk (§9.0)

This idea is **Tier C at the extreme**. "Aged care is impossible for families to navigate" is
arguably the single most publicly-documented pain in Australian social policy — it had a Royal
Commission. Every founder, and every model those founders prompt, reaches this conclusion from the
same public corpus. Six incumbents is the evidence that the convergence already happened, years
ago.

Per §9.0 this does not automatically screen the bet out — contested is sometimes the right call,
and neglect arbitrage thrives in contested markets. But it does mean any wedge must survive the
question the doctrine demands of convergent options: *why do we win a race that every competitor's
model can also see?* I could not construct an honest answer for the general comparison site.

---

## 7. Where the opportunity actually is

### 7.1 Provider-side compliance tooling — the strongest candidate

The regulatory calendar has manufactured recurring, dated, legislated chores across 3,000+
providers, and unlike families they are repeat customers with budgets:

- Price publication on My Aged Care **plus** own website, **reviewed every two months** from
  1 Jan 2026 — a permanent recurring compliance treadmill with an explicit cadence.
- The Department is "actively monitoring providers ... and will engage with relevant providers who
  are non-compliant with My Aged Care pricing transparency requirements."
- Star Ratings from the May 2026 update require homes to meet **both** legislated care-minutes
  targets (total and registered-nurse) to score 3+ stars on Staffing — a metric with direct
  commercial consequence that operators now actively manage.
- Aged Care Act 2024 + Aged Care Rules 2025 in force since 1 Nov 2025; new registration
  categories, obligations, agreements.
- Payer pain is acute: average operating deficit of **$1.04M per home** for the six months to
  31 Dec 2025; **$9.80/bed-day deficit** in 1H FY26 against a $1.56 surplus a year earlier; 61% of
  homes operating at a loss.

This sits exactly where §9.4 mechanism 4 says profit re-pools — compliance and assurance — rather
than in the commoditised comparison layer. It is a different product from the one asked about, but
it is in the same market and reachable with the same domain research.

### 7.2 Conflict-free fee-for-service advisory

The commission model's structural weakness is that advisers only recommend providers who pay. That
is the core criticism of the archetype: in the US, A Place for Mom drew a Senate Special Committee
on Aging probe (Sen. Casey, June 2024) for "misleading older adults and their families by claiming
that it is an unbiased and no-cost recommendation and referral service", and settled a TCPA class
action for $6M. Fee-based brokers in Australia already market against this: charging clients
directly "removes the conflict of interest that comes with commission-based models."

Real positioning, genuinely differentiated. But it is a human services business with a headcount
ceiling, not a website — and it competes with a free government care-finder workforce.

### 7.3 Narrow segment verticals

Incumbents are generalist. Dementia-specific, CALD/language-specific, LGBTI+, or single-region
plays are defensible in a way the national generalist site is not. Shrinks the market
substantially, which for a solo operator may be the point (§9.4 mechanism 2: "accept the ceiling —
for a solo player the ceiling is the point").

---

## 8. Regulatory tail risk on the commission model

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
carrying a policy risk that cannot be hedged.

---

## 9. Cheapest disconfirming tests

Per §2.2, pre-registered, XYZ-grammar, thresholds set before running. All three are under $1,000
and inside three months. Run them in order; the first is free and attacks the binding constraint.

**ASM-001 — the panel test (attacks §4's binding constraint, costs $0)**
> At least 30% of residential aged care homes contacted with a current vacancy in one metro region
> will agree in principle to a placement-fee agreement with a new, unbranded entrant within 30 days
> of first contact.

Method: 20 direct approaches. Metric: `agreements_in_principle_rate_pct`, `>= 30`, sample_min 20.
Evidence strength 4 (reputation/commitment). **If this fails, nothing else matters** — no panel,
no revenue, at any traffic volume.

**ASM-002 — the acquisition-cost test (~$300)**
> At least 2% of visitors arriving from paid search on aged care placement keywords will complete a
> qualified enquiry form at a blended cost per qualified enquiry under $150.

Method: small paid-search buy against a single honest landing page. Metric:
`cost_per_qualified_enquiry_aud`, `<= 150`, sample_min 100 clicks. Evidence strength 2–3. Note the
push-motion unlock gate in CLAUDE.md — paid spend normally waits on validated demand, so treat this
as a deliberately-scoped instrument, not a launch.

**ASM-003 — the pivot test for §7.1 (costs $0, and is the more interesting one)**
> At least 20% of Support at Home providers contacted will confirm they currently update My Aged
> Care prices manually on the two-monthly cycle, and will name a monthly price they would pay to
> automate it.

Method: 20 Mom Test conversations, no product. Metric: `named_willingness_to_pay_rate_pct`,
`>= 20`, sample_min 20. Evidence strength 4. This tests §7.1 for the same zero dollars and is, on
my read, the higher-expected-value experiment.

---

## 10. What I did not verify

Stated explicitly rather than left implied, per the unknowns discipline:

- `UNRESOLVED:` average length of stay in permanent residential care — load-bearing for the
  provider-ROI argument in §3.
- `UNRESOLVED:` actual enquiry→placement conversion rates at any incumbent (§4 uses my estimate).
- `UNRESOLVED:` cost-per-click / cost-per-lead on AU aged care keywords (§4).
- `UNRESOLVED:` revenue, headcount or profitability of any incumbent — PitchBook/Crunchbase
  profiles exist but returned no financials.
- `UNRESOLVED:` whether the Aged Care Rules 2025 impose any specific disclosure or prohibition on
  third-party placement commissions. Searches surfaced conflict-of-interest duties for *supporters
  and care finders* but nothing directly governing commercial placement brokers. This should be
  read in the Rules themselves before any commitment.
- Three sources were unreachable from this environment (blocked by the egress proxy):
  `agedcaredecisions.com.au`, `carevo.com.au`, `hellocare.com.au`. Their fee-schedule and
  kickback-controversy detail is reported here second-hand via search result text, not read at
  source, and should be confirmed directly.

---

## Sources

- [CareAbout — Compare Aged Care Providers](https://www.careabout.com.au/compare-aged-care-providers)
- [Aged Care Decisions](https://agedcaredecisions.com.au/) · [Partner providers](https://agedcaredecisions.com.au/partnerproviders/)
- [Aged Care Guide](https://www.agedcareguide.com.au/) · [About](https://www.agedcareguide.com.au/about) · [Advertise](https://www.agedcareguide.com.au/advertise)
- [Compare Aged Care](https://compareagedcare.org.au/) · [Aged Care Choices](https://agedcarechoices.com.au/)
- [My Aged Care — Find a provider](https://www.myagedcare.gov.au/find-a-provider) · [Support at Home](https://www.myagedcare.gov.au/support-at-home) · [Support at Home pricing changes](https://www.myagedcare.gov.au/news-and-updates/support-home-pricing-changes) · [Help from a care finder](https://www.myagedcare.gov.au/help-care-finder)
- [Star Ratings — how they work](https://www.health.gov.au/our-work/star-ratings-for-residential-aged-care/how-star-ratings-works) · [Quarterly data extracts](https://www.health.gov.au/resources/collections/star-ratings-quarterly-data-extracts) · [AIHW GEN, Aug 2026 extract](https://www.gen-agedcaredata.gov.au/resources/access-data/2026/august/star-ratings-quarterly-data-extract-august-2026)
- [Consumer protections for Support at Home prices (fact sheet)](https://www.health.gov.au/sites/default/files/2025-10/consumer-protections-for-support-at-home-prices-fact-sheet-for-providers_0.pdf) · [Prices for Support at Home participants](https://www.health.gov.au/our-work/support-at-home/charging-for-support-at-home-services/prices-for-support-at-home-participants)
- [StewartBrown — Aged Care Financial Performance Survey, Sept 2025](https://www.stewartbrown.com.au/images/documents/StewartBrown_-_Aged_Care_Financial_Performance_Survey_Report_September_2025.pdf) · [Dec 2025 analysis](https://stewartbrown.com.au/aged-care-articles/stewartbrown-aged-care-financial-performance-survey-analysis-report-december-2025) · [Support at Home Pricing and Margin Analysis, Feb 2026](https://stewartbrown.com.au/images/documents/StewartBrown_-_Support_at_Home_Pricing_and_Margin_Analysis_February%202026.pdf)
- [AIHW GEN — Admissions into aged care](https://www.gen-agedcaredata.gov.au/topics/admissions-into-aged-care) · [People using aged care](https://www.gen-agedcaredata.gov.au/topics/people-using-aged-care)
- [KPMG — Aged care sector analysis 2026](https://kpmg.com/au/en/insights/industry/aged-care-market-analysis.html) · [Productivity Commission — RoGS 2026, Aged care services](https://www.pc.gov.au/ongoing/report-on-government-services/community-services/aged-care-services/)
- [Care finder program](https://www.health.gov.au/our-work/care-finder-program) · [Conflicts of interest policy](https://www.health.gov.au/resources/publications/conflicts-of-interest-policy)
- [MinterEllison — Aged Care Act 2024 now in force](https://www.minterellison.com/articles/commencement-of-the-new-aged-care-act) · [Aged Care Act 2024, Federal Register of Legislation](https://www.legislation.gov.au/C2024A00104/latest/text)
- [ABC — Aged home care package changes set off cash conflict between providers and suppliers (4 Dec 2025)](https://www.abc.net.au/news/2025-12-04/home-care-package-kickbacks-row-between-providers-suppliers/106095962) · [Royal commission hears kickbacks offered to secure home care clients](https://www.abc.net.au/news/2019-03-20/home-care-business-offered-kickbacks-for-clients-after-sanction/10915638) · [Refunds but no price caps (19 May 2026)](https://www.abc.net.au/news/2026-05-19/refunds-but-no-price-caps-for-ripped-off-older-australians/106697598)
- [AASP — Placement Services in the Spotlight at the Royal Commission](https://www.aasp.org.au/2020/11/06/placement-services-in-the-spotlight-at-the-royal-commission/) · [Hellocare — Care finders receiving kickbacks for aged care placements](https://hellocare.com.au/care-finders-receive-kickbacks-aged-care-placements/)
- [McKnight's — Sen. Casey calls out A Place for Mom](https://www.mcknightsseniorliving.com/news/sen-casey-calls-out-a-place-for-mom-over-potentially-deceptive-business-practices/) · [NBC — Senate announces probe of A Place for Mom](https://www.nbcnews.com/news/us-news/senate-announces-probe-place-for-mom-referral-service-rcna157282) · [Forbes — 'Free' for-profit senior services referrals: buyer beware](https://www.forbes.com/sites/howardgleckman/2024/07/03/free-for-profit-senior-services-referrals-buyer-beware/)
- [The Senior — Problems with My Aged Care portal revealed in new report](https://www.thesenior.com.au/story/9126726/flawed-problems-with-my-aged-care-portal-revealed-in-new-report/)
- [The Conversation / UQ — Australians wait 12 months for aged care](https://theconversation.com/australians-wait-12-months-for-aged-care-and-the-latest-budget-funding-is-unlikely-to-change-that-282960)

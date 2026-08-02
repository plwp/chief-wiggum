# Marketing-capability tiers (M0 → M1 → M2)

Revenue-triggered marketing-capability graduation, mirroring the stack cost-tier idiom
(`patterns/stacks/*/manifest.json` `cost_tiers`/`graduation_triggers`) — chief-wiggum#241
leg 2, decision 7. Tiers + triggers are recorded alongside the bet's pricing artifact;
**graduation is a journaled human decision** (a `bet.py` journal event with a reason),
never an automatic promotion. Graduation triggers are formulas, not vibes.

## The buy-not-build stance (decision 5)

CW **integrates with and exports to** existing sales/marketing service platforms — it never
rebuilds them: paid-acquisition platforms (Meta Ads, Google Ads), AI platforms (OpenAI-class
asset generation; AI distribution surfaces like the GPT store count under the
`existing-platforms` channel), ESP/email sequences, CRM, social scheduling, SEO tooling,
directory/listing services. Platform subscriptions and ad spend are **ordinary cost-inputs
line items** (`flat_monthly` and metered entries in `templates/cost-inputs-schema.json`) and
count against the bet envelope like any other spend.

**Paid channels carry the paid-spend unlock gate** (docs/business-factory.md §4, invariant 4):
no paid-acquisition spend before `pmf_score ≥ 40% (n ≥ 40)` or equivalent commitment-class
evidence — and channel viability requires LTV above the channel's CAC floor. At micro price
points paid ads are frequently underwater; the channel-experiment **verdict must say so**
rather than let spend drift.

## Tiers

| Tier | Capability | Monthly nut |
| --- | --- | --- |
| **M0** | Founder + platforms: the operator runs the focused channel personally, on bought platform subscriptions, with CW-stamped assets (launch checklists, copy scaffolds, Mom-Test scripts, prospect briefs, follow-up sequences). | Low flat platform nut only. |
| **M1** | M0 + a freelance specialist executing **the focused channel** (never a generalist across unfocused channels). | Specialist's monthly cost + platforms. |
| **M2** | Agency or part-time marketing hire; the focused channel is scaling past one specialist's execution capacity. | Fully-loaded agency/hire cost. |

## Graduation formulas (decision 7 — formulas, not vibes)

**M0 → M1** when BOTH hold:

```
focused_channel.measured_cac < bet.target_cac_usd        # channel PROVEN, not hoped
portfolio_mrr >= 3 × specialist_monthly_cost_usd         # the specialist pays for themselves with margin
```

**M1 → M2** when BOTH hold:

```
focused_channel_monthly_spend > one_specialist_execution_capacity   # scale constraint is real
gross_margin_after_fully_loaded_cost > 0                            # margin supports the fully-loaded cost
```

Demotion is symmetric: when a formula's premise stops holding (channel saturates, MRR drops
below 3× the specialist cost), the tier steps back down — journaled the same way.

## Per-bet record

Record the active tier and the trigger inputs alongside the bet's pricing artifact:

```json
{
  "marketing_tier": "M0",
  "specialist_monthly_cost_usd": null,
  "graduated": []
}
```

Each `graduated` entry: `{"to": "M1", "date": "...", "trigger_values": {...}, "journal_record": "rec-00042"}` —
the formula inputs at decision time, so the graduation is auditable against the formulas above.

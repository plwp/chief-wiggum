# Feature-Value Discovery — parked ideas

> **Status: parked, deliberately unbuilt.** These are directions for growing the
> factory's *generative* value axis. None is built yet, on purpose — the shape of
> each (which signals, what ranking, what thresholds) should be **molded by real
> usage, not guessed**. Build a direction when its trigger signal (below) appears.

## Why this exists

Everything the factory measures today optimizes **"are we building it *right*"** —
gates, the [cost/value verdict](factory-telemetry.md), reflection-of-slippage. All
of it is **defensive value: bugs caught, defects prevented.**

The complementary axis — **"are we building the *right thing*"**, i.e. features that
create value for users — is under-served. A validation that catches zero bugs looks
worthless in the verdict; a feature that ships and delights users generates value
the telemetry can't even see. The verdict answers *"is this validation worth its
cost"* but never *"is this feature worth building."*

| Axis | Question | Status |
|--|--|--|
| **Defensive** (build it right) | correctness, defect prevention — `caught` findings | built (this is the whole telemetry/gate/verdict layer) |
| **Generative** (build the right thing) | feature value delivered to users | **the gap this doc parks** |

## Parked directions

### 1. Feature-opportunity discovery
The generative counterpart to defect-mining. A lens that reads *product* signal —
engagement gaps, feature requests, conversion drop-offs, competitor gaps — and
surfaces **ranked feature opportunities** (what to build next, grounded in evidence).
Where [`/reflect`](../.claude/commands/reflect.md) mines slippage and defects, this
mines *opportunity*.
- **Trigger:** a built product with real user signal (feedback, engagement data)
  accumulating — there's nothing to mine until then.
- **Leans on:** [`engagement-instrumentation`](../patterns/engagement-instrumentation/pattern.md)
  signal + a feedback source.

### 2. Feature-adoption telemetry
The generative counterpart to the gate cost/value verdict. Instrument which *shipped
features* actually get used / convert, so a feature's build+run cost (which
`factory_log` + `config/model_pricing.json` can already compute) is weighed against
the value it delivers — a real **product ROI**, not "did tests pass."
- **Trigger:** shipped features with real traffic.
- **Note:** parallels the gate verdict exactly, on the generative axis — *cost*
  (build + run $) vs *value* (adoption / conversion). `engagement-instrumentation`
  already captures per-item completion; extend it to per-feature.

### 3. Business-value / pricing — [chief-wiggum#122](https://github.com/plwp/chief-wiggum/issues/122)
The `/business-consultant` skill: cost-of-features scaling, pricing models, unit
economics — quantify what a feature is worth building and charging for. Already
ticketed; grounded in `dogeared-coach/docs/pricing.md`.
- **Trigger:** a product approaching a monetization/packaging decision.

### 4. Reframe the improvement loop
Make the [`improvement-loop`](../patterns/improvement-loop/pattern.md) `Finding`
model carry **opportunities** (not just defects), so the loop optimizes *delivered
value* (conversion / engagement) as a first-class goal alongside correctness. The
pattern already declares `success_metrics` with goal directions (`mrr↑`,
`activation↑`); this would wire the loop to actually act on them, not just on
correctness.
- **Trigger:** an improvement loop running on a real product with conversion /
  engagement metrics to optimize.

## The discipline (why parked, not built)

Each of these needs real usage to mold its shape — the exact signals, the ranking,
the thresholds are guesses until there's data. Building them speculatively risks the
same trap the [gate-rollout discipline](gate-rollout.md) warns about: a
feature-discovery lens that's noisy or mis-ranked on real products trains the
operator to ignore it.

The **defensive** cost/value telemetry is built and will start producing data from
the first real factory runs (dogeared-coach). **That data may itself reveal which
generative direction matters first** — e.g. if the verdict shows a validation loop
costing a lot to catch defects in a feature nobody uses, that's the signal to build
#2. Revisit this doc when a trigger fires; don't pre-empt it.

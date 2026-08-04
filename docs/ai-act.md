# EU AI Act: classifying what CW builds (chief-wiggum#316)

This is about **products CW builds** — a distinct subject from CW's own posture
under the Act (`docs/ai-act-posture.md`, chief-wiggum#317). Do not conflate the two:
a built product's `docs/compliance/ai-act.json` describes that product only.

## The wrong trigger, and the right one

Before this ticket, CW's only AI-regulatory hook was one line in
`templates/compliance-requirements.md` §10, reached only when `/seed`/`/saas-gate`
decided the product "holds regulated or sensitive data" (health, financial,
biometric, children's, government-classified, or PII at scale). **The EU AI Act
does not trigger on data sensitivity** — it triggers on AI functionality and where
the output lands. A product with a conversational agent and zero regulated data got
zero AI Act treatment under the old trigger, while Art. 50(1) (tell the user they're
talking to an AI) has had no grace period since 2 August 2026.

`docs/compliance/ai-act.json` is therefore a **standalone artifact**, decoupled from
the data-sensitivity trigger: `/seed` records `eu_scope` independently of whether the
product holds regulated data, and `/architect` produces the classification for every
AI feature regardless.

## Scope: the in-force layer only

Built now:

- **Art. 5 prohibited practices** (in force since 2 Feb 2025) — manipulative
  techniques, workplace/education emotion inference, social scoring, biometric
  categorisation by protected attribute, and (per the Digital Omnibus, transitional
  period to 2 Dec 2026) NCII/CSAM-generating systems assessed against foreseeable
  misuse.
- **Art. 4 AI literacy** (in force since 2 Feb 2025) — a one-page posture, no
  machinery; recorded via the deployer relationship to third-party models.
- **Art. 50 transparency** (in force since 2 Aug 2026) — the buildable core of this
  capability: tell the human they're talking to an AI, mark synthetic output
  machine-readably, disclose deepfakes/AI-generated public-interest text.

**Deliberately parked**: the Chapter III high-risk conformity pack (Arts. 8-17, 43,
47-49, 72-73). The Digital Omnibus deferred standalone Annex III systems to
2 Dec 2027 and Annex I embedded systems to 2 Aug 2028, and the harmonised standards
those obligations resolve against do not exist yet — building against unfinished
standards is exactly the speculation `docs/gate-validation.md` forbids.

## The artifact

`docs/compliance/ai-act.json` in the target repo (schema: `templates/ai-act-schema.json`).
One record **per AI feature**, not per product:

- `eu_scope`: `in_scope` | `out_of_scope` | `TBD`, with the Art. 2 limb relied on.
- Per feature: `feature_id`, `role` (`provider`|`deployer`|`both`), `tier`
  (`prohibited`|`high_risk_annex_i`|`high_risk_annex_iii`|`transparency_art50`|`minimal`),
  `performs_profiling` (always an explicit boolean — profiling is ALWAYS high-risk
  and voids any Art. 6(3) derogation), `annex_iii_area` (1-8, required iff
  `tier=high_risk_annex_iii`), `derogation_assessment` (names one of the four Art.
  6(3) conditions when a `high_risk_annex_iii` feature is claimed not high-risk),
  `obligations[]`, `evidence[]` (`@cw-trace` handles), `legal_signoff[]` (`TBD:`
  markers).

## Art. 6(4): absence is a legal fact, not a null

A provider claiming an Annex III system is NOT high-risk must document that
assessment **before** the system is placed on the market — if the document never
existed, the claim cannot be made later, no matter how the classification would have
come out. `check_ai_act.py` mechanizes the distinction this creates:

- **`classification_status: "missing"`** — no `ai-act.json` at all. `outcome: findings`
  — never a silent pass, because the assessment was never made.
- **`classification_status: "recorded"`, empty `features: []`** — a genuine, explicit
  "this product has no AI functionality". `outcome: inapplicable`.
- **`classification_status: "recorded"`, features present, clean** — `outcome: pass`.
- **Any `fail`-severity finding** (a `prohibited` tier, an undocumented Annex III
  derogation, an undeclared `eu_scope`, a `transparency_art50` feature with no Art.
  50 obligation cited) — `outcome: findings`.
- **Unparseable artifact** — `outcome: error`.

These four states are the standard chief-wiggum gate vocabulary
(`pass`/`findings`/`inapplicable`/`error`, see `check_traceability.py`); the
`classification_status` field carries the Art. 6(4) distinction on top of it,
because "inapplicable" and "missing" must never be interchangeable here.

## Authority boundary

`check_ai_act.py` checks that a disclosure obligation is **declared** and a
derogation assessment **exists and names its condition**. It does NOT check that:

- a disclosure is adequate to "a natural person who is reasonably well-informed"
  (Art. 50's own standard),
- a derogation's reasoning is legally sound,
- the classification is *correct* at all.

It does not touch conformity assessment, CE marking, EU-database registration, or
post-market monitoring. Everything past that line is a `legal_signoff` `TBD:` in the
artifact itself, for a lawyer to confirm.

**Known limitation, v1**: this ships checking the artifact's own declared fields for
internal consistency and Art. 5/6/50 completeness. It does not yet cross-check a
`transparency_art50` feature's `evidence[]` against `code_query.py`-scanned
model-call sites, or a UI surface's first-interaction reachability in `ui-spec.json`
— that deeper scan is follow-up work once this is dry-run against a real target
(per `docs/gate-rollout.md`'s bring-up ramp).

## Wiring

- `/seed` Step 2.5: records `eu_scope` (independent of the regulated-data trigger).
- `/architect` Step 4j: produces/updates the per-feature classification; runs
  `check_ai_act.py` report-only before Step 6.
- `/close-epic` Step 2j2: runs `check_ai_act.py` report-only; surfaces the outcome
  and any `fail`-severity findings in the close report.
- `patterns/registry.json`: every pattern entry carries an `ai_act` block
  (`risk_surface`, `obligations_stamped`, `escalates_tier_when`) so a pattern that can
  move a product across a risk boundary (e.g. `engagement-instrumentation` applied to
  worker monitoring) says so at adoption time. The `ai-transparency-disclosure`
  pattern stamps the Art. 50 contract pack (disclosure at first interaction,
  provenance marker on synthetic output, deployer disclosure config) as an
  invariant cluster `/apply-pattern` can install.

## Gate status

Report-only everywhere (see `docs/gate-rollout.md`'s ledger). Promotion to `--gate`
requires a dry-run against a real shipped target and a passing
`docs/quality/validation/check_ai_act.json` record per `docs/gate-validation.md`,
including the mandatory evasion classes (omission, config-indirection,
sampling-gap).

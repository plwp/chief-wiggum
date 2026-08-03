# Design Taste — a living current-craft brief

**as_of: 2026-08-03**

External source of taste for every divergent-design/outward-asset flow in the factory
(chief-wiggum#250, companion to #249). A model's own priors date badly and converge on
AI-default aesthetics (purple-gradient dark heroes, glassmorphism, Inter-everything) —
this brief is the grounding input that keeps divergent variants (`/design`, the
`landing-page-smoke-test`/`presale` patterns' INV-LPS-006/INV-PRE-006, Track H stamped
assets) tied to what currently reads as high-craft rather than to the model's default.

**Staleness rule**: any `/design` run or outward-asset variant generation MUST ground
against a brief `as_of` no more than 90 days stale. A stale brief triggers a refresh pass
(`/update` Step 3.7, below) BEFORE generation proceeds — the same staleness-honesty idiom
as `models.md` and `/update`. This document does not gate anything mechanically (no lint
reads `as_of` today); the staleness check is a step in the workflows that consume it.

**Method note, stated honestly**: this first generation is a compiled brief, not a
verified live-fetch snapshot — treat every "currently reads as high-craft" claim below as
the compiler's assessment, re-groundable and correctable at the next refresh. It draws on
the pinned source roster (§4) by name and genre convention rather than a fresh crawl of
each; the refresh mechanism (§5) is what makes it reproducible and improvable rather than
a one-off opinion.

---

## 1. Design moves that currently read high-craft

Each move is a **named, adoptable pattern** — cite the moment it started reading fresh,
not just "clean design":

- **Authored, specific copy over marketing abstraction.** The current tell of craft is a
  sentence that could only have been written by someone who watched a real user do the
  task — a number, a named artifact, a quoted problem — rather than a virtue noun
  ("seamless", "effortless", "reconciliation-first"). This is the same discipline
  chief-wiggum#255 encodes as a lint; the design brief and the copy brief are one
  discipline viewed from two angles.
- **Editorial, asymmetric layout over centered-card monotony.** Real typographic
  hierarchy (a genuine display size jump, not three weights of the same size), generous
  whitespace used to create a reading path, and grids that break intentionally at one or
  two points rather than everywhere.
- **Warm, slightly imperfect color over saturated gradient defaults.** Muted, named
  colors (not "primary blue") with one deliberate accent; grain/texture or a single
  photographic/illustrative asset used sparingly beats a synthetic gradient mesh.
  Monochrome-plus-one-accent reads as considered; five-stop rainbow gradients read as
  generated.
- **A single strong typographic voice, not a font pairing showcase.** One distinctive
  display face (often a serif or a slab, not a geometric grotesk) carrying the whole
  personality, paired with a workhorse text face used quietly. Two safe sans-serifs
  paired "for contrast" is the tell of a template, not a choice.
- **Real interface density for tools, real air for consumer/marketing.** A dev-tool or
  trust-tool landing page can show an actual dense data table or terminal output as
  proof-of-substance; a consumer page earns the opposite — deliberate emptiness as a
  confidence signal. Matching density to genre is itself a craft signal (see §4 direction
  briefs).
- **Motion that clarifies state, not motion that decorates.** A transition that shows
  *where something went* (shared-element, direction-preserving) reads current; parallax
  and pointer-tied camera motion read as 2018 and can be genuinely uncomfortable for some
  viewers — never ship pointer-tied camera motion (see `feedback_visual_design_iteration`
  in operator memory: it has made the operator ill before).
- **Honest, unglamorous screenshots over polished device mockups.** Real product
  screenshots (even slightly rough) in a plain frame now read as more trustworthy than a
  glossy isometric device render — the render is what a template generates by default.

## 2. Instantly-dating anti-patterns (the AI-default list)

Flag these on sight — they are the fastest way a divergent variant collapses back to
"looks AI-generated":

- Purple-to-blue (or purple-to-pink) gradient dark hero, usually with a blurred glow blob
- Glassmorphism panels (frosted-glass blur + thin white border) as a default surface
  treatment rather than a deliberate, sparing choice
- Inter (or a visually-identical geometric grotesk) as both display AND body face, at
  default weights, with no distinctive display type doing any work
- Three feature cards in a row, each with a rounded pastel icon-in-a-circle and a
  two-sentence description — the single most template-recognizable block in existence
  today
- Perfectly symmetric centered hero: headline / subhead / two buttons / device mockup,
  with no asymmetry anywhere on the page
- Abstract 3D blob/gradient-sphere illustrations standing in for a real product image
- Em-dash-heavy, antithesis-heavy marketing copy ("it's not X — it's Y") riding the same
  visual template (chief-wiggum#255's copy lint targets the verbal half of this same tell)
- Emoji used as section iconography instead of a real icon set

## 3. Typography / palette / layout currency notes

- **Typography**: display serifs and slabs with real personality (not "safe" geometric
  sans) are currently doing the differentiation work that font-pairing used to do;
  variable fonts used for genuine weight/optical-size range (not just two static cuts) are
  a craft signal. Oversized, tightly-tracked display type against small, generous body
  text is the currently-legible hierarchy contrast.
- **Palette**: desaturated, named palettes (a specific ink, a specific paper, one accent)
  read as considered; default Tailwind-shade palettes used unmodified read as a template.
  Dark mode as an equal first-class palette (not an afterthought inversion) is table
  stakes now, not a differentiator.
- **Layout**: asymmetric grids, intentional single-column breakouts for emphasis, and
  real content-driven layout (the copy determines the grid, not a fixed 12-column
  template filled with placeholder text) read current. Bento-grid feature layouts are
  transitioning from fresh to common — usable, but no longer a distinguishing move on
  their own.

## 4. Direction briefs per genre (2–3 executable directions each)

These are starting briefs for `/design` and the validation-experiment patterns' INV-*-006
divergent-variant step — each is a distinct, nameable direction, not a description of "a
nice design":

### Trust-tool (compliance, finance, regulated calculation, records-of-truth)

1. **Ledger-editorial** — serif display type, cream/ink palette, real tabular data shown
   honestly (a genuine reconciliation table, not a mockup), minimal chrome. Reads as
   "written by an accountant who also has taste."
2. **Clinical-precise** — monospace or slab numerals for every figure, high-contrast
   black-on-white, generous whitespace, zero decoration; every visual element earns its
   place by displaying a real number.
3. **Institutional-warm** — a single warm accent (not blue) against near-black text,
   editorial photography of real workspaces/paperwork rather than abstract icons, long-form
   explanatory copy treated as a feature, not filler.

### Dev-tool (CLIs, APIs, infra, developer-facing SaaS)

1. **Terminal-honest** — a real terminal/log output as the hero visual, monospace
   throughout, dark-mode-first with a single bright accent for state changes, zero
   marketing illustration.
2. **Spec-as-interface** — the landing page IS a rendered version of the product's own
   config/schema format, syntax-highlighted, with the pitch woven into inline comments —
   the interface is the pitch.
3. **Brutalist-functional** — deliberately unstyled system fonts, visible borders and
   grid lines, information density over polish — signals "we spent the effort on the
   product, not the marketing site," which is itself a trust signal for this audience.

### Consumer (SMB tools, lifestyle, low-cap micro-SaaS per §9.6)

1. **Quiet-confidence** — a lot of empty space, one photograph of a real person doing the
   real task, short plain-language copy with a specific number in the first sentence, no
   more than one accent color.
2. **Trade-specific-vernacular** — visual language borrowed from the buyer's own trade
   (invoice-book textures for a bookkeeping tool, work-order clipboards for a trades tool)
   rather than generic SaaS iconography — signals "built for you specifically," which
   matters more than polish at this price point.
3. **Friendly-editorial** — warm serif headlines, hand-drawn-feeling (not generated)
   accent marks, a genuine customer quote given real typographic weight rather than
   buried in a testimonial carousel.

---

## 5. Refresh mechanism

`/update` Step 3.7 (`.claude/commands/update.md`) re-runs the roster below through
research agents and regenerates this brief with fresh citations, on the same cadence as
the model/pricing refresh it already performs. This is a **research pass over a fixed,
editable roster**, never a scraper service or a training/fine-tuning pipeline (explicit
non-goal, §7). Running the refresh:

1. Read the current roster (§4 below — actually §6, see note) and this brief's `as_of`.
2. For each source, note what currently reads as fresh vs what has become common —
   research agents fetch/browse, they don't guess.
3. Regenerate §1–§4 above with the new observations; bump `as_of`; commit.

## 6. Pinned source roster

Editable, not vibes — a refresh reads exactly this list:

- **Curated galleries**: godly.website, landing.love, Awwwards Site of the Day,
  minimal.gallery, siteinspire, dark.design
- **Type foundry showcases**: Klim Type Foundry, Grilli Type, Pangram Pangram, Colophon
  Foundry
- **Design writing**: Dense Discovery (AU) plus 2–3 current design/product newsletters
  (rotate in whatever is actively publishing at refresh time — the roster names the
  *category*, the refresh finds the current best instances)
- **"Small products with taste" list**: indie SaaS products with named, discussed craft
  (sourced from Indie Hackers "design" threads, Hacker News "Show HN" design commentary,
  and the design-focused corners of Product Hunt) — this list is deliberately curated by
  each refresh pass rather than hard-pinned, since indie-SaaS craft exemplars turn over
  faster than galleries or foundries

## 7. Explicit non-goals (anti-theater both ways)

- **No aesthetic gate script.** Taste is not lintable — INV-LPS-006/INV-PRE-006 and this
  brief's staleness rule are human checkpoints. There is no `check_design_taste.py` and
  none is planned.
- **No automated scraping pipeline.** A cadenced research pass (`/update` Step 3.7) is
  the entire mechanism — no crawler, no scheduled job, no stored corpus of scraped pages.
- **No training or fine-tuning on gallery content.** This brief is read as grounding
  context per generation, the same way any other reference document is; it is never used
  as training data.

## 8. Operator taste profile (wire-in, schema only — lives in the portfolio repo)

Every chosen-not-converged pick from the #249 variant flow or `/design` is recorded in the
**private** portfolio repo, never in this public repo: `~/.chief-wiggum/portfolio/taste/
choices.jsonl`, one append-only JSON object per pick — see
`templates/taste-choice-schema.json` for the record shape. Future variant generation reads
the profile to **centre** the spread on demonstrated preference, and must never collapse
the spread — the divergence count stays fixed regardless of how narrow the operator's
demonstrated taste turns out to be; narrowing the centre while keeping the count is the
whole point (a converged single default is exactly the failure #249 exists to prevent).

## 9. Wire-in points

- **`/design` Step 1** reads this brief (and its `as_of`) before generating divergent
  directions; a stale brief triggers a refresh first.
- **`landing-page-smoke-test` / `presale`** (INV-LPS-006 / INV-PRE-006, chief-wiggum#249)
  cite this brief as the mandatory grounding input for their divergent-variant step.
- **Track H stamped-asset flow** (docs/business-factory.md, channel-engine subsection)
  references this brief for launch-checklist copy/positioning candidates.

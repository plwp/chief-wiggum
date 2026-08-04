# CW's own posture under the EU AI Act

> This records a determination to be reviewed, not legal advice. It is chief-wiggum's
> position on itself as an AI system — the factory-output question (whether *products*
> CW builds comply with the Act) is a separate subject, tracked by chief-wiggum#316 and
> `docs/compliance/ai-act.json` in each target repo, and is explicitly OUT of scope here.

`last_reviewed: 2026-08-04`

## What CW is, under the Act

- **CW is an AI system** on the Art. 3(1) definition — machine-based, infers from its
  input how to generate outputs (code, plans, PR and issue text) that influence its
  environment, and operates with autonomy. This is not worth arguing against; the
  interesting question is what follows.
- **Role: deployer**, operating for its own use, from outside the Union (AU-based).
  It is not a provider of a placed-on-the-market product in its own right — the
  factory runs against target repos on behalf of whoever invokes it.
- **Not Annex III** — software-development tooling is not a listed high-risk area.
- **Not an Art. 5 practice.**
- **Art. 2(12) FOSS exemption claimed**: CW is MIT-licensed, a public GitHub repo
  (`visibility: PUBLIC`), v0.1.0, not published to PyPI or npm. The Regulation "does not
  apply to AI systems released under free and open-source licences, unless they are
  placed on the market or put into service as high-risk AI systems or as an AI system
  that falls under Article 5 or 50" (Art. 2(12)). CW is neither high-risk nor an Art. 5
  practice, so the exemption's availability turns entirely on **Article 50**.

## The Article 50 analysis, limb by limb

Three limbs; only one is genuinely arguable.

1. **50(1) — disclose you're an AI.** CW interacts directly with a human, but that it
   is an AI system is obvious to a reasonably well-informed person operating a CLI
   invoked by slash commands. **Exempt on its face.**
2. **50(2) — machine-readable marking of synthetic text.** Binds providers of AI
   systems generating synthetic text. **CW generates text and publishes it**: commit
   messages, PR bodies, issue bodies, and documentation, into a public GitHub repo. The
   available exemptions are for assistive editing and for systems that do not
   substantially alter input data — neither obviously fits a system that authors the
   artifact from scratch. **This is the live question, and it is deliberately mooted
   rather than answered — see Decision below.**
3. **50(4) — AI-generated text published to inform the public on matters of public
   interest.** Carve-out where a human exercises editorial review and assumes
   responsibility, which merging a reviewed PR plausibly satisfies. Recordable rather
   than arguable, and not load-bearing while (2) is mooted.

## Decision: moot 50(2), don't resolve it

The conservative posture is cheap enough that resolving the legal question stops being
on the critical path: **disclose AI authorship on CW-generated public artifacts** — a
trailer on generated commits (`chief_wiggum.ai_disclosure.COMMIT_TRAILER`), and a line
in generated PR and issue bodies (`chief_wiggum.ai_disclosure.DISCLOSURE_LINE`, wired
through `chief_wiggum.shipping.build_pr_body` and `tracker.py create --disclose-ai`).
Cost is one line per artifact; benefit is that the most arguable limb of the only
article that threatens the Art. 2(12) exemption no longer has to be won.

`TBD: whether Art. 50(2) actually applies to CW's generated text is still open for
legal sign-off. Mooting it in practice (universal disclosure) is not the same as
answering it — this line must stay TBD until a lawyer confirms or CW stops relying on
the mooting posture.`

## Two unresolved edges

Named rather than papered over. Neither blocks this posture doc.

1. `TBD: Art. 2(1)(c) extends the Regulation to third-country deployers "where the
   output produced by the AI system is used in the Union." CW's output is source code
   that may end up running in the Union — an arguable reach. Argument for reach: the
   provision is drafted broadly and doesn't carve out incidental/indirect output use.
   Argument against: CW itself does not target, market to, or know its output is bound
   for the Union — the deployment-location trigger reads as aimed at systems that
   knowingly serve EU-located users/outputs, not tooling several removes upstream of
   wherever a target repo eventually runs. Needs legal sign-off.`
2. `TBD: "placing on the market" turns on supply "in the course of a commercial
   activity" — a public MIT repo maintained by an operating company is not
   self-evidently outside that. Argument for in-scope: the maintaining entity is a
   commercial operator, not a hobbyist. Argument against: MIT-licensed, no charge, no
   support SLA, no commercial packaging of CW itself — the commercial activity (if any)
   is in what CW is used to build, not in supplying CW. Needs legal sign-off.`

## Re-assessment triggers

Re-run this determination if CW is:

- (a) offered as a hosted or commercial product,
- (b) relicensed off MIT,
- (c) extended into an Annex III domain, or
- (d) operated by an entity established in the Union.

Any one of these can void the Art. 2(12) exemption independently of the others.

## Art. 4 — AI literacy (applies via deployed third-party models)

The FOSS exemption does not cover this: CW is also a **deployer of third-party AI
systems** (Claude, Codex, Gemini — see `config/providers.json`). Those models are not
open-source-exempt, so **Art. 4 AI literacy attaches through them regardless of CW's
own Art. 2(12) status.** No machinery required — this is a one-page posture record, not
a gate. `TBD: Art. 4 is reported to have been softened by the Digital Omnibus from
"ensuring" to "supporting the development of" literacy — verify against the final
consolidated text; the wording matters for how much CW needs to document here.`

## Upstream GPAI documentation (Art. 53)

CW consumes general-purpose models whose providers owe downstream-provider information
under Art. 53. Keep those references alongside `models.md` / `config/providers.json` —
they become load-bearing the moment a model CW integrates is placed on the EU market as
part of something else, where **Art. 25 would make the integrator the provider of that
system.**

## Staleness, mechanized

`scripts/check_cw_standards.py` asserts this document exists and carries a
`last_reviewed` date within the last 12 months (report-only by default, `--gate` to
block — see `docs/gate-rollout.md`). It asserts the determination has been
**revisited**, never that it is **correct**.

## Sources

- [Art. 2 scope (incl. 2(12) FOSS exemption, 2(1)(c) output-used-in-Union)](https://artificialintelligenceact.eu/article/2/)
- [Art. 4 AI literacy](https://artificialintelligenceact.eu/article/4/)
- [Art. 50 transparency](https://artificialintelligenceact.eu/article/50/)
- [Annex III](https://artificialintelligenceact.eu/annex/3/)
- [Digital Omnibus agreement (Gibson Dunn)](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/) — Art. 4 reported softened from *ensuring* to *supporting the development of* literacy; verify against final consolidated text

Legal position confirmed against the sources above on 2026-08-04. This document
records a determination to be reviewed, not legal advice. See chief-wiggum#317.

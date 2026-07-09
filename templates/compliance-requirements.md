<!--
Chief Wiggum — Regulated-Data Compliance Requirements template.

USE THIS when a product holds regulated or sensitive data at rest — health, financial,
biometric, children's, criminal-record, or government-classified data, or PII at scale.
Fill it during /seed (or the first /architect pass) and commit it to the target repo as
`docs/compliance-requirements.md`. It is the design-time complement to /saas-gate's runtime
checks: /saas-gate proves the controls RUN; this document defines what they must BE.

It is jurisdiction-parameterized — the worked hints below are illustrative (AU shown as an
example because that is where it was first authored). Replace them with the obligations of the
product's actual jurisdiction(s) and buyer.

Two load-bearing patterns this template exists to enforce:
  1. THE LLM/AI DATA-PATH GATE. If regulated data is ever sent to a third-party model (extraction,
     classification, RAG, generation), the residency / no-training / no-retention posture of that
     model provider is a GO/NO-GO GATE resolved BEFORE the pipeline is built — not a downstream
     spike. Cross-border disclosure law (e.g. AU APP 8, EU GDPR Ch. V) usually keeps YOU
     accountable for the provider's mishandling. Model availability by region is frequently the
     deciding constraint and often overturns the "obvious" cloud choice.
  2. LEGAL-SIGNOFF TBDs GATE WORK. Any obligation that can't be confirmed against a primary legal
     source is written as a `TBD:` and gates the dependent ticket (same mechanism as
     scripts/check_unresolved.py). Do not hard-code a retention period, a lawful basis, or a
     classification you inferred — mark it TBD and get a lawyer to sign it off.
-->

# {PRODUCT} — Compliance & Security Requirements

> **The licence to operate.** {PRODUCT} holds {DATA_CATEGORY} at rest for {BUYER}. Meeting
> {GOVERNING_STANDARD} is not a phase-2 nicety — a vendor that fails the assessment does not get
> in the door. These controls are built from the first commit and **gate every data-touching
> ticket**. Every material claim below is cited; unconfirmed items are `TBD:` and gate dependent
> work until a lawyer signs them off.

## 1. Data classification
- Classify the data under the buyer's scheme ({e.g. AU PSPF OFFICIAL:Sensitive; US data
  categories; EU special-category data}). The classification **drives every control below** —
  handling, logging, access, encryption, residency.
- Identify the **governing standard for the specific buyer** — it is often narrower/state-level,
  not the headline national framework ({e.g. a state regulator → state data-security standard, not
  the Commonwealth one}). `TBD:` confirm the exact classification + security schedule in the
  actual contract/RFT.

## 2. Privacy / data-protection law
- **What law applies, to whom, and where.** Map the obligation set for each jurisdiction the data
  touches ({national + state/sector regimes}). Note any exemptions AND whether they actually apply
  to **this** entity — a common trap is assuming a customer's exemption (e.g. an employer's
  employee-records exemption) flows to a third-party **processor**. It usually does not.
- **The obligations that bite hardest** (fill per regime): lawful basis / consent for collection;
  collection notice; use/disclosure limits (incl. "no secondary use to train a model without
  explicit opt-in"); cross-border disclosure (§4); security proportional to sensitivity (§8);
  access + correction rights (§9).

## 3. Sector / records law (if any)
- Sector-specific regimes ({health-records act, financial-records rules, education/children's
  codes}) often apply **concurrently** with general privacy law and can impose stricter retention,
  access-response SLAs, and a different regulator. Capture them here. `TBD:` any characterisation
  that changes the obligation (e.g. "are we legally a health-service provider?").

## 4. Cross-border & the AI/LLM data-path — GATE
- **State the cross-border rule** ({e.g. AU APP 8: reasonable steps + ongoing accountability;
  GDPR Ch. V: SCCs/adequacy}). Assume a foreign-HQ'd provider triggers it even when compute is
  in-region, unless proven otherwise.
- **Resolve the AI/LLM data-path GATE explicitly:**
  - [ ] Which provider + region actually processes and stores the data? (Model availability by
        region is frequently the binding constraint — verify, don't assume the parent cloud has it.)
  - [ ] Contractual **no-training** on your data (+ explicit opt-in only if ever otherwise).
  - [ ] **Zero / short retention**, and where any retained data lives.
  - [ ] Signed DPA; subprocessor register; breach-notification + audit rights.
  - [ ] Pin an **in-region model identifier** where cross-region routing is possible.
  - [ ] A written "reasonable-steps" / transfer-impact file, kept even when compute is in-region.
  - **Verdict:** {compliant route = …} / {fallback = …} / {no compliant route → the AI feature
    changes}.

## 5. Data-breach obligations
- The applicable breach-notification scheme(s), the assessment window, who to notify, and the
  trigger threshold. Sensitive data usually pushes the "serious harm" test toward notification —
  treat those breaches as high-severity by default. Ship a runbook (§ checklist).

## 6. Retention schedule & legal hold
- A per-record-type **retention schedule** with the statutory basis for each; default
  **conservatively** where a period is `TBD:` pending legal sign-off.
- **Legal hold** — a preservation duty arises once litigation/investigation is reasonably
  anticipated, and destroying anticipated evidence can be a **criminal offence**. A `legal_hold`
  flag must **hard-block** retention-expiry deletion, be immutably logged, and be releasable only
  by an authorised role.

| Record type | Retention | Basis |
|---|---|---|
| … | … | … (mark TBD where unconfirmed) |

## 7. De-identification & minimisation
- The legal bar for de-identification ({e.g. "no reasonable likelihood of re-identification"}) and
  that **pseudonymisation with a retained key is still personal data**. Beware small-n /
  spontaneous-recognition risk — **suppress small cells (n<{k})** and aggregate for analytics.
- Split features: which run on **de-identified/aggregated** data (benchmarking, trends) vs which
  genuinely **require identity** (per-subject case handling). Minimising held identified data is
  the single strongest control.

## 8. Security controls (target: {GOVERNING_STANDARD} + {baseline, e.g. Essential Eight ML2})
- Encryption at rest via **customer-managed keys** (rotation policy; crypto-shred at expiry) +
  strong TLS in transit.
- **Immutable/tamper-evident audit** (WORM/object-lock or hash-chain) of every access to and
  mutation of regulated data; retain to match the record schedule.
- Enforced **MFA/SSO**, least-privilege RBAC, **tenant isolation** (DB row-level security done
  right: forced RLS, non-owner app role, per-request tenant context, a zero-rows test),
  break-glass with mandatory review.
- Baseline hygiene to the named maturity level (patching, app control, admin hardening, backups),
  secrets in a manager (never env vars), malware scanning on upload, network segmentation.
- **Defer** the expensive formal certification/assessment (e.g. IRAP, HITRUST) until a deal
  requires it — but build to the bar from day 1 and leverage the cloud provider's own assessment.

## 9. Individual-rights mechanics
- Access + correction (+ any erasure) rights, their **response SLAs**, and whether correction is
  annotation vs deletion. For **data subjects who are not your users** (e.g. a customer's
  employees/patients), route requests through the controller with your product as processor.

## 10. AI-specific governance & upcoming reform
- Automated-decision-making transparency, high-risk-AI rules, and any pending reforms with a
  commencement date — track what is IN FORCE vs pending. Plus model governance: labelled benchmark
  corpus, accuracy-by-class, confidence calibration, prompt/model regression tests, model-change
  approvals, prompt-injection handling.

---

## What {PRODUCT} must build/do — consolidated checklist
- [ ] Data-classification policy + tagging that drives handling
- [ ] Consent / lawful-basis engine + collection notices (+ never-train-by-default)
- [ ] Access & correction workflow with the right SLAs (+ controller-mediated for non-users)
- [ ] Cross-border / AI-data-path controls (DPA, in-region model, no-training, reasonable-steps file)
- [ ] Security baseline to {GOVERNING_STANDARD}+{maturity} (CMK, TLS, MFA, RLS, immutable audit)
- [ ] Retention engine + `legal_hold` hard-block
- [ ] De-identification pipeline (small-cell suppression) with identified data walled off
- [ ] Breach-response runbook (assessment window → notification templates)
- [ ] AI transparency / model-governance obligations

## `TBD:` — legal sign-off required before hard-coding (these gate dependent tickets)
- {list every inferred classification, lawful basis, retention period, or characterisation here}

## Sources
- {primary legal + provider sources, with URLs}

# AI Transparency Disclosure

## Problem

A product with a conversational agent, an image generator, or an emotion-inference
feature owes disclosure obligations under EU AI Act Art. 50 — and those obligations
are **already in force** (50(1)/(3)/(4) since 2 Feb 2025, 50(2) since 2 Aug 2026,
with no grace period for 50(1)). The classification layer (`docs/compliance/ai-act.json`,
chief-wiggum#316) can *record* that a feature owes Art. 50(1) — but recording an
obligation is not the same as the product actually disclosing anything. Left to a
generic "build the feature" ticket, disclosure is exactly the kind of one-line, easy-
to-forget requirement that ships absent, gets added behind a settings toggle nobody
enables, or gets removed in a later refactor because nothing protects it.

## Solution

Stamp the Art. 50 obligations as an invariant cluster with real scaffold code, the
same way `build-test-floor` stamps a CI floor rather than just documenting "run tests
in CI":

1. **First-interaction disclosure** (INV-ATD-001) — every human-facing conversational
   surface mounts a disclosure element reachable AT OR BEFORE the first exchange
   (e.g. a persistent "You're chatting with an AI assistant" line in the chat header,
   not a footnote in an about page).
2. **Provenance marker** (INV-ATD-002) — every synthetic-output write path persists a
   machine-readable marker alongside the content (a `generated_by` field on the
   stored record, an embedded metadata tag on generated media) — never a separable
   side-table that can silently detach from the content it describes.
3. **Deployer disclosure config** (INV-ATD-003) — emotion-recognition, biometric-
   categorisation, deepfake-publishing, or AI-generated-public-interest-text features
   carry an explicit, on-by-default disclosure config. If the product claims the
   Art. 50(4) human-editorial-responsibility carve-out, that claim is recorded with
   its basis (who reviewed, what "assumes responsibility" means here) — never assumed
   because "someone probably reviews it".
4. **Protected disclosure path** (INV-ATD-004) — the disclosure component and the
   provenance-marker write path are protected paths (`docs/ratchet.md`): a worker
   fixing an unrelated bug cannot quietly delete the disclosure banner or stop writing
   the provenance field. Removing either requires a human-reviewed goalpost change.

## Applies when

- The product has a human-facing conversational surface (chat/voice assistant).
- The product generates synthetic output (text/image/audio/video) that is persisted
  or published.
- The product runs emotion recognition or biometric categorisation, publishes
  deepfake media, or publishes AI-generated text on matters of public interest.

Does not apply to a product with no AI functionality — record `eu_scope`/`tier:
minimal` in `docs/compliance/ai-act.json` instead of stamping this pattern.

## Relationship to `docs/compliance/ai-act.json`

The classification artifact says a feature **owes** Art. 50(1)/(2)/(3)/(4)
(`tier: transparency_art50`, `obligations: ["Art. 50(1)"]`). This pattern is what
makes the `evidence[]` entry real: the `@cw-trace` handle should point at the
disclosure component / provenance-marker write path this pattern installs, not at a
TODO comment. `scripts/check_ai_act.py`'s `art50_no_evidence` warning is exactly the
gap this pattern closes.

## What this does NOT cover

Whether a disclosure is adequate "to a natural person who is reasonably well-
informed" (Art. 50's own standard) is a design/copy judgment this pattern does not
make for you — it guarantees a disclosure element EXISTS and is reachable, not that
its wording clears that bar. See `docs/ai-act.md`'s authority-boundary section.

# UX Review — Behavioural Product Walk-through

Drives the built product like a real user — across every persona — and reports the **behavioural** UX: task friction, dead-ends, inconsistency, leaked internals, missing guardrail screens, and empty/error-state quality. Produces a severity-ranked findings report.

This is the product-level counterpart to `/close-epic` Step 6. That step walks the **cross-ticket journeys of one epic**, derived from `integration-tests.md`. This skill walks the **whole product across personas** and isn't epic-scoped — use it after an epic lands, before a release, or whenever "is this actually good to use?" needs a real answer. "Build + tests green" never answers that; only driving the running app does.

## Usage
```
/ux-review <owner/repo> [--base-url <url>] [--personas a,b,c] [--epic "<name>"]
```

## Parameters
- `owner/repo`: the product repo (resolved via `repo.py`).
- `--base-url`: a running instance to drive (e.g. a staging URL). If omitted, the skill stands up a seeded local stack (preferred — reproducible and safe).
- `--personas`: restrict to a subset (default: every persona the product has — anonymous, and each authenticated role).
- `--epic`: optionally focus the walk on the surfaces a named epic touched (still persona-driven, not test-derived).

## Autonomy

Run the whole walk and the report to completion **without pausing** — it is read-only against the product (it drives the UI, it doesn't change product data beyond what a walk necessarily creates, and it never ships code). The report is the deliverable; the human decides what to act on. The **only** hard stops are the auth guardrails below — never route around them, and never block the user for something a seeded local stack solves.

## Key principles

- **The loop must look at the UI — as a user, not a screenshot harness.** Actually sign in, click through a real task, and read what a real person would read. A value rendered but illegible (a raw id, an error code) is a defect even though every test passed.
- **Two systemic gaps hide behind many symptoms.** Most behavioural findings collapse into one of two root causes — probe for both explicitly:
  1. **Internals leaking wherever a value renders without a "resolve to a human label" step** — raw UIDs/ObjectIds in logs/ledgers/feeds, backend error *codes* shown as user copy, internal status enums in the UI. One fix (a resolver / a code→copy map at the boundary) kills a whole class.
  2. **Missing guardrail screens** — no catch-all 404 (an unmatched URL renders a blank page), no sign-out on a shared-device shell, no empty/error state on a surface that will hit one. These get deprioritised as "edge cases" but real users hit them routinely.
- **Severity is impact-on-user, not effort-to-fix.** A one-line fix can be the top finding.
- **"This flow is clean" is a real finding.** Don't invent problems; confirm strengths too — they're what a refactor must not break.
- **Never trust a self-report.** The orchestrator (or a driven browser) must reach each screen itself. A journey you couldn't walk is reported as not-walked, never as "probably fine".

## Auth guardrails (non-negotiable)

Getting authenticated access is where this skill meets the safety rules. Hold them:

- **Prefer a seeded local stack** (Firebase Auth emulator / test DB) where "sign-in" uses seeded users and no real secret — it sidesteps the whole question and is reproducible. Stand it up (don't punt): reuse the repo's own `e2e`/`global-setup` harness to seed one user per role.
- **Against a deployed env**, use only a **documented demo/test credential** (published in the repo, e.g. `docs/OPERATIONS.md`) — that's test-fixture data, not a personal/financial/production secret. Never type a user's real password to authenticate.
- **Never self-grant access** — do not set an admin custom claim (or any role/permission) on an account to reach a gated surface. That's an access-control change and is off-limits. If a persona (e.g. admin) is only reachable via a claim you'd have to grant, walk it on the **local stack** (where seeding an admin is legitimate test setup), and note on the deployed env that it wasn't reachable.
- **Browser tip:** a password-manager extension often hijacks field focus (errors like "Cannot access a chrome-extension:// URL"). Set fields via `form_input` (by element ref from `read_page`) instead of click-to-focus + type, then click the submit control by ref.

## Workflow

### Step 1: Resolve paths and the product's persona/journey map

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
UX_TMP="$CW_TMP/ux-review"; mkdir -p "$UX_TMP"
```

Derive the personas and the journeys that matter from the product itself — routes/roles in the frontend, the auth model, the domain. Every product has at least an **anonymous** persona and one authenticated role; most SaaS have several (e.g. an operator/admin, a primary paying user, an invited end-user). For each persona, list the handful of journeys that carry its core value plus the moments most likely to break:
- **Anonymous** — landing → sign-up → sign-in (success *and* failure paths). First-impression + conversion clarity.
- **Primary user** — onboarding (empty states!) → the core create/assign/produce action → daily surfaces (dashboard, billing/plan, settings, feedback).
- **Invited/end-user** — accept-invite → consume → progress. Often the least technical persona and on a shared device.
- **Operator/admin** — the internal surfaces; do they actually answer the operator's real questions?

### Step 2: Get a running instance

Prefer the local stack. Stand it up from the repo's own tooling; seed one user per role plus a deliberately **sparse** account (a brand-new tenant) so empty/first-run states get walked, not just populated ones. Or point at `--base-url` under the guardrails above.

### Step 3: Walk every journey, screenshot every step

Run inside a **verification-worker** (contract: `docs/worker-contracts.md#verification-worker`) — *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"` — driving the repo's Playwright/browser-use setup (or the Claude-in-Chrome tools for a deployed env). For each journey, from a clean session, follow every step and capture a screenshot at every **page nav, modal open/close, menu/dropdown, state transition, and empty/error state** to `"$UX_TMP/<persona>/<journey>/step-N.png"`. Record for each step: label, URL, screenshot path, and any console errors. Do not fake a step you couldn't reach — mark it not-walked with the reason (e.g. an external dependency with no local emulator; note how its failure path behaved, which is itself a data point).

For breadth and speed, split personas across parallel workers; the orchestrator still **looks at the screenshots itself** before writing findings.

### Step 4: Evaluate against the behavioural checklist

For every journey, judge — citing the screenshot for each finding:

1. **Leaked internals** *(root cause 1)* — any raw id (UID/ObjectId), backend error *code*, internal enum/status, DB field, or system actor name shown where a human label belongs.
2. **Guardrail screens** *(root cause 2)* — unmatched URL → is there a friendly 404 or a blank page? Shared/end-user shell → is there a sign-out? Every surface that can be empty or error → is that state designed?
3. **Task friction** — can the persona complete its core task without a dead-end, a surprise state, or a false-success (a success toast when the underlying call failed)?
4. **Navigation & consistency** — do menus/breadcrumbs behave the same across features? Any nav item that points nowhere? Does the brand/chrome persist across pages, or drop on some (sign-in, an end-user shell)?
5. **Empty states** — do they have the product's voice and a next action, or a bare "No data"? (A genuine strength when done well — call it out.)
6. **Labelling & terminology** — same concept, same word everywhere? Casing/label drift across screens?
7. **Information exposure & density** — internal/admin fields leaking to a user; a row packing so many controls it's unscannable (prefer an overflow menu).
8. **Responsive** — does the primary mobile journey hold up (especially if the product promises "on their phone")?
9. **Expectation-setting** — does the funnel set expectations it later charges on (e.g. pricing/trial knowable *before* sign-up, not only in-app)?

Synthesise via a **synthesis-worker** (contract: `docs/worker-contracts.md#synthesis-worker`, `model: "opus"`) given the epic/product goal, the journey manifest, and screenshot paths. It rates each finding `high` / `medium` / `low` (impact-on-user), names the persona and screenshot, and proposes a concrete fix. It also lists **confirmed strengths**.

### Step 5: Report

Produce a severity-ranked report: a summary scoreboard (counts by severity + confirmed strengths), the **two root-cause patterns** with their instances grouped under them, a **Top-N in priority order**, then findings by severity (each: title, severity chip, persona/source, the defect, the fix), a **What's working** section, a one-paragraph maturity verdict, and a method/coverage appendix (which journeys were walked live vs not-walked and why). Render it as an **Artifact** (load the `artifact-design` skill first; ground it in the *product's own* brand, not a template) so it's shareable, or as markdown if no visual surface is warranted.

**Offer, don't auto-do:** if the user wants the findings actioned, they become tickets (`/create-issue` or a seeded set) and then an epic (`/plan-epic` → `/architect` → `/implement-wave` → `/close-epic`). Don't file issues or fix code as part of the review unless asked.

## Key Principles (recap)

- Drive the product as a user; read what a user reads.
- Probe explicitly for the two root causes — leaked internals and missing guardrail screens.
- Respect the auth guardrails: seeded local stack first; documented demo creds only on deployed envs; never self-grant a role; never type a real password.
- Walk empty and error states, not just the happy path — seed a sparse account on purpose.
- Report strengths as well as defects; severity is impact-on-user, not effort.

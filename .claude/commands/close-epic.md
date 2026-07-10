# Close Epic - Epic-Level Quality Gate

Runs after all tickets in an epic are implemented. Validates the epic as a whole: integration tests, mutation testing, cross-surface consistency, stitch-audit, traceability completeness, and retrospective capture.

Individual ticket quality is handled by `/implement`. This skill validates what no single ticket can: the seams between tickets.

## Usage
```
/close-epic <owner/repo> --epic "<milestone name>"
```

## Parameters
- `owner/repo`: GitHub repository in `owner/repo` format
- `--epic`: The milestone name (e.g., `"Epic: Order Lifecycle"`)

## Autonomy

**Run to completion without pausing.** This is a validation/audit skill. Present the final report and let the user decide what to act on. The only exception: if a critical integration test fails, stop and report immediately — do not continue validating on top of a broken foundation.

## Workflow

### Step 1: Resolve paths and load epic context

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# One tested call resolves CW_HOME, CW_TMP, TARGET_REPO, DEFAULT_BRANCH, EPIC_SLUG, EPIC_DIR.
# Capture first and check status so a resolver failure aborts cleanly.
CW_CTX=$(python3 "$CW_HOME/scripts/workflow_context.py" "$owner_repo" --epic "$epic_name" --shell) || {
  echo "workflow_context failed for $owner_repo" >&2; exit 1; }
eval "$CW_CTX"
```

Load epic artifacts from `$EPIC_DIR/`:
- `contracts.md`
- `state-machines.md`
- `invariants.md`
- `integration-tests.md`
- `traceability.md`

Fetch the epic's tickets:
```bash
gh issue list --repo "$owner_repo" --milestone "$epic_name" --state all --limit 100 --json number,title,state,labels
```

Verify all tickets are closed. If any are still open, report which ones and ask the user whether to proceed with a partial close or wait.

### Step 1b: Run the deterministic audit

Run the audit orchestrator once up front. It coordinates the deterministic audits — traceability coverage, unresolved markers + blocked tickets, transition-map verification (when a state machine model exists), optional stitch findings, mutation-tooling availability, and the integration test run — and writes `close-epic-manifest.json` + `close-epic-report.md`. **It exits non-zero if the epic cannot be closed** (integration tests failed, or unresolved markers still block tickets):

```bash
python3 "$CW_HOME/scripts/close_epic_audit.py" \
  --epic-dir "$EPIC_DIR" --target-repo "$TARGET_REPO" \
  --output-dir "$CW_TMP/close-epic"
```

Steps 2 (traceability), 2b (transition map), 2c (unresolved), 4 (mutation tooling), 7, and 11 **consume `$CW_TMP/close-epic/close-epic-manifest.json`** rather than recomputing — the exploratory parts (cross-surface consistency, UX flow, retrospective) still launch agents, but the audit state is structured. A `blocked: true` manifest is a workflow-level stop: resolve the failure before closing.

### Step 2: Traceability audit

Parse and audit the traceability matrix with the tested helper. It returns per-status counts, coverage %, and the gap list (rows with no test, or `missing`/`failing` status):

```bash
python3 "$CW_HOME/scripts/traceability.py" audit "$EPIC_DIR/traceability.md"
```

Then, for each acceptance criterion the audit flags as a gap (or still `covered` rather than `passing`):

1. Run the specific test referenced in the row and verify it passes.
2. Record the verified status with the updater (`passing` / `failing` / `missing`):
   ```bash
   python3 "$CW_HOME/scripts/traceability.py" update "$EPIC_DIR/traceability.md" --ticket 43 --status passing --ac "Create order"
   ```

Report:
```markdown
## Traceability Audit

| Ticket | AC | Test | Status |
|--------|----|------|--------|
| #42 | GET /health returns 200 | api_test.go:TestHealth | passing |
| #43 | Create order returns 201 | api_test.go:TestCreateOrder | passing |
| #44 | Invalid start date rejected | — | MISSING |
```

**Flag any MISSING or FAILING items.** These are gaps that must be addressed before the epic can be declared complete.

### Step 2b: Transition-map audit

If `$EPIC_DIR/models/state-machines.json` exists, run a full transition-map verification:

```bash
python3 "$CW_HOME/scripts/verify_transitions.py" "$TARGET_REPO" "$EPIC_DIR/models/state-machines.json" --output "$CW_TMP/transition-map-final.json" --format text
```

Report:
```markdown
## Transition Map Audit

### [Entity Name]
| From | To | Event | Ticket | Status | Code Location |
|------|----|-------|--------|--------|---------------|
| pending | confirmed | confirm | #43 | COVERED | handlers/booking.go:142 |
| confirmed | in_progress | start | #45 | MISSING | — |

Summary: X/Y covered (Z%), W undocumented
```

Gate criteria:
- **UNDOCUMENTED > 0**: Flag as finding — either code has unauthorized transitions or model is incomplete
- **MISSING > 0**: Flag as finding — either tickets were not fully implemented or model overspecified
- Target: 100% COVERED, 0 UNDOCUMENTED

Findings feed into Step 9 (multi-AI analysis) and the final report.

### Step 2c: Unresolved-unknowns audit

No epic closes with open unknowns in its artifacts:

```bash
python3 "$CW_HOME/scripts/check_unresolved.py" "$EPIC_DIR" --format text
```

Any surviving `TBD:`/`UNRESOLVED:`/`PLACEHOLDER` marker is a finding: either the fact was resolved during implementation (update the artifact with the real value and a citation) or it wasn't (which means some ticket was built on a guess — trace it and verify what actually shipped). Target: zero markers.

### Step 2d: Traceability coverage gate

Prove every contract/invariant is realized, guarded by code, and verified by a test — from the `@cw-trace` annotations (see `docs/traceability.md`):

```bash
python3 "$CW_HOME/scripts/check_traceability.py" "$EPIC_DIR" --source "$TARGET_REPO" --gate coverage --format text
```

**Uncovered contracts** (no code `@cw-trace guards/ensures`) and **untested contracts** (no test `@cw-trace verifies`) are findings — the contract isn't proven implemented/tested. Dangling annotations (a tag referencing an ID that no longer exists) indicate a refactor left a stale link; fix the link or the ID. Degrades gracefully when the epic uses no annotations.

### Step 2e: Single-writer coverage gate

For every invariant that declares a **single write path** / **single source of truth** (carrying `controls_field` + `sanctioned_writers` metadata — see `docs/single-writer.md`), prove no second mutator exists. This catches the class of bug where a pre-existing control (e.g. a legacy admin `ChangePlan` dropdown) is a second writer of a field an epic's invariant said had one atomic write path — something traceability and the ratchet cannot see, because they check contract↔code↔test *links* and the pass-set, not *who writes a field*.

```bash
python3 "$CW_HOME/scripts/check_single_writer.py" "$EPIC_DIR" --source "$TARGET_REPO" --gate coverage --format text
```

Any writer of a controlled field whose enclosing symbol/file is **not** in `sanctioned_writers` is a hard-blocking violation — either route it through the sanctioned path or add it to (and re-justify) the invariant's sanctioned set. Test-file writes are treated as fixtures, not violations. Degrades gracefully when the epic declares no single-write-path invariants.

### Step 2f: Ratchet gate

If the repo has `docs/quality/ratchet.json` (see `docs/ratchet.md`), the epic must close with the quality ratchet **held or advanced** — the high-water pass-set intact and no contract definition weakened or removed since the `/architect` baseline:

```bash
python3 "$CW_HOME/scripts/ratchet.py" score --repo "$TARGET_REPO"
python3 "$CW_HOME/scripts/ratchet.py" check --repo "$TARGET_REPO"
python3 "$CW_HOME/scripts/ratchet.py" recent --repo "$TARGET_REPO" --n 10   # per-wave/ticket history for the retrospective
```

A violation blocks the close: a regression means something merged that shouldn't have; a weakened/removed contract means the spec was edited outside the sanctioned path. If a contract revision was a *deliberate* decision made during the epic (confirm with the user — it should be visible in review threads, not discovered here), journal it explicitly so the baseline moves in the open, then re-check:

```bash
python3 "$CW_HOME/scripts/ratchet.py" record --repo "$TARGET_REPO" --event epic-close \
  --ref "$EPIC_SLUG" --merged --amend CTR-xxx-001 --retire INV-xxx-002 \
  --notes "<why the contract changed, link to the decision>"
```

Otherwise, once the check passes, record the epic close (same command without `--amend`/`--retire`) and commit `docs/quality/`. The journal entry is the epic's quality sign-off and feeds the next epic's amnesia context.

### Step 2g: SaaS NFR gate (optional)

For SaaS products, validate non-functional requirements (security headers + CSRF posture, auth rate-limiting, tenant isolation, health + structured logging) against the running app. Start the app if needed (don't punt), then:

```bash
python3 "$CW_HOME/scripts/saas_gate.py" --repo "$TARGET_REPO" --base-url "$BASE_URL" --gate --markdown
```

It reports five statuses (`pass`/`fail`/`warn`/`skipped`/`not_applicable`); a real `fail` (e.g. missing CSP, a cross-tenant data leak) blocks the epic close, while `warn`/`skipped` are surfaced but don't block. See `/saas-gate` for the full check list (tenant isolation, performance, data integrity need the live multi-user app).

### Step 2h: Adversarial security review (user-facing / auth / money epics)

If the epic touches **user input, authentication, identity, or money** (public or authed endpoints, login/reset/invite flows, billing), run an adversarial security review. The deterministic NFR gate (2g) checks a running app's *posture* (headers, CSRF, a live isolation probe); it cannot reason about *this epic's* logic. This step exists because functional tests, traceability, and the ratchet all pass while a real vulnerability ships — a feedback epic once closed green with an unthrottled submit endpoint (a spam/abuse vector) and a PII-in-logs leak that only a manual audit caught afterward.

Launch a **review-worker** (contract: `docs/worker-contracts.md#review-worker`) — *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"` — prompted to ATTACK the epic's new/changed endpoints and data paths against this checklist, citing `file:line` for each finding:

- **Account enumeration** — auth / reset / invite / login flows return uniform responses **and timing** whether or not the account exists.
- **Rate limiting / abuse** — every public or cheap-to-hit authed endpoint (feedback, reset, search, upload) has a limiter; an unbounded one is a spam/DoS vector.
- **IDOR / tenant isolation** — every new data-access path scopes by tenant/owner **server-side** (never trusts a client-supplied id); cross-tenant reads/writes are rejected.
- **PII / secrets in logs** — no email, token, key, or raw request body written to logs.
- **Input bounds** — unbounded strings/payloads are capped (oversized free-text fields, giant uploads).

Run the same prompt through the reviewer quorum for divergence, then reconcile the two into one findings list:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role reviewer "$CW_TMP/security-review-prompt.md" --output-dir "$CW_TMP/security-review" --cwd "$TARGET_REPO"
```

Triage every finding like the other gates: a confirmed exploitable issue is **blocking** (fix before close); a plausible-but-unproven one is **parked for the human** with the `file:line` and the concrete attack. Never close a user-facing/auth/money epic on an unreviewed security surface. Skip only when the epic is purely internal/back-office with **no new external surface** — and say so explicitly in the close report.

### Step 2i: AI-slop signals (report-only)

Two signals the literature converged on for AI-generated code degradation: **elevated 2-week churn** (code reverted/reworked soon after authoring — GitClear; DORA 2024 stability drop) and **rising production duplication** (copy/paste written to be added, not reused). Run them over the target as a standing guardrail on top of the one-off `/code-metrics` audit:

```bash
python3 "$CW_HOME/scripts/quality_slop_gate.py" --repo "$TARGET_REPO" --report
```

This is **report-only** (per `docs/gate-rollout.md`): it computes code survival (% of added lines surviving 14/30 days via git-of-theseus) and production-only duplication (% clones, tests excluded, via jscpd), prints each against GitClear's `[VENDOR]` reference bands (survival: pre-AI ~96.9% / AI-assisted ~94.3%; duplication: pre-AI 8.3% / AI 12.3%), and **always exits 0** — it never blocks the close. Surface its output verbatim in the final report under `### AI-slop signals`. It degrades gracefully: if git-of-theseus / jscpd / node are absent it prints `skipped (tool not found)`, and survival self-skips when the repo has < 14 days of history (too young to measure 2-week survival) — report that caveat honestly rather than treating a young repo as a pass. A future blocking mode is behind `--gate` (off by default, and even then only a regression *past* the AI band counts — the bands are directional).

### Step 3: Integration test execution

Run the integration tests defined in `integration-tests.md`. These test cross-ticket behaviour that no individual ticket validates.

For each integration test:

1. Set up the test scenario (create data via API, set up state)
2. Execute the assertions across multiple surfaces
3. Report pass/fail with details

If the target repo has Playwright or E2E infrastructure, use it for UI-surface assertions. Otherwise, use API calls and database queries.

**Run inside a verification worker** (contract: `docs/worker-contracts.md#verification-worker`) to keep the heavy test execution out of the orchestrator context. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`. The worker should:
- Start services if needed (`docker compose up -d`)
- Execute each integration test
- Capture results
- Clean up (`docker compose down`)
- Return a concise pass/fail summary

### Step 4: Stitch-audit across epic scope

Run `/stitch-audit` for each major feature keyword in the epic. This traces data flow across the full stack and flags where fields get lost, names drift, or validation diverges between layers.

```bash
python3 "$CW_HOME/scripts/stitch_extract.py" "$TARGET_REPO" --trace "$keyword" -o "$CW_TMP/stitch-extraction.json"
python3 "$CW_HOME/scripts/stitch_diff.py" "$CW_TMP/stitch-extraction.json" --format text -o "$CW_TMP/stitch-findings.txt"
```

If findings exist, run provenance and Gemini analysis (same as `/stitch-audit` Steps 4-5).

Report BREAK/WARN findings with fix recommendations.

### Step 5: Cross-surface consistency check

For each entity the epic touches, verify that all surfaces that display it agree:

1. Create a test entity via API (or identify an existing one)
2. Query it from every surface that should show it:
   - Admin list view
   - Admin detail view
   - Related entity views (e.g., customer profile showing orders)
   - Customer-facing views (if applicable)
   - Dashboard / summary views
3. Compare: do all surfaces show the same values for the same fields?

This catches the class of bugs where different screens use different queries or different field sources.

Report:
```markdown
## Cross-Surface Consistency

### Order #123
| Field | Admin List | Admin Detail | Customer Profile | Dashboard | Customer Portal |
|-------|-----------|-------------|----------------|-----------|-----------------|
| status | confirmed | confirmed | confirmed | confirmed | confirmed |
| customer | Jane Doe | Jane Doe | Jane Doe | — | Jane Doe |
| items | Widget, Gadget | Widget, Gadget | Widget, Gadget | — | Widget, Gadget |
| dates | Apr 5-8 | Apr 5-8 | Apr 5-8 | Apr 5 (start date) | Apr 5-8 |

All consistent: YES / NO (detail discrepancies)
```

### Step 6: UX flow audit

Walk the cross-ticket user journeys to catch UX seams that no individual ticket owns: menus that behave inconsistently across features, orphaned pages, dead-end flows, and surprise states that only emerge when multiple tickets are combined.

#### Derive journeys from `integration-tests.md`

Read `integration-tests.md` and filter for UI-facing journeys — those that reference a browser, page, route, modal, menu, or UI component. Skip API-only and database-only integration tests. For each qualifying journey, record:
- Journey name and the tickets that contributed to it
- Entry point (URL or navigation path)
- Key states and transitions described in the test spec

If `integration-tests.md` has no UI-facing journeys, skip this step and note the gap in the final report.

#### Walk each journey with Playwright/browser-use

Run inside a verification worker (contract: `docs/worker-contracts.md#verification-worker`) that has access to the target repo's Playwright or browser-use setup. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`. For each journey:

1. Start from a clean authenticated session (or unauthenticated if the journey requires it)
2. Follow every step in the journey spec
3. Capture a screenshot at every:
   - Page navigation
   - Modal or drawer open/close
   - Menu or dropdown interaction
   - State transition (e.g., form submitted, status changed)
   - Error or empty state encountered
4. Save screenshots to `$CW_TMP/ux-audit/<journey-slug>/<step-N>.png`
5. Record the sequence: step label, URL, screenshot path, any console errors

The worker returns a manifest at `$CW_TMP/ux-audit/manifest.json`:
```json
[
  {
    "journey": "Create order and view on customer profile",
    "tickets": [42, 43, 47],
    "steps": [
      { "label": "Admin creates order", "url": "/admin/orders/new", "screenshot": "step-1.png" },
      { "label": "Order appears in list", "url": "/admin/orders", "screenshot": "step-2.png" },
      { "label": "Customer profile shows order", "url": "/customers/99", "screenshot": "step-3.png" }
    ],
    "console_errors": []
  }
]
```

If the target repo has no Playwright or browser-use setup, flag the gap and skip to the findings report.

#### UX review

Launch a **synthesis worker** (contract: `docs/worker-contracts.md#synthesis-worker`) with: *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`.
- Epic goal and the original ticket requirements for each ticket referenced in the journeys
- `contracts.md`, `state-machines.md`, and `invariants.md` from the epic
- The full journey manifest with screenshot paths (the worker can view images)

The worker should evaluate each journey for epic-level UX concerns:

1. **Menu and navigation consistency**: Do menus, breadcrumbs, and navigation patterns behave the same way across features introduced by different tickets? Does a menu item added by ticket A disappear or change label on pages owned by ticket B?
2. **Information architecture**: Is data grouped and labelled logically across the full flow? Does the same entity surface under different headings or in unexpected sections depending on how the user arrived there?
3. **Dead-end states and orphan pages**: Are there pages reachable by this journey that have no clear next action or back path? Are there states where the user has completed an action but has nowhere obvious to go?
4. **Surprise states**: What happens when features from different tickets interact? Does combining the outputs of two tickets produce a state that neither ticket's requirements anticipated (e.g., an order that is both "confirmed" and "pending review" simultaneously)?
5. **Field exposure**: Are any internal, technical, or admin-only fields leaking into user-facing views? (e.g., database IDs, internal status codes, system user names)
6. **Labelling and terminology consistency**: Does the same concept use the same label across all screens in the journey, or does it drift (e.g., "booking" on one screen, "reservation" on another, "appointment" on a third)?

For each finding, record:
- Severity: `high` (blocks the journey or exposes data incorrectly), `medium` (confusing but workable), `low` (polish)
- Which ticket(s) introduced the issue
- What the finding is
- A suggested fix

The worker writes findings to `$CW_TMP/ux-audit-findings.md`.

#### Report format

```markdown
## UX Flow Audit

### Journey: Create order and view on customer profile
**Tickets**: #42, #43, #47

| Severity | Finding | Ticket(s) | Suggested fix |
|----------|---------|-----------|---------------|
| high | "Orders" tab disappears from customer profile nav when order has status "draft" — no nav path back to the list | #47 | Show tab regardless of order status |
| medium | Order status label is "CONFIRMED" (all-caps) on admin detail but "Confirmed" on customer profile — same state, inconsistent display | #42, #43 | Normalise to title case from a shared constant |
| low | After creating an order the user lands on the order detail with no breadcrumb — no path back to the order list without using the browser back button | #42 | Add breadcrumb: Orders > #123 |

### No findings
[Journey name] — no UX concerns identified.
```

UX audit findings feed into Step 9 (multi-AI analysis) — include `$CW_TMP/ux-audit-findings.md` in the findings prompt alongside the other automated gate results. High-severity UX findings must be listed in the final report under a `### UX Flow Audit` section and included in the `FIX` list if any are present.

### Step 7: Mutation testing

Run mutation testing on all files changed across the epic. This validates that the test suite actually catches bugs, not just executes code.

Identify changed files:
```bash
# Get all files changed across the epic's PRs
gh pr list --repo "$owner_repo" --state merged --search "milestone:\"$epic_name\"" --json number --jq '.[].number' | while read pr; do
  gh pr diff "$pr" --repo "$owner_repo" --name-only
done | sort -u > $CW_TMP/epic-changed-files.txt
```

Run mutation testing on changed files only (full-codebase mutation testing is too slow):

- **Go**: `go-mutesting` on changed `.go` files
- **TypeScript/JavaScript**: `npx stryker run --mutate "file1.ts,file2.ts"` (if Stryker is configured) or flag as a gap
- **Python**: `mutmut run --paths-to-mutate "file1.py,file2.py"` (if mutmut is installed) or flag as a gap

If mutation testing tooling is not available in the target repo, flag it as a recommendation and skip.

Report:
```markdown
## Mutation Testing

| File | Mutants | Killed | Survived | Score |
|------|---------|--------|----------|-------|
| order_handler.go | 24 | 22 | 2 | 91.7% |
| order_model.go | 18 | 16 | 2 | 88.9% |
| OrderList.tsx | 12 | 10 | 2 | 83.3% |

**Overall mutation score: 88.5%** (threshold: 80%)

### Surviving mutants (action needed)
- order_handler.go:142 — changed `>=` to `>` and tests still pass. Missing boundary test for capacity check.
- order_model.go:89 — removed `customer_id` nil check and tests still pass. Add test for order without customer.
```

If score is below 80%, list surviving mutants and recommend specific tests to add.

### Step 8: Invariant verification

Walk each invariant from `invariants.md` and verify it holds in the current codebase:

1. **Data integrity invariants**: Query the database or API to verify (e.g., "no order with status >= pending has null customer_id")
2. **Consistency invariants**: Covered by Step 5 (cross-surface check)
3. **Operational safety invariants**: Test by disabling services and verifying graceful degradation (e.g., disable email config, attempt email-dependent operation, verify error is surfaced not swallowed)

Report pass/fail for each invariant.

### Step 9: Multi-AI analysis of findings

The automated gates (Steps 2-8) produce raw data. Use multi-AI consultation to interpret the findings holistically — automated checks catch individual issues, but an AI review can identify patterns across them.

Prepare a findings prompt at `$CW_TMP/close-epic-review-prompt.md` containing:
- Epic goal, ticket list, contracts, and invariants
- Traceability audit results (Step 2)
- Integration test results (Step 3)
- Stitch-audit findings (Step 4)
- Cross-surface consistency results (Step 5)
- Transition-map audit results (Step 2b)
- UX flow audit findings (Step 6)
- Mutation testing results with surviving mutants (Step 7)
- Invariant verification results (Step 8)
- Specific questions:
  1. Do the surviving mutants and integration test failures point to the same underlying weakness?
  2. Are there patterns in the stitch-audit findings that suggest a systemic issue rather than isolated gaps?
  3. Based on the cross-surface consistency results, are there data model assumptions that need revisiting?
  4. Do the UX flow audit findings indicate systemic navigation or information architecture issues, or isolated per-ticket gaps?
  5. What is the highest-risk area of this epic that needs the most attention before shipping?
  6. Are there any gaps the automated checks could not cover?

Run the `reviewer` quorum (codex + gemini in parallel, with retries + output validation):

```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role reviewer $CW_TMP/close-epic-review-prompt.md \
  --output-dir "$CW_TMP/close-review" --cwd "$TARGET_REPO"
```

Read `$CW_TMP/close-review/reviewer-codex.md` and `reviewer-gemini.md` (status in `reviewer-manifest.json`). Synthesise both reviews. Categorise findings:
- **Consensus risks**: Both AIs flagged the same area — high confidence, address before shipping
- **Unique insights**: Only one AI flagged — investigate, may be a genuine blind spot or a false positive
- **Recommendations**: Suggestions for the retrospective and future epics

### Step 10: Retrospective capture

Compile a retrospective from the epic's implementation, incorporating the multi-AI analysis from Step 9:

1. **What went well**: Tickets that landed cleanly, patterns that worked
2. **What went wrong**: Bugs found during integration testing, gaps in contracts, surprising failures
3. **What to improve**: Lessons for future epics — informed by multi-AI consensus risks and unique insights. Should contracts be more specific? Were integration tests sufficient? Did the dependency ordering work?
4. **Metrics**:
   - Tickets: X planned, Y completed, Z required rework
   - Traceability: N acceptance criteria, M covered, P gaps
   - Mutation score: overall percentage
   - Integration tests: pass/fail counts
   - Stitch-audit findings: BREAK/WARN counts

Write the retrospective to `$EPIC_DIR/retrospective.md` and commit.

### Step 11: Final report

Present the full epic close report:

```markdown
## Epic Close Report: [Epic Name]

### Status: PASS / FAIL / PARTIAL

### Traceability
- X/Y acceptance criteria covered and passing
- Gaps: [list any missing coverage]

### Integration Tests
- X/Y passing
- Failures: [details]

### Stitch-Audit
- BREAK findings: N (list)
- WARN findings: N (list)

### Cross-Surface Consistency
- Entities checked: N
- Discrepancies: [list or "none"]

### Transition Map
- Entities verified: N
- Transitions: X/Y covered
- Undocumented transitions: Z (list)
- Missing implementations: W (list)

### UX Flow Audit
- Journeys walked: N
- High-severity findings: N (list)
- Medium-severity findings: N (list)
- Low-severity findings: N

### Mutation Testing
- Overall score: X%
- Surviving mutants requiring attention: N

### Invariants
- X/Y verified
- Failures: [details]

### AI-slop signals (report-only)
- Code survival (14d/30d): X% / Y% — [beats pre-AI baseline / between bands / past AI band] (or skipped: too young / tool absent)
- Production duplication: Z% — [beats pre-AI baseline / between bands / past AI band] (or skipped: tool absent)
- _[VENDOR] GitClear bands; directional. Informational — does not block the close._

### Multi-AI Analysis
- Consensus risks: [areas both AIs flagged]
- Unique insights: [areas only one AI flagged]
- Blind spots: [gaps the automated checks could not cover]

### Recommendation
- [SHIP: All gates pass] or
- [FIX: List of items to address before declaring epic complete]
```

If all gates pass, offer to close the milestone:
```bash
gh api repos/$owner_repo/milestones/$milestone_number -f state=closed
```

## Key Principles

- **This skill validates the seams, not the stitches.** Individual ticket quality is `/implement`'s job. This skill catches what emerges from the interaction between tickets.
- **Mutation testing answers "are these tests real?"** High coverage with low mutation score means the tests execute code without actually verifying behaviour.
- **The retrospective compounds.** Each epic's lessons feed into future `/architect` runs. Capture what was surprising, not what was obvious.
- **A failing gate is valuable information, not a failure.** Better to catch a cross-surface inconsistency here than in a manual bug bash.

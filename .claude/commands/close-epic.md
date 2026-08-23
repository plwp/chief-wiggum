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
# Pin the interpreter CW scripts run under. A bare `python3` is whatever
# the shell resolves, so a Homebrew bump silently strands keyring /
# jsonschema / google-genai and kills consults mid-phase (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
# One tested call resolves CW_HOME, CW_TMP, TARGET_REPO, DEFAULT_BRANCH, EPIC_SLUG, EPIC_DIR.
# Capture first and check status so a resolver failure aborts cleanly.
CW_CTX=$("${CW_PY:-python3}" "$CW_HOME/scripts/workflow_context.py" "$owner_repo" --epic "$epic_name" --shell) || {
  echo "workflow_context failed for $owner_repo" >&2; exit 1; }
eval "$CW_CTX"
```

Load epic artifacts from `$EPIC_DIR/`:
- `contracts.md`
- `state-machines.md`
- `invariants.md`
- `integration-tests.md`
- `traceability.md`

**Load the target's own review authorities (#264)** — an adopted repo's house rules are as binding on the epic-level review as CW's own checklist:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/review_authorities.py" show "$TARGET_REPO" \
  --phase review > "$CW_TMP/review-authorities.txt" || {
  echo "review-authorities binding is malformed — refusing to close the epic against CW defaults alone" >&2
  exit 2; }
```

Exit 2 means the binding exists but is unreadable — stop and fix it rather than closing an epic as if the target had no conventions. Empty output means none are recorded (the greenfield default). Otherwise load each listed skill and apply its conventions throughout the epic-level review, alongside CW's own gates.

Fetch the epic's tickets via `tracker.py` instead of calling `gh issue` directly — this is what makes the skill usable against non-github tracker backends, including a repo whose upstream has issues disabled entirely (see `docs/tracker.md`):
```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/tracker.py" --repo-root "$TARGET_REPO" members "$owner_repo" "$epic_name"
```
This returns every ticket currently grouped into the epic — `ref`, `title`, `state`, `labels`, ... — for ANY backend.

Verify all tickets are closed (`state == "closed"` for every returned ticket). If any are still open, report which ones and ask the user whether to proceed with a partial close or wait.

### Step 1b: Run the deterministic audit

Run the audit orchestrator once up front. It coordinates the deterministic audits — traceability coverage, unresolved markers + blocked tickets, transition-map verification (when a state machine model exists), optional stitch findings, mutation-tooling availability, and the integration test run — and writes `close-epic-manifest.json` + `close-epic-report.md`. **It exits non-zero if the epic cannot be closed** (integration tests failed, or unresolved markers still block tickets):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/close_epic_audit.py" \
  --epic-dir "$EPIC_DIR" --target-repo "$TARGET_REPO" \
  --output-dir "$CW_TMP/close-epic"
```

Steps 2 (traceability), 2b (transition map), 2c (unresolved), **3 (integration tests, #323)**, 4 (mutation tooling), 7, and 11 **consume `$CW_TMP/close-epic/close-epic-manifest.json`** rather than recomputing — the exploratory parts (cross-surface consistency, UX flow, retrospective) still launch agents, but the audit state is structured. A `blocked: true` manifest is a workflow-level stop: resolve the failure before closing.

The manifest carries `target_sha` — the target repo's HEAD at the moment this audit ran. Steps 2b/2c/3 below reuse the manifest's fields ONLY while `target_sha` still matches the current `$TARGET_REPO` HEAD (`git -C "$TARGET_REPO" rev-parse HEAD`) — provably the same state, nothing observed since could have changed. A step running AFTER a mutation (Step 2f's ratchet-journal commit in embedded mode is the one that can move HEAD mid-close) is not looking at stale data by accident: the sha mismatch is the signal to fall back to that step's own standalone recomputation, never to silently trust an old manifest.

### Step 2: Traceability audit

Parse and audit the traceability matrix with the tested helper. It returns per-status counts, coverage %, and the gap list (rows with no test, or `missing`/`failing` status):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/traceability.py" audit "$EPIC_DIR/traceability.md"
```

Then, for each acceptance criterion the audit flags as a gap (or still `covered` rather than `passing`):

1. Run the specific test referenced in the row and verify it passes.
2. Record the verified status with the updater (`passing` / `failing` / `missing`):
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/traceability.py" update "$EPIC_DIR/traceability.md" --ticket 43 --status passing --ac "Create order"
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

If `$EPIC_DIR/models/state-machines.json` exists, Step 1b's audit already ran this exact scan (same `$TARGET_REPO`, same model file, zero intervening writes at this point in the flow) and recorded the result in the manifest's `.transitions`. Reuse it when the manifest is still fresh (`target_sha` matches current HEAD — #323); re-run only when it isn't:

```bash
MANIFEST="$CW_TMP/close-epic/close-epic-manifest.json"
if [ "$(git -C "$TARGET_REPO" rev-parse HEAD)" = "$(jq -r .target_sha "$MANIFEST")" ]; then
  jq '.transitions' "$MANIFEST" > "$CW_TMP/transition-map-final.json"
else
  # HEAD moved since Step 1b (the manifest no longer describes the current
  # tree) — this is the ONLY case that re-runs the scan.
  "${CW_PY:-python3}" "$CW_HOME/scripts/verify_transitions.py" "$TARGET_REPO" "$EPIC_DIR/models/state-machines.json" \
    --output "$CW_TMP/transition-map-final.json" --format json > /dev/null
fi
jq -r '.outcome as $o | .summary as $s |
  "outcome: \($o) — \($s.covered)/\($s.total_model_transitions) covered, \($s.missing) missing, \($s.undocumented) undocumented",
  (.entities[] | .name as $e | (.transitions[] | select(.status=="missing") | "MISSING  \($e): \(.from) -> \(.to) (\(.event))"),
    ((.undocumented // []) | .[] | "UNDOCUMENTED  \($e): \(.from // "?") -> \(.to)"))' \
  "$CW_TMP/transition-map-final.json"
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

No epic closes with open unknowns in its artifacts. Step 1b's audit already scanned `$EPIC_DIR` for these markers into the manifest's `.unresolved`/`.blocked_tickets` — reuse it while the manifest is still fresh (same `target_sha` check as 2b); the standalone scan is the fallback, not the default:

```bash
MANIFEST="$CW_TMP/close-epic/close-epic-manifest.json"
if [ "$(git -C "$TARGET_REPO" rev-parse HEAD)" = "$(jq -r .target_sha "$MANIFEST")" ]; then
  jq -r '.unresolved[] | "\(.file):\(.location) [\(.marker)] \(.text)  blocks: \(.tickets | join(", "))"' "$MANIFEST"
else
  "${CW_PY:-python3}" "$CW_HOME/scripts/check_unresolved.py" "$EPIC_DIR" --format text
fi
```

Any surviving `TBD:`/`UNRESOLVED:`/`PLACEHOLDER` marker is a finding: either the fact was resolved during implementation (update the artifact with the real value and a citation) or it wasn't (which means some ticket was built on a guess — trace it and verify what actually shipped). Target: zero markers.

### Step 2c2: Gate-validation check (docs/gate-validation.md)

Before Steps 2d and 2e pass `--gate coverage` to `check_traceability.py` / `check_single_writer.py`, and before Step 2f passes `--gate-verifier-tests` to `ratchet.py check`, confirm each checker has EARNED that blocking authority — a passing gate-validation-protocol record proving it fires on seeded defects (including the mandatory evasion classes) and stays clean on a known-good corpus with coverage evidence, not just an assertion in a ledger. The records for CW's own gate suite ship **with chief-wiggum** at `$CW_HOME/docs/quality/validation/` (corroborated by the ratchet journal beside them), so this normally passes and Steps 2d/2e/2f keep their existing enforcement unchanged. **One process checks all three gates** (#323) — `check_gate_validation.py` accepts multiple gate names and verifies the shared ratchet journal chain once for the whole call, instead of three separate processes each re-walking the same chain from genesis:

```bash
GATE_VALIDATION=$("${CW_PY:-python3}" "$CW_HOME/scripts/check_gate_validation.py" \
  check_traceability check_single_writer ratchet \
  --validation-dir "$CW_HOME/docs/quality/validation" --format json)
echo "$GATE_VALIDATION"
TRACEABILITY_VALIDATED=$(echo "$GATE_VALIDATION" | jq -r '.gates.check_traceability.passing')
SINGLE_WRITER_VALIDATED=$(echo "$GATE_VALIDATION" | jq -r '.gates.check_single_writer.passing')
RATCHET_VALIDATED=$(echo "$GATE_VALIDATION" | jq -r '.gates.ratchet.passing')
```

(A target repo that hosts gates of its own keeps their records at the same relative path in that repo — `docs/quality/validation/<gate>.json`, sibling to its ratchet journal — and this step checks them the same way.)

**If a gate's `_VALIDATED` var is not `true` (no record, a stale/forged one, or a failing one), do not pass the corresponding blocking flag in the step below** — for `check_traceability`/`check_single_writer` that flag is `--gate coverage`; for `ratchet` it is `--gate-verifier-tests` (the ratchet's core pass-set/contract-hash check in Step 2f stays hard-blocking regardless — only the verifier-test dimension's blocking authority is governed by the record, per chief-wiggum#208) — run it report-only instead, surface a blocking finding in the close report ("`<checker>` is not validated under the gate-validation protocol — see docs/gate-validation.md"), and direct the operator to complete the protocol (or explicitly accept the risk at the human checkpoint). This is `/close-epic` refusing `--gate` for a checker lacking a passing validation record — the same "report-only until proven" posture as `docs/gate-rollout.md`, enforced mechanically here instead of by convention.

If a checker that was previously wired blocking (`check_gate_validation.py ... --wire` was run for it earlier) shows `.gates.<name>.authority.demoted == true` in `$GATE_VALIDATION`, its record went stale or missing/invalid WHILE blocking — surface the printed `DEMOTION` instruction (`.gates.<name>.authority.instruction`, carrying `previous_authority`/`demotion_reason`) verbatim in the close report alongside the coverage finding above (see `docs/gate-validation.md`'s "Auto-demotion" section, chief-wiggum#198); this is the same instruction-surfacing pattern as the escape-driven `demotion_check` in "Demotion: an escape a seed class should have caught," just triggered by staleness instead of a production escape.

### Step 2d: Traceability coverage gate

Prove every contract/invariant is realized, guarded by code, and verified by a test — from the `@cw-trace` annotations (see `docs/traceability.md`). Only pass `--gate coverage` if Step 2c2's `$TRACEABILITY_VALIDATED` is `true`; otherwise drop `--gate coverage` and report the finding instead. **One invocation does both the coverage scan and the sidecar write** (#323 — `--write-links` is a no-op when the gate doesn't pass, so it always rides the same run instead of a second full annotation scan of the whole repo moments later):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/check_traceability.py" "$EPIC_DIR" --source "$TARGET_REPO" --gate coverage --write-links --format text
```

**Uncovered contracts** (no code `@cw-trace guards/ensures`) and **untested contracts** (no test `@cw-trace verifies`) are findings — the contract isn't proven implemented/tested. Dangling annotations (a tag referencing an ID that no longer exists) indicate a refactor left a stale link; fix the link or the ID. Degrades gracefully when the epic uses no annotations.

**Suspect links (#169, report-only)**: the same run surfaces `suspect_links` — code/test links whose contract's definition hash has changed since that link was last validated ("code claims to guard CTR-X but CTR-X changed since that claim was validated"), distinct from a dangling/uncovered finding. This does not block the gate yet (see `docs/gate-rollout.md`) but re-review any suspect link before closing.

**JUSTIFIED waivers**: a contract that's genuinely not going to be covered (e.g. manual QA only) may carry a committed waiver at `docs/epics/<slug>/justifications/*.json` (`reason`/`approver`/`expiry`/`ticket` — a justification without a ticket ref is invalid and does NOT satisfy coverage). Valid, non-expired waivers render as `justified_contracts` and satisfy the coverage gate honestly instead of a fabricated guard/verify annotation.

`--write-links` (re)writes the definition-hash sidecar so future runs can detect suspect links — never hand-maintain this file. It only updates `$TARGET_REPO/docs/quality/trace-links.json` when the coverage gate passes in THIS SAME run — a failing run leaves the sidecar untouched, so a broken state is never recorded as validated. Commit this file alongside the rest of `docs/quality/` in Step 2f.

### Step 2e: Single-writer coverage gate

For every invariant that declares a **single write path** / **single source of truth** (carrying `controls_field` + `sanctioned_writers` metadata — see `docs/single-writer.md`), prove no second mutator exists. This catches the class of bug where a pre-existing control (e.g. a legacy admin `ChangePlan` dropdown) is a second writer of a field an epic's invariant said had one atomic write path — something traceability and the ratchet cannot see, because they check contract↔code↔test *links* and the pass-set, not *who writes a field*. Only pass `--gate coverage` if Step 2c2's `$SINGLE_WRITER_VALIDATED` is `true`; otherwise drop `--gate coverage` and report the finding instead:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/check_single_writer.py" "$EPIC_DIR" --source "$TARGET_REPO" --gate coverage --format text
```

Any writer of a controlled field whose enclosing symbol/file is **not** in `sanctioned_writers` is a hard-blocking violation — either route it through the sanctioned path or add it to (and re-justify) the invariant's sanctioned set. Test-file writes are treated as fixtures, not violations. Degrades gracefully when the epic declares no single-write-path invariants.

### Step 2f: Ratchet gate

`$QUALITY_DIR` is already resolved (Step 1's `workflow_context.py`, #324 — one resolution per session, reused rather than re-invoking `artifacts.py show` per step). If `$QUALITY_DIR/ratchet.json` exists (see `docs/ratchet.md`), the epic must close with the quality ratchet **held or advanced** — the high-water pass-set intact, no contract definition weakened or removed since the `/architect` baseline, and no verifier-test body rewritten behind its still-green test ID.

**Reuse Step 1b's verification run instead of paying for the suite twice** (chief-wiggum#322, same pattern as `/implement` Step 4b) — `close_epic_audit.py` already ran `ver.verify(repo, ["test"])` on this exact `$TARGET_REPO` commit and recorded it in `close-epic-manifest.json`'s `verification.steps`. When that run named a `report` for its `test`-profile step (a pytest junit-xml file) and the ratchet config has exactly one `junit-xml` suite, pass that report straight through with `--reuse-report`; otherwise fall back to a normal (re-run) `score` — never a silent skip of scoring:

```bash
REPORT=$("${CW_PY:-python3}" -c "import json; d=json.load(open('$CW_TMP/close-epic/close-epic-manifest.json')); v=d.get('verification') or {}; print(next((s['report'] for s in v.get('steps', []) if s['profile']=='test' and s.get('report')), ''))")
SUITE=$("${CW_PY:-python3}" -c "import json; d=json.load(open('$QUALITY_DIR/ratchet.json')); js=[s['name'] for s in d['suites'] if s['parser']=='junit-xml']; print(js[0] if len(js)==1 else '')")
if [ -n "$REPORT" ] && [ -n "$SUITE" ] && [ -f "$TARGET_REPO/$REPORT" ]; then
  "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" score --repo "$TARGET_REPO" --reuse-report "$SUITE=$TARGET_REPO/$REPORT"
else
  "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" score --repo "$TARGET_REPO"
fi
"${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" check --repo "$TARGET_REPO" --gate-verifier-tests
"${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" recent --repo "$TARGET_REPO" --n 10   # per-wave/ticket history for the retrospective
```

Pass `--gate-verifier-tests` only if Step 2c2's `$RATCHET_VALIDATED` is `true`; otherwise drop the flag (the check still *prints* `weakened_verifier_tests`/`removed_verifier_tests` findings report-only — surface them in the close report) and direct the operator to the gate-validation protocol, same as 2d/2e.

A violation blocks the close: a regression means something merged that shouldn't have; a weakened/removed contract means the spec was edited outside the sanctioned path. A `weakened_verifier_tests`/`removed_verifier_tests` violation (chief-wiggum#206, channel C1c) means a test annotated `@cw-trace verifies` — the executable expression of a contract — was rewritten or dropped behind its still-green test ID; fix the code, or if the verifier test was *deliberately* revised, journal it via `ratchet.py record --amend-verifier <ref>` (a human act, same semantics as `--amend` for contracts). A `missing_tests` entry caused by a genuinely flaky/order-dependent case (not a real regression) is fixed by `ratchet.py record --retire-case` with a reason and expiry (#278) — surface it to the user and get their approval; never self-approve it, and never `--force` past the gate instead; state the quarantine count (and nearest expiry) in the close report so it isn't discovered later. `check`'s output also surfaces `suspect_links` (#169) — if `docs/quality/trace-links.json` exists, any link recorded against a contract whose definition hash just changed is printed explicitly, so a weakening is never silently absorbed into "the ratchet held"; this is report-only and does not change the exit code. If a contract revision was a *deliberate* decision made during the epic (confirm with the user — it should be visible in review threads, not discovered here), journal it explicitly so the baseline moves in the open, then re-check:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" record --repo "$TARGET_REPO" --event epic-close \
  --ref "$EPIC_SLUG" --merged --amend CTR-xxx-001 --retire INV-xxx-002 \
  --notes "<why the contract changed, link to the decision>"
"${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" record --repo "$TARGET_REPO" --event epic-close \
  --ref "$EPIC_SLUG" --retire-case 'pytest::tests/test_flaky.py::*' \
  --retire-case-reason "order-dependent shared state" --retire-case-owner plwp \
  --retire-case-expiry 2026-11-01   # quarantine a flaky class (#278)
```

Otherwise, once the check passes, record the epic close (same command without `--amend`/`--retire`/`--retire-case`) and commit `docs/quality/` (embedded mode only — in sidecar mode the journal/state live outside the target, so there is nothing to commit in-tree). The journal entry is the epic's quality sign-off and feeds the next epic's amnesia context.

### Step 2g: Minimal-CI check (report-only)

An epic's quality is only as durable as the enforcement layer that keeps it green on `main`. Report whether the target repo has any GitHub Actions workflow at all — a repo with no CI lets red tests, lint errors, and uninstallable deps merge unnoticed:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ci_scaffold.py" --repo "$TARGET_REPO" --report
```

This is **report-only** (always exits 0, per `docs/gate-rollout.md`) — surface a `MISSING` finding in the close report, don't block on it. It also prints the detected stack(s); `/setup` can scaffold a minimal CI workflow (`--scaffold`) for a repo that has none.

### Step 2h: SaaS NFR gate (optional)

For SaaS products, validate non-functional requirements (security headers + CSRF posture, auth rate-limiting, tenant isolation, health + structured logging) against the running app. Start the app if needed (don't punt), then:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/saas_gate.py" --repo "$TARGET_REPO" --base-url "$BASE_URL" --gate --markdown
```

It reports five statuses (`pass`/`fail`/`warn`/`skipped`/`not_applicable`); a real `fail` (e.g. missing CSP, a cross-tenant data leak) blocks the epic close, while `warn`/`skipped` are surfaced but don't block. See `/saas-gate` for the full check list (tenant isolation, performance, data integrity need the live multi-user app).

### Step 2i: Adversarial security review (user-facing / auth / money epics)

If the epic touches **user input, authentication, identity, or money** (public or authed endpoints, login/reset/invite flows, billing), run an adversarial security review. The deterministic NFR gate (2g) checks a running app's *posture* (headers, CSRF, a live isolation probe); it cannot reason about *this epic's* logic. This step exists because functional tests, traceability, and the ratchet all pass while a real vulnerability ships — a feedback epic once closed green with an unthrottled submit endpoint (a spam/abuse vector) and a PII-in-logs leak that only a manual audit caught afterward.

Launch a **review-worker** (contract: `docs/worker-contracts.md#review-worker`) — *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"` — prompted to ATTACK the epic's new/changed endpoints and data paths against this checklist, citing `file:line` for each finding:

- **Account enumeration** — auth / reset / invite / login flows return uniform responses **and timing** whether or not the account exists.
- **Rate limiting / abuse** — every public or cheap-to-hit authed endpoint (feedback, reset, search, upload) has a limiter; an unbounded one is a spam/DoS vector.
- **IDOR / tenant isolation** — every new data-access path scopes by tenant/owner **server-side** (never trusts a client-supplied id); cross-tenant reads/writes are rejected.
- **PII / secrets in logs** — no email, token, key, or raw request body written to logs.
- **Input bounds** — unbounded strings/payloads are capped (oversized free-text fields, giant uploads).

Run the same prompt through the reviewer quorum for divergence, then reconcile the two into one findings list:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/consult_ai.py" --role reviewer "$CW_TMP/security-review-prompt.md" --output-dir "$CW_TMP/security-review" --cwd "$TARGET_REPO"
```

Triage every finding like the other gates: a confirmed exploitable issue is **blocking** (fix before close); a plausible-but-unproven one is **parked for the human** with the `file:line` and the concrete attack. Never close a user-facing/auth/money epic on an unreviewed security surface. Skip only when the epic is purely internal/back-office with **no new external surface** — and say so explicitly in the close report.

**Log a real finding as an escape.** When this adversarial review (or the cross-surface/UX review in Step 9) confirms a genuine bug that an *earlier* gate should have caught — the ticket's own tests/review, `traceability`, `ratchet`, `check_single_writer` — that's exactly the class of miss `caught` counters can't see: the gate reported clean while a real bug shipped anyway. Log it so `/reflect` can measure gate RECALL, not just catches (no-op unless telemetry is enabled, never blocks):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" bug --repo "$owner_repo" \
  --summary "reset endpoint leaks account existence via timing" --severity high \
  --missed-by ticket-gate --found-in close-epic-review --ticket 42 --fixed
```

(Convention: `docs/factory-telemetry.md` → "Escapes — measuring gate RECALL, not just catches".)

### Step 2i: AI-slop signals (report-only)

Two signals the literature converged on for AI-generated code degradation: **elevated 2-week churn** (code reverted/reworked soon after authoring — GitClear; DORA 2024 stability drop) and **rising production duplication** (copy/paste written to be added, not reused). Run them over the target as a standing guardrail on top of the one-off `/code-metrics` audit:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/quality_slop_gate.py" --repo "$TARGET_REPO" --report
```

This is **report-only** (per `docs/gate-rollout.md`): it computes code survival (% of added lines surviving 14/30 days via git-of-theseus) and production-only duplication (% clones, tests excluded, via jscpd), prints each against GitClear's `[VENDOR]` reference bands (survival: pre-AI ~96.9% / AI-assisted ~94.3%; duplication: pre-AI 8.3% / AI 12.3%), and **always exits 0** — it never blocks the close. Surface its output verbatim in the final report under `### AI-slop signals`. It degrades gracefully: if git-of-theseus / jscpd / node are absent it prints `skipped (tool not found)`, and survival self-skips when the repo has < 14 days of history (too young to measure 2-week survival) — report that caveat honestly rather than treating a young repo as a pass. A future blocking mode is behind `--gate` (off by default, and even then only a regression *past* the AI band counts — the bands are directional).

### Step 2j: Tutorial drift & coverage (report-only)

An epic that changes the UI silently invalidates the product's tutorial videos — the flows still work but the recordings now show the old chrome, and a new user-facing journey the epic added (a new nav destination, a new settings/billing surface) has no tutorial at all. "Build + tests green" never catches this; only comparing the shipped UI against the tutorial library does. This step makes that review part of the close, so a UI-touching epic can't quietly leave a stale tutorial library behind (it did, once — a UX-hardening epic drifted every provider tutorial's visuals and added billing/settings journeys with no tutorial, and nothing flagged it until a human noticed).

**Only runs when the target repo has a tutorial system.** Detect it:

```bash
TUT_STATUS_SCRIPT="$TARGET_REPO/scripts/maintain_tutorials.py"   # the repo's own tutorial maintainer
if [ -d "$TARGET_REPO/docs/tutorials" ] && [ -f "$TUT_STATUS_SCRIPT" ]; then
  # The maintainer's status scan reports per-tutorial drift (content-hash +
  # product-drift) AND coverage gaps — including a nav-vs-storyboard scan that
  # surfaces a shipped journey with no tutorial even when it has no e2e spec.
  # --json is machine-parseable and exits non-zero when anything needs work.
  python3 "$TUT_STATUS_SCRIPT" status --json > "$CW_TMP/tutorial-status.json" || true
fi
```

If the repo has no `docs/tutorials/` (or no maintainer script), **skip and say so** — most products won't have a tutorial library. When it does run, parse `$CW_TMP/tutorial-status.json` and surface, in the final report under `### Tutorial coverage`:

- **DRIFTED / PRODUCT-DRIFT** tutorials whose mapped surfaces this epic touched — their visuals/flow are stale and should be re-produced (`/tutorial-videos`). Cross-reference the epic's changed files: a tutorial is *epic-relevant* if it demonstrates a page this epic modified.
- **NAV-GAP** entries (`nav_gaps` in the JSON) — user-facing journeys the epic shipped with no tutorial (a new nav destination, a new settings/billing screen). Each is a candidate new tutorial.
- **UNCOVERED** spec-audit gaps, as before.

This is **report-only** — it never blocks the close (a stale tutorial is a follow-up, not a broken seam). Recommend `/tutorial-videos` to re-produce drifted ones and author the gaps, and **ticket the new-tutorial gaps** so they aren't lost. Do not attempt to record videos inside `/close-epic` — production needs a running instance and is its own workflow.

### Step 2j2: EU AI Act check (report-only) — chief-wiggum#316

Checks `docs/compliance/ai-act.json` against the in-force layer only (Art. 5 prohibitions, Art. 6(4) derogation documentation, Art. 50 transparency) — the Chapter III high-risk conformity pack is parked (deferred by the Digital Omnibus, harmonised standards don't exist yet):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/check_ai_act.py" "$TARGET_REPO" --format text
```

**Report-only** (always exits 0 here, per `docs/gate-rollout.md` — this gate has no `--gate` wired into any workflow yet, and won't until a dry-run against a real shipped target and a `docs/quality/validation/check_ai_act.json` record exist per `docs/gate-validation.md`). Surface the finding count in the close report under `### EU AI Act`, distinguishing the four states: `pass` (all declared features clean), `findings` (a `fail`-severity hit — a `prohibited` tier, an undocumented Annex III derogation claim, an undeclared `eu_scope`, or a **missing artifact entirely** — Art. 6(4): absence is never a silent pass), `inapplicable` (the artifact exists with an explicit empty `features: []` — a genuine, recorded "no AI functionality here"), `error` (the artifact exists and could not be parsed). A `missing` classification_status on a product with an obvious AI feature (a chat widget, a recommendation surface) is worth flagging prominently in the close report even though it doesn't block — it means the Art. 6(4) assessment was never made, which the operator should fix before, not after, this epic ships.

### Step 2j4: Boot-and-hit (report-only) — chief-wiggum#352

`go build ./...` and green package tests do not compose the binary and ask it for a route. A duplicate `mux.Handle("/")` once panicked at startup and was found on deploy as a Cloud Run crash-loop; `cmd/server` had **no test files** at all, and the demo page and `/widget.js` were never wired into the server until the deploy branch.

**Start the assembled service, then probe it.** Reaching a base URL is itself the startup check — a binary that crash-loops has no URL to give.

```bash
# Boot however this target boots (docker compose, `go run ./cmd/server`, npm start).
# Then, with $BASE_URL pointing at it:
"${CW_PY:-python3}" "$CW_HOME/scripts/check_boot_and_hit.py" "$EPIC_DIR" \
  --source "$TARGET_REPO" \
  ${BASE_URL:+--base-url "$BASE_URL"} \
  --format text
```

**Report-only** (exits 0 here, per `docs/gate-rollout.md`; no `--gate` until a `docs/quality/validation/check_boot_and_hit.json` record exists). Surface it in the close report under `### Boot-and-hit`:

- **`not_served`** (404/405) — declared in `contracts.json` and not wired into the assembled service. This is the finding the gate exists for.
- **`unreachable`** — the service did not answer at all. Never a pass; usually the startup panic.
- **`error_status`** (5xx) — registered and erroring.
- **`served` / `served_gated`** — the composition answers. A 401/403 still proves the route is registered, which is the question being asked.
- **`not_probed`** — a mutating method. The gate does not fire POST/PUT/PATCH/DELETE by default, because doing so at a real service has side effects. Pass `--probe-mutating` only against a disposable target.
- **`unprobeable`** — a parameterized path. Supply `--path-param id=123`; the gate will not invent a value, because a 404 from an invented id means "no such record" as readily as "not registered".

**Without `--base-url` the outcome is `inapplicable`, not `pass`** — nothing was composed and nothing was asked. Do not let that read as a green boot check in the close report.

The entrypoint rule is a **conjunction**: an entrypoint (`cmd/*/main.go`, `main.py`, `src/main.ts`, ...) is only a finding when it has no test files **and** no boot-and-hit coverage. An untested entrypoint whose routes just answered is exercised.

### Step 2j3: External-integration smoke (report-only) — chief-wiggum#353

Every external system this epic declares needs **one real round-trip**, and a skip must be LOUD. Four production bugs in one epic — a turn-flow bug, a missing system prompt, a trailing-newline token that made `Bearer <token>\n` an invalid HTTP header, and a guessed route — each needed exactly one real end-to-end interaction to surface, and none was required to pass. The unit tests were green the whole time, because they never touched the real system.

Run it with the epic's test results so it can tell "ran and passed" from "was skipped":

```bash
SMOKE_RESULTS="$REPORT"   # the junit-xml the verification step already produced, if any
"${CW_PY:-python3}" "$CW_HOME/scripts/check_external_smoke.py" "$EPIC_DIR" \
  --source "$TARGET_REPO" \
  ${SMOKE_RESULTS:+--results "$SMOKE_RESULTS"} \
  --format text
```

**Report-only** (exits 0 here, per `docs/gate-rollout.md`; no `--gate` until a `docs/quality/validation/check_external_smoke.json` record exists). Surface the per-system state in the close report under `### External integrations`, and treat the states as genuinely different things:

- **`verified`** — the smoke ran and passed. The only state that is evidence.
- **`unverified`** — the smoke was **SKIPPED** (credentials absent, build tag off). This is the state the ticket exists for: report it as a visible gap in the close report, never as a green tick. An integration nobody exercised is not a working integration.
- **`failed`** — the smoke ran and failed.
- **`never_ran`** — annotated, but no case in the results matched it. If the annotation is unpinned, add `case=<test name>` so matching is exact.
- **`no_smoke`** — no `@cw-smoke <system>` site anywhere. Nothing performs a real round-trip against that system.
- **`smoke_declared`** — a static-only answer (no `--results`, or an unpinned annotation whose only evidence is other passing tests in the same file). Explicitly **not** `verified`.

`inapplicable` means the epic declares no external system at all (`"external": true` with an `external_system` name — chief-wiggum#350). If this epic integrates a third-party system and this reports `inapplicable`, the gap is the *declaration*, not the smoke — see #350's declaration-gap note in Step 2j and fix it there.

### Step 2k: Remediation-epic acceptance — the inventory re-run (blocking)

**Only for remediation epics** — epics whose ticket bodies carry `DEBT-` ids
(planned by `/plan-epic --from-debt`; the plan lives at the resolver quality
dir's `remediation-plan.json`). Skip and say so otherwise.

**The inventory re-run IS this epic's acceptance test**: a remediation epic
claims specific `DEBT-` ids are gone, and the same mechanical scan that
minted them proves it. Re-run the inventory fresh (to a scratch dir, so the
epic's own baseline isn't clobbered mid-audit), then hard-check every
TICKETED id:

```bash
# $QUALITY_DIR already resolved at Step 1 (#324).
"${CW_PY:-python3}" "$CW_HOME/scripts/debt_inventory.py" --repo "$TARGET_REPO" --out "$CW_TMP/close-epic/fresh-debt"
"${CW_PY:-python3}" "$CW_HOME/scripts/plan_from_debt.py" verify --repo "$TARGET_REPO" \
  --plan "$QUALITY_DIR/remediation-plan.json" \
  --debt "$CW_TMP/close-epic/fresh-debt/debt.json"
```

`verify` exits 1 listing every ticketed `DEBT-` id still present in the fresh
inventory — a **blocking** finding: the ticket that claimed it was not
actually remediated (or the "fix" changed the code without removing the
finding). It also blocks on **MOVED** ids (#216 F1): a ticketed id absent
from the fresh inventory whose content anchor reappears under a NEW id (e.g.
a `git mv` that renamed the file without fixing the finding) is renamed, not
resolved — listed as `MOVED old -> new`. Ticketed **candidate** ids resolve
against the pending store, not the fresh inventory (#216 F2): fix the thing,
then run `debt_inventory.py resolve-candidate --repo "$TARGET_REPO" --id
DEBT-...` — the explicit operator act (pass `--repo` to `verify` so it can
read the store). Only **ticketed** ids are checked: budgeted-out leftovers
and boundary referrals are the normal end state, never a close failure. NEW
ids that appeared in a ticket's own pathset files are printed as an
informational report ("review before closing") — never a failure; note that
a REWORDED marker mints a new anchor and surfaces there, not as MOVED (the
stated anchor-compare boundary). An id may be **explicitly waived** instead
of fixed — via the `adopt.py grandfather --extend` path (a loud operator act
recording reason/owner/expiry; chief-wiggum#215) *after* planning; `verify`
reports those as WAIVED and passes, but only when the grandfather entry's own
timestamp POSTDATES the plan's `generated_at` (#216 F8) — a pre-plan
grandfather does NOT waive (remediating it was the ticket's point), an
expired grandfather never waives, and an entry without a timestamp never
waives. Once `verify` passes, refresh the real inventory in `$QUALITY_DIR`
(run `debt_inventory.py` without `--out`) so the trend line records the drop.

### Step 3: Integration test execution

Run the integration tests defined in `integration-tests.md`. These test cross-ticket behaviour that no individual ticket validates.

**Check freshness before re-running the suite** (#323): Step 1b's audit already ran `ver.verify(repo, ["test"])` — the target's full test suite, which is where a repo's integration tests actually live and run — on this exact `$TARGET_REPO` HEAD, and recorded it as `close-epic-manifest.json`'s `.verification`. Re-running the whole suite a second time, moments later, on the identical unchanged commit is the single most expensive duplicate in this workflow:

```bash
MANIFEST="$CW_TMP/close-epic/close-epic-manifest.json"
FRESH=$([ "$(git -C "$TARGET_REPO" rev-parse HEAD)" = "$(jq -r .target_sha "$MANIFEST")" ] && echo true || echo false)
SUITE_OK=$(jq -r '.verification.ok // false' "$MANIFEST")
```

- **`$FRESH == true` and `$SUITE_OK == true`**: Step 1b's run is provably the same state and it was green. Do **not** re-run the suite from scratch. Instead, walk `integration-tests.md` and, for each named integration test, confirm it is represented as a passing case in Step 1b's evidence (`.verification.steps` in the manifest, or the test report it points at) by test path/id — report it PASS on that evidence. Only actually execute a specific test yourself when you cannot find it represented there (e.g. a Playwright/E2E spec outside the `test` profile Step 1b ran) — never claim a pass with no genuine evidence behind it.
- **Otherwise** (`$FRESH == false` — a commit landed since Step 1b, e.g. Step 2f's ratchet-journal commit in embedded mode — or `$SUITE_OK == false`, or the manifest is missing): this is not a redundant second run, it is the only observation of the CURRENT state (or the first one that actually captures per-test failure detail). Run the full walk below for real.

For each integration test (when actually running it):

1. Set up the test scenario (create data via API, set up state)
2. Execute the assertions across multiple surfaces
3. Report pass/fail with details

If the target repo has Playwright or E2E infrastructure, use it for UI-surface assertions. Otherwise, use API calls and database queries.

**Run inside a verification worker** (contract: `docs/worker-contracts.md#verification-worker`) to keep the heavy test execution out of the orchestrator context. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`. Give the worker `$FRESH`/`$SUITE_OK` and the manifest path so it makes the reuse-vs-rerun call above itself, rather than the orchestrator deciding blind. The worker should:
- Start services if needed (`docker compose up -d`) — only when it is actually going to execute tests
- Execute each integration test not already evidenced by a fresh, green Step 1b run
- Capture results
- Clean up (`docker compose down`) — only if it started services
- Return a concise pass/fail summary, noting which results came from Step 1b's evidence vs. a fresh run

### Step 4: Stitch-audit across epic scope

Run `/stitch-audit` for each major feature keyword in the epic. This traces data flow across the full stack and flags where fields get lost, names drift, or validation diverges between layers.

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/stitch_extract.py" "$TARGET_REPO" --trace "$keyword" -o "$CW_TMP/stitch-extraction.json"
"${CW_PY:-python3}" "$CW_HOME/scripts/stitch_diff.py" "$CW_TMP/stitch-extraction.json" --format text -o "$CW_TMP/stitch-findings.txt"
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
- **The target's own review authorities (Step 1, #264)** — the conventions of each skill listed by `review_authorities.py show ... --phase review`, rendered inline. Loading them in Step 1 is not enough: the quorum runs as separate provider calls that see only this prompt, so an authority absent from it never reaches the reviewers and the epic closes against CW defaults alone.
- Specific questions:
  1. Do the surviving mutants and integration test failures point to the same underlying weakness?
  2. Are there patterns in the stitch-audit findings that suggest a systemic issue rather than isolated gaps?
  3. Based on the cross-surface consistency results, are there data model assumptions that need revisiting?
  4. Do the UX flow audit findings indicate systemic navigation or information architecture issues, or isolated per-ticket gaps?
  5. What is the highest-risk area of this epic that needs the most attention before shipping?
  6. Are there any gaps the automated checks could not cover?
  7. Does this epic violate any of the target's own recorded review authorities above? Attribute each such finding to the skill it comes from, so a house-rule finding is distinguishable from a CW-checklist one.

Run the `reviewer` quorum (codex + gemini in parallel, with retries + output validation):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/consult_ai.py" --role reviewer $CW_TMP/close-epic-review-prompt.md \
  --output-dir "$CW_TMP/close-review" --cwd "$TARGET_REPO"
```

Synthesise the reviews via the manifest, never by naming the files:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/synthesize_reviews.py" \
  --manifest "$CW_TMP/close-review/reviewer-manifest.json"
```

Naming them drifts as the `reviewer` role's roster changes (this step read `reviewer-gemini.md` long after gemini left the role), and a file list cannot tell "every reviewer answered" from "one never did" — chief-wiggum#416. The manifest carries who was expected and in which tier, so an absent provider is reported instead of quietly shrinking the quorum. Categorise findings:
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

**Record validation telemetry.** `/close-epic` is the epic-level validation — record its value. The deterministic audits (traceability, unresolved, single-writer, ratchet) already emit their own gate events; this captures the epic-level LLM analysis (Step 9) + cross-surface/UX findings. Emit one gate event with the total count of substantive findings surfaced across the close (exclude nits) — no-op unless telemetry is enabled, never blocks:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" emit --event gate --name close-epic \
  --result "$([ "$n_findings" -gt 0 ] && echo fail || echo pass)" --caught "$n_findings" --repo "$owner_repo"
```

(Convention: `docs/factory-telemetry.md` → "LLM validations report their value".)

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

### Tutorial coverage (report-only)
- Drifted tutorials this epic touched: [slugs] — re-produce with `/tutorial-videos` (or "none" / "no tutorial system")
- New-tutorial gaps (nav destinations the epic shipped with no tutorial): [slugs/routes] — ticketed as [#N]
- _Report-only; a stale tutorial is a follow-up, not a broken seam._

### EU AI Act (report-only)
- Classification status: [missing / recorded] — [N] feature(s) declared, outcome: [pass / findings / inapplicable / error]
- Fail-severity findings: [list, or "none"] — [e.g. "AIACT-CHATBOT-001: annex_iii_undocumented_assessment"]
- _Report-only; in-force layer (Art. 5/6(4)/50) only — the Chapter III conformity pack is parked. A `missing` status on an obviously-AI product is worth flagging even though it doesn't block._

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

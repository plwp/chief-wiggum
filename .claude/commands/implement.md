# Implement - Full Implementation Loop

The core orchestration skill. Takes a ticket and drives it through the full implementation lifecycle: clarify → consult → **test-first specification** → implement → **static analysis** → structured review → apply fixes → **verify** → validate → ship.

## Policy

- **The orchestrator verifies independently.** A worker's "tests pass" is a claim; Step 8 re-runs the suite, starts the services, and walks the acceptance criteria itself.
- **Every step runs, at every change size.** Quorum completeness is enforced mechanically (`consult_ai.py --role` and `run_review.py` exit non-zero when a required provider fails; `synthesize_reviews.py --manifest` reports an incomplete quorum) — do not route around those exits.
- **Workers never open or merge PRs, and never edit goalposts** (contracts, specs, ratchet state — `ratchet.py protected` parks such diffs for the human). The orchestrator ships in Step 11.
- **Run autonomously.** Do not pause for "ready to proceed?". User input is needed only for genuinely ambiguous requirements (Step 3), an approach conflict with no clear winner (Step 4B), and the final summary (Step 11).
- **Fix the environment, don't punt.** Docker down → start it. Dependency missing → install it.

## Disclosure (#317)

Every commit this workflow authors carries the AI-authorship trailer; PR bodies get it from `draft_pr.py` (see `docs/ai-act-posture.md`):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ai_disclosure.py" commit-trailer --file "$CW_TMP/<ticket>/commit-msg.txt"
git commit -F "$CW_TMP/<ticket>/commit-msg.txt"
```

## Usage
```
/implement <owner/repo#number> [--skip-browser-use]
```

## Parameters
- `owner/repo#number`: GitHub issue to implement (e.g., `acme/app#42`)
- `--skip-browser-use`: Skip browser-use validation step (useful if target repo has no browser-use setup)

## Workflow

### Step 1: Resolve paths and load epic context

Keep the machine awake where the tool exists, and resolve every path through the tested resolver — never hardcode:

```bash
command -v caffeinate >/dev/null && { caffeinate -ims & CAFFEINATE_PID=$!; }   # kill $CAFFEINATE_PID on exit

CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# Pinned interpreter (chief-wiggum#374) — every CW script below runs under the resolved interpreter.
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
# Resolves CW_TMP, TARGET_REPO, DEFAULT_BRANCH, ISSUE_NUMBER, QUALITY_DIR, CW_META_ROOT, CW_META_MODE once.
CW_CTX=$("${CW_PY:-python3}" "$CW_HOME/scripts/workflow_context.py" "$owner_repo#$issue_number" --shell) || {
  echo "workflow_context failed for $owner_repo#$issue_number" >&2; exit 1; }
eval "$CW_CTX"

TICKET_TMP="$CW_TMP/$issue_number"     # per-ticket scratch; $CW_TMP is per-session
mkdir -p "$TICKET_TMP"
```

**Time every phase** (`docs/factory-telemetry.md`). Stamp before, record after; `|| true` because telemetry must never fail the loop it measures:

```bash
PHASE_T0=$("${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" now)
# ... the phase runs ...
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" phase \
  --name step4a_consults --since "$PHASE_T0" --ticket "$issue_number" || true
```

Pass `--outcome error` when a phase blew up, so a fast failure doesn't read as a fast phase.

**Preflight the providers** (chief-wiggum#375) so an environment failure is found here, once, not serially mid-phase:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/provider_preflight.py" --human --usage \
  --role explorer --role reviewer --role implementer \
  | tee "$TICKET_TMP/preflight.txt"
```

Exit `0` every role can run; `1` a role is blocked by a down required provider — decide now whether to fix it or run on the named fallback voices; `2` a provider could not be verified (`unknown` is not `ok`); `3` config unreadable. `--usage` is what surfaces `exhausted` (`[SPENT]`): a quota-exhausted CLI passes every structural check. Record any degraded set — it belongs in the PR body (`synthesize_reviews.py --manifest`, chief-wiggum#416).

**Meter this build** (`docs/ticket-cost.md`) — Step 11 slices the ledger from this stamp:

```bash
export CW_TELEMETRY=1
date +%s > "$TICKET_TMP/build-start-ts"
# Catch-up ingest, bounded by --until-ts so it can never consume this ticket's own turns (chief-wiggum#345).
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" ingest-claude-transcripts \
  --since-days 7 --until-ts "$(cat "$TICKET_TMP/build-start-ts")" || true
```

**Load epic context** (if this ticket belongs to an epic):

```bash
MILESTONE=$(gh issue view "$issue_number" --repo "$owner_repo" --json milestone -q '.milestone.title // empty')
if [ -n "$MILESTONE" ]; then
  EPIC_SLUG=$("${CW_PY:-python3}" "$CW_HOME/scripts/env.py" slug "$MILESTONE")
  EPIC_DIR="$CW_META_ROOT/epics/$EPIC_SLUG"    # $CW_META_ROOT already resolved above (#324)
fi
```

`$EPIC_DIR/` holds `contracts.md`, `state-machines.md`, `invariants.md`, `traceability.md`, and optionally `models/` (`contracts.json`, `state-machines.json`, `ui-spec.json`, `test-paths.json`, `test-plan.md`, `test_state_machine.py`, `transition-map.json`). **Do not read the prose docs into your context (#333)** — note that the paths exist, then query what a given step needs via `code_query.py` (`docs/code-query.md`): `orient <file>` for what governs a file, `contract`/`state <ID>` for one artifact, `show <handle>` to dereference. The full prose docs are read only when the epic is prose-only (`$HAS_FORMAL_MODELS == false`), and then by the step that needs them, not here.

Build the artifact inventory once (discovers prose/model/design artifacts, validates model JSON, runs the unresolved-marker scan):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/epic_inventory.py" "$TARGET_REPO" --epic-slug "${EPIC_SLUG:-}" \
  ${EPIC_DIR:+--epic-dir "$EPIC_DIR"} --issue "$issue_number" > "$TICKET_TMP/inventory.json"
EPIC_STATUS=$(jq -r '.epic_status' "$TICKET_TMP/inventory.json")
if [ "$EPIC_STATUS" = "missing" ]; then
  echo "Milestone $MILESTONE names an epic but its artifacts are not at $EPIC_DIR — a moved epic, wrong election, or never-run /architect. STOP and fix the location; never proceed as a standalone ticket." >&2
  exit 1
fi
HAS_FORMAL_MODELS=$(jq -r '.flags.HAS_FORMAL_MODELS' "$TICKET_TMP/inventory.json")
HAS_UI_SPEC=$(jq -r '.flags.HAS_UI_SPEC' "$TICKET_TMP/inventory.json")
HAS_TRANSITION_MAP=$(jq -r '.flags.HAS_TRANSITION_MAP' "$TICKET_TMP/inventory.json")
[ "$HAS_FORMAL_MODELS" = "true" ] && MODELS_DIR="$EPIC_DIR/models"
```

`epic_status` is three-state (`none`/`present`/`missing`, chief-wiggum#286): `none` is a ticket with no epic and proceeds standalone; `missing` is always a defect. The epic artifacts are hard constraints — the worker must satisfy them, the review checklist verifies them, and Step 5 derives tests from the formal models mechanically.

**Unresolved-unknowns gate**: the inventory already ran the scan — read it, don't re-scan:
```bash
jq '.unresolved' "$TICKET_TMP/inventory.json"
```
If any finding's `tickets` list includes this ticket (or sits on an entity/operation it implements), do not implement on the guessed value: resolve the unknown against the real source, update the artifact with a citation, then proceed. Fallback only if `inventory.json` is missing or unreadable: `"${CW_PY:-python3}" "$CW_HOME/scripts/check_unresolved.py" "$EPIC_DIR" --format json`.

All subsequent steps work within `$TARGET_REPO`, use `$CW_HOME` for scripts/templates, `$TICKET_TMP` for per-ticket files, and `$DEFAULT_BRANCH` instead of `main`.

### Step 2: Pick and read the ticket

```bash
gh issue view "$issue_number" --repo "$owner_repo" \
  --json title,body,author,labels,assignees,milestone,comments \
  | tee "$TICKET_TMP/issue-raw.json"
```

Present title, description, acceptance criteria, labels, comments, and (if loaded) the governing contracts/invariants/transitions for this ticket. Then write the ticket context that Step 4's prompt and Step 7's review read — comments included, because AC amendments live there (#83):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/write_ticket_context.py" \
  --issue-json "$TICKET_TMP/issue-raw.json" \
  --number "$issue_number" \
  --acceptance-criteria "<AC line 1>" \
  --acceptance-criteria "<AC line 2>" \
  --output "$TICKET_TMP/ticket.json"
```

One `--acceptance-criteria` per AC line (omit for a ticket with none). Do not pass `--print` — the raw issue is already in your context once (#333).

### Step 3: Clarify requirements (only if needed)

State scope in/out. Ask only what isn't inferrable from the ticket or codebase — unclear AC, conflicting requirements, missing critical details. If the ticket is well-specified, state your understanding and move on.

### Step 4: Consult AIs on approach

```bash
PHASE_T0=$("${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" now)
```

Two phases, each kept out of the orchestrator's context: external voices propose, a synthesis worker reconciles against the live repo.

#### Phase A: Gather approaches

Write `$TICKET_TMP/approach-prompt.md`:
- Ticket title, description, acceptance criteria (from `ticket.json`).
- **Epic constraints** (if any) — these are constraints, not suggestions. With formal models, pull only the contracts/invariants/transitions this ticket touches via `code_query.py contract`/`state`/`orient`; for a prose-only epic, include the relevant `contracts.md`/`invariants.md`/`state-machines.md` sections verbatim (no cheaper alternative exists).
- **Orientation for repo-blind seats only**: providers with `reads_repo: true` (`config/providers.json`) explore the checkout themselves via `--cwd`; the role also carries `reads_repo: false` seats, and for those give the minimum lay of the land (stack, layout, test command). Never include suspected files, root causes, or solution directions — divergence is the value, and the role manifest's `blindness` report says whether a blind seat was under-served.
- The question: "Propose an implementation approach: files to modify/create, ordered plan, design decisions and trade-offs, risks, testing strategy."

Run the `explorer` quorum — parallel, retries, output validation, and a manifest naming who was expected. It exits non-zero when a required provider never produced valid output; that exit is the quorum gate, so fix and re-run rather than proceeding on the files that happen to exist:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/consult_ai.py" --role explorer "$TICKET_TMP/approach-prompt.md" \
  --output-dir "$TICKET_TMP/approaches" --cwd "$TARGET_REPO" --ticket "$issue_number"
```

Responses land at `$TICKET_TMP/approaches/explorer-<provider>.md` with `explorer-manifest.json`. An absent optional provider is a legitimate outcome; note it for the PR body.

#### Phase B: Reconcile into implementation plan

Launch a **synthesis worker** (contract: `docs/worker-contracts.md#synthesis-worker`). *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`. It has the repo; it reads the code, not a description of the code:

1. Read every `explorer-*.md` and the manifest.
2. Ground each proposal against the live checkout — `code_query.py orient` on every file the proposals name (with formal models), plus `docs/quality/hotspots.json` if present for where scrutiny concentrates; read the actual code the proposals would touch.
3. Identify consensus, conflicts, and unique insights.
4. Write `$TICKET_TMP/implementation-plan.md` as **decisions with handles**, not a code walkthrough — the implementation worker explores live too:
   - Chosen approach and why; where the voices diverged and the recommendation
   - Files to create/modify (`file:line` handles for the touch points, existing functions to extend or mirror)
   - **Contract enforcement**: which REQUIRES/ENSURES/transitions must appear as runtime guards
   - Test plan: cases to write, how to run them
   - What NOT to touch, and open questions for the user (if any)
5. Return a concise summary.

Present the summary. Ask only if approaches genuinely conflict with no clear winner; otherwise proceed to Step 5.

#### Declared touch plan (adopted repos — all ticket kinds)

Brownfield scope discipline switches on when the adoption record exists (`$CW_META_ROOT` from Step 1):

```bash
ADOPTION_JSON="$CW_META_ROOT/adoption/adoption.json"
[ -f "$ADOPTION_JSON" ] && IS_ADOPTED=true || IS_ADOPTED=false
```

When `$IS_ADOPTED`, the plan ends with a **declared pathset** (files/globs this ticket expects to touch, tests included) written to `$TICKET_TMP/pathset.json`:

- `--from-debt` ticket (body carries `DEBT-` ids and a plan reference): derive it, adding callers/tests that must move as `--collateral`:
  ```bash
  "${CW_PY:-python3}" "$CW_HOME/scripts/plan_from_debt.py" pathset \
    --plan "$QUALITY_DIR/remediation-plan.json" --id RT-001 \
    --collateral "tests/test_pricing.py" -o "$TICKET_TMP/pathset.json"
  ```
- Any other ticket:
  ```bash
  printf '%s\n' '{"paths": ["pkg/orders/*.py", "tests/test_orders.py"], "source": "ticket #42 declared touch plan"}' > "$TICKET_TMP/pathset.json"
  ```

Step 7 flags diff hunks outside the pathset for the reviewers; Step 8 re-checks (report-only per `docs/gate-rollout.md`).

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" phase \
  --name step4_consult_and_synthesis --since "$PHASE_T0" --ticket "$issue_number" || true
```

### Step 5: Test-first specification

```bash
PHASE_T0=$("${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" now)
```

#### `kind: refactor` inverts this step

For a refactor ticket (`/plan-epic --from-debt`, or labeled `refactor`), the worker writes **characterization tests that pin CURRENT behavior before any code changes** — golden values / snapshots per stack (pytest goldens, `syrupy`; Go golden files with `-update`; Jest snapshots) — all **green before the refactor commit** (a red one means you mis-captured current behavior), committed as `test: characterization baseline for #[number]`. Then: the ratchet pass-set may not shrink (retire a genuinely flaky case only via the human-approved `record --retire-case`, #278, never `--force`); run mutation testing scoped to the pathset where a tool exists (`mutmut`, `go-mutesting`, Stryker) and STATE when it was not measured; behavior preservation is the first review-checklist item; goalpost moves go through the existing `--amend`/`--retire` journal path (`docs/ratchet.md`).

Every other ticket kind proceeds as below.

**Write failing tests before implementation.** The objective becomes "make these tests pass".

**With formal models** (`$HAS_FORMAL_MODELS == true`), generate the mechanical test artifacts first — a deterministic script call, not LLM work:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/generate_formal_test_artifacts.py" "$MODELS_DIR" --output "$TICKET_TMP/"
```

The manifest (`$TICKET_TMP/formal-artifacts-manifest.json`) lists what was generated; a non-zero exit means a present model failed validation — fix the model first. The worker adapts these to the repo's test framework rather than inventing tests.

Launch an **implementation worker** (contract: `docs/worker-contracts.md#implementation-worker`) in its own isolated checkout. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`. Pass it:
- The implementation plan; epic contracts and traceability matrix if they exist; the repo's test framework and conventions
- With formal models: everything in `$TICKET_TMP/formal-artifacts-manifest.json` (test plan, test paths, contract assertions, Hypothesis skeleton, guard templates)
- With a UI spec (`$HAS_UI_SPEC == true`): the pages, component trees, and interaction contracts this ticket touches from `$MODELS_DIR/ui-spec.json` — structural decisions ("sidebar-panel", "3-dot-menu") and interactions (trigger → action → target) are binding. If the spec has a `design` section, pass its tokens, component-library binding, assets, and voice; bind tokens as CSS variables/theme values, never the library's defaults (Step 9 reviews screenshots against this).

**Worker rules**:
- No `gh pr create`/`gh pr merge`, no merging. Write code, commit to the feature branch.
- Assert isolation first — `"${CW_PY:-python3}" "$CW_HOME/scripts/git_safety.py" assert-worktree --main "$TARGET_REPO"` — and work only in the checkout root it prints. Never `cd` to `$TARGET_REPO`; never run `reset --hard`/`clean -f` on the main checkout.
- Copy each `.env.local` / `.env.*.local` from the main checkout into the worktree and symlink `node_modules`/`.venv` **before** starting any server or test run — a worktree without them runs against the wrong backend and fails in ways that look like app bugs.

The worker:
1. Creates a feature branch named after the ticket (e.g., `feat/42-add-dark-mode`)
2. Writes test files FIRST: model-derived tests (each path in `test-paths.json` a case, each invalid transition a negative case, each contract assertion a pre/postcondition check — tagged `# DERIVED: model`); one or more tests per AC (following the traceability matrix if present); contract tests for each REQUIRES/ENSURES touched; state-machine tests (adapt the Hypothesis skeleton to the real API); at least one property test for pure functions where a property library exists; at least one error-path test per endpoint/operation
3. Runs them — **all should fail**. A test that passes before implementation isn't testing new behaviour; investigate.
4. Commits: `test: add failing tests for #[number] — [title]`
5. Reports which tests were written (model-derived vs authored), frameworks used, traceability gaps, and the worktree path + branch — Step 6 works in the SAME worktree.

### Step 6: Implement

**Load the target's own authoring authorities first (#264)** — a brownfield repo's house rules, often packaged as harness skills, that a worker cannot infer from a diff:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/review_authorities.py" show "$TARGET_REPO" \
  --phase authoring > "$TICKET_TMP/authoring-authorities.txt" || {
  echo "review-authorities binding is malformed — refusing to build against CW defaults while the target's own conventions are unreadable" >&2
  exit 2; }
```

Exit 2 = binding exists but is unreadable: stop and fix it. Empty output = none recorded (normal greenfield). For each skill id printed, load it and fold its conventions into the worker prompt as binding constraints alongside the epic contracts.

Launch an **implementation worker** (contract: `docs/worker-contracts.md#implementation-worker`) in the same checkout as Step 5. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`, `isolation: "worktree"`. Pass it the plan, any user feedback, and the fact that failing tests already exist on the branch. Same worker rules as Step 5, plus:

- **Found ≠ fixed (adopted repos — `$IS_ADOPTED`)**: anything discovered mid-ticket that the ticket doesn't cover is filed the same turn as a `DEBT-` candidate and left untouched in the diff:
  ```bash
  "${CW_PY:-python3}" "$CW_HOME/scripts/debt_inventory.py" append-candidate --repo "$TARGET_REPO" \
    --engine manual --path "pkg/orders/handler.py:88" --note "duplicate retry loop, clone of billing.py"
  ```
  Candidates land in the mode-independent pending store (`~/.chief-wiggum/pending/<target-id>/candidates.json`) and leave it only via `resolve-candidate` after a reviewed fix. No drive-by fixes, formatting, or renames outside the declared pathset — those hunks get flagged and parked.

The worker:
1. Makes the failing tests green. Resolve ground truth with the tools instead of guessing — `lsp_query.py` (Go/Python; `hover`, `diagnostics`; returns `available: false` when no server is installed) and, with epic context, `code_query.py orient <file>` before editing it so an un-annotated REQUIRES/invariant isn't missed:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/lsp_query.py" --root "$(git rev-parse --show-toplevel)" diagnostics path/to/file.py
   "${CW_PY:-python3}" "$CW_HOME/scripts/code_query.py" --repo "$(git rev-parse --show-toplevel)" --epic "$EPIC_SLUG" orient path/to/file.go
   ```
2. Enforces contracts as runtime guards: every REQUIRES → guard clause at entry; every ENSURES → postcondition check (or integration test); every transition → current-state validation
3. Runs the **full** suite — a `/test` skill or `make ci` target if the repo has one (replicates CI), else the stack's standard command
4. Runs the linter if configured; runs Playwright/E2E if present
5. Fixes **all** failures, pre-existing included — every PR leaves CI green
6. Reports back after 3 failed attempts at the same error
7. **Reports honestly**: a step that could not run is stated with the reason, never marked passed. The orchestrator verifies independently.

**Frontend build principles (UI tickets)**: no `window.alert`/`confirm`/`prompt` — render messages into on-page elements with the exact required text; match the naming/id/attribute style the spec or codebase demonstrates (kebab-case `data-testid`s, BEM…) without speculatively decorating elements nothing calls for; build the complete idiomatic component (a nav bar gets its brand and links, a table its headers) without inventing features the requirements don't name.

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" phase \
  --name step5_6_tdd_and_implement --since "$PHASE_T0" --ticket "$issue_number" || true
```

### Step 7: Multi-AI code review with structured checklist

```bash
PHASE_T0=$("${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" now)
```

Runs at every change size. Run the whole step inside a **review worker** (contract: `docs/worker-contracts.md#review-worker`); the orchestrator receives only the synthesized summary. *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`.

The worker:

1. Resets the per-attempt scratch file (3b/3c append to it; a re-run after Step 8 must not inherit the previous attempt's findings, #333):
   ```bash
   mkdir -p "$TICKET_TMP/reviews"
   : > "$TICKET_TMP/reviews/review-context-extra.md"
   ```

1b. **Loads the target's review authorities (#264)**:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/review_authorities.py" show "$TARGET_REPO" \
     --phase review --format json > "$TICKET_TMP/review-authorities.json" || {
     echo "review-authorities binding is malformed — refusing to review against CW defaults alone" >&2
     exit 2; }
   ```
   Exit 2 = unreadable binding, stop. `present: false` = none recorded. Otherwise load each listed skill and render its rules into `$TICKET_TMP/review-authorities.md` — the reviewer quorum sees only the assembled prompt, so loading skills into your own context is not enough. Attribute house-rule findings to their skill in the summary.

1c. **Builds the governing-context slice.** With formal models, reviewers get the contracts that govern the changed files as handles — not the whole prose doc; the same pass flags measured hotspots (#187, advisory — a hotspot deserves deeper review, not a different bar; its absence is not evidence of safety):
   ```bash
   PROSE_ARTIFACTS=()
   if [ "$HAS_FORMAL_MODELS" = "true" ]; then
     : > "$TICKET_TMP/reviews/governing.md"
     for f in $(git diff --name-only "$DEFAULT_BRANCH"...HEAD); do
       out=$("${CW_PY:-python3}" "$CW_HOME/scripts/code_query.py" --repo "$(git rev-parse --show-toplevel)" \
         --epic "$EPIC_SLUG" --format text orient "$f")
       printf '%s\n' "$out" >> "$TICKET_TMP/reviews/governing.md"
       grep -q '^- (hotspot)' <<<"$out" && echo "$f: measured hotspot — escalate review depth" >> "$TICKET_TMP/reviews/review-context-extra.md"
     done
   elif [ -n "${EPIC_DIR:-}" ]; then   # prose-only epic: no cheaper slice exists
     PROSE_ARTIFACTS=(--epic-artifact "Contracts=$EPIC_DIR/contracts.md" --epic-artifact "Invariants=$EPIC_DIR/invariants.md")
   fi
   ```

2. Runs the review pipeline in one call — captures the `base...HEAD` diff, assembles `templates/review-prompt.md` + `review-checklist.md` + the artifacts you pass, runs the `reviewer` quorum, writes the synthesis prompt + manifest. Every provider gets the identical prompt; `config/providers.json`'s `reviewer.lenses` may add a per-provider `## Your charter` (charters in `config/lenses.json`) without changing the shared context. `ticket.json` renders the comment thread as authority-separated regions so reviewers judge against the CURRENT AC (CTR-fh-003):
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/run_review.py" \
     --ticket-context "$TICKET_TMP/ticket.json" \
     --worktree "$(git rev-parse --show-toplevel)" --base "$DEFAULT_BRANCH" \
     --output-dir "$TICKET_TMP/reviews" \
     --ticket "$issue_number" \
     --epic-artifact "Governing contracts=$TICKET_TMP/reviews/governing.md" \
     --epic-artifact "Target review authorities=$TICKET_TMP/review-authorities.md" \
     "${PROSE_ARTIFACTS[@]}"
   ```
   `--epic-artifact` silently skips a path that doesn't exist, so the fixed lines are safe unconditionally. `--ticket` attributes the quorum's spend to this ticket (chief-wiggum#345). Outputs: `impl-diff.txt`, `review-prompt.md`, `reviewer-<provider>.md`, `review-manifest.json`. Non-zero exit = a required provider never produced valid output.

3b. **Prevention signals (#216, report-only)** — new duplication, dead exports, assertion-free tests, appended for the reviewers' eyes:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/prevention_signals.py" --repo "$(git rev-parse --show-toplevel)" \
     --base "$DEFAULT_BRANCH" >> "$TICKET_TMP/reviews/review-context-extra.md"
   ```

3c. **Out-of-pathset flagging (adopted repos — `$IS_ADOPTED`, report-only)**:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" pathset --repo "$(git rev-parse --show-toplevel)" \
     --base "$DEFAULT_BRANCH" --pathset-file "$TICKET_TMP/pathset.json" --report-only \
     2>> "$TICKET_TMP/reviews/review-context-extra.md"
   ```
   Escapes — especially formatting-only hunks, renames, style changes — go in the review summary: either the declaration was wrong (fix it honestly) or the diff carries undeclared work (drop it, file it via `append-candidate`).

4. Reviews the diff itself, including `review-context-extra.md`. For `kind: refactor`, behavior preservation is the first item: characterization tests committed green BEFORE the refactor and not weakened by it.

5. Synthesizes:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/synthesize_reviews.py" \
     --manifest "$TICKET_TMP/reviews/reviewer-manifest.json"
   ```
   `--manifest`, never a hand-listed set of files (chief-wiggum#416): the manifest knows who was expected, so an absent required reviewer is reported as `QUORUM INCOMPLETE` instead of silently shrinking the quorum. When `reviewer` is lensed, expect **disjoint** findings: reconcile by union, cross-verify only genuinely contradictory claims, and never majority-vote a lensed quorum.

6. Returns a concise summary: **high-confidence fixes** (concrete bugs, apply), **medium** (verify locally, then apply), **low / architectural** (flag for the user); style-only comments ignored unless they point at a defect. Plus the **checklist scorecard** (pass/fail per item, one-line justification for failures).

7. Records the review's value (`docs/factory-telemetry.md`; no-op unless telemetry is on):
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" emit --event gate --name code-review \
     --result "$([ "$n_findings" -gt 0 ] && echo fail || echo pass)" --caught "$n_findings" --repo "$owner_repo"
   ```

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" phase \
  --name step7_review_quorum --since "$PHASE_T0" --ticket "$issue_number" || true
```

### Step 8: Apply review fixes and verify

```bash
PHASE_T0=$("${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" now)
```

Apply clear-cut fixes, flag ambiguous ones, then **the orchestrator verifies the final state itself** — not delegable.

1. **Apply clear-cut fixes** directly (no worker for trivial changes)
2. **Flag ambiguous feedback** for the user — only what genuinely needs input
3. **Static analysis** on changed files: LSP diagnostics first where available (`lsp_query.py ... diagnostics <file>`), then the linter (`golangci-lint run ./...`, `npx eslint --no-warn-ignored`/`npx biome check`, `ruff check`/`flake8`). Fix violations; gate on zero high-severity findings.
4. **Full test suite**, once, as JSON — Step 4b and the PR body both reuse this run:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/run_verification.py" --repo "$(git rev-parse --show-toplevel)" --profile test,lint,build --json > "$TICKET_TMP/verify.json"
   ```
   Non-zero exit if any step fails (`jq .ok "$TICKET_TMP/verify.json"`). All tests pass — zero tolerance.
4b. **Ratchet check** (`docs/ratchet.md`; skip only when `$QUALITY_DIR/ratchet.json` doesn't exist). Reuse Step 4's junit report rather than running the suite twice (chief-wiggum#284) — `--reuse-report` fails loudly if the report is missing or stale, which means drop to a plain `score`, never `--force`:
   ```bash
   REPORT=$("${CW_PY:-python3}" -c "import json; d=json.load(open('$TICKET_TMP/verify.json')); print(next((s['report'] for s in d['steps'] if s['profile']=='test' and s.get('report')), ''))")
   SUITE=$("${CW_PY:-python3}" -c "import json; d=json.load(open('$QUALITY_DIR/ratchet.json')); js=[s['name'] for s in d['suites'] if s['parser']=='junit-xml']; print(js[0] if len(js)==1 else '')")
   REPO_ROOT="$(git rev-parse --show-toplevel)"
   if [ -n "$REPORT" ] && [ -n "$SUITE" ] && [ -f "$REPO_ROOT/$REPORT" ]; then
     "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" score --repo "$REPO_ROOT" --reuse-report "$SUITE=$REPO_ROOT/$REPORT"
   else
     "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" score --repo "$REPO_ROOT"
   fi
   "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" check --repo "$REPO_ROOT" --gate-verifier-tests
   ```
   `--gate-verifier-tests` only if `check_gate_validation.py ratchet --validation-dir "$CW_HOME/docs/quality/validation" --gate` passes (it ships with CW); otherwise drop the flag and surface `weakened_verifier_tests` report-only. A violation blocks like a failing test: `missing_tests` = a passing case regressed; `weakened_contracts`/`removed_contracts` = a contract edited to make code pass; `weakened_verifier_tests`/`removed_verifier_tests` (#206) = a `@cw-trace verifies` test rewritten behind its green ID. Fix the code, never the contract or its verifier. A genuine revision is a human decision journaled via `record --amend`/`--retire` (contracts) or `--amend-verifier`/`--retire-verifier`; a genuinely flaky `missing_tests` case via `record --retire-case` with reason and expiry (#278) — user-approved, never self-approved, never `--force`.
4c. **Single-writer / traceability quick check** (with `$EPIC_DIR`), scoped to this branch's files — an early signal, not the coverage gate (`/close-epic` scans the whole repo):
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/check_single_writer.py" "$EPIC_DIR" --source "$(git rev-parse --show-toplevel)" \
     --changed-since "$DEFAULT_BRANCH" --format text
   "${CW_PY:-python3}" "$CW_HOME/scripts/check_traceability.py" "$EPIC_DIR" --source "$(git rev-parse --show-toplevel)" \
     --changed-since "$DEFAULT_BRANCH" --gate soundness --gate-scope changed --format text
   ```
   Traceability soundness **blocks here for findings in this diff** (chief-wiggum#379 — `@cw-trace` direction errors are unambiguous and cheap): fix the annotation, don't `--force`. Epic-doc findings stay report-only here because the worker may not edit goalposts; they block in `/architect` and `/close-epic`. To inspect a finding by ID: `code_query.py trace <BR-or-CTR-ID>`, `writers <INV-ID>`, `guards`/`verifies <CTR-ID>`.
4d. **Scope re-check after fixes (adopted repos — `$IS_ADOPTED`, report-only)**:
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" pathset --repo "$(git rev-parse --show-toplevel)" \
     --base "$DEFAULT_BRANCH" --pathset-file "$TICKET_TMP/pathset.json" --report-only
   ```
   Named files go in the PR body under "Out of declared pathset": a legitimate late addition updates the declaration with a one-line reason; an illegitimate one is dropped and filed via `append-candidate`.
5. **Start services** and verify: `docker compose up -d` if a compose file exists (start Docker itself if it's down); hit health checks and the endpoints the ticket names; check responses.
6. **Walk the acceptance criteria**: each one verified as "it works", not "code exists" — curl the endpoint, run the tests yourself.
7. **Verify contract enforcement** (with epic context): REQUIRES present as guards; an invalid transition actually rejected (try one); ENSURES hold after operations.
8. **Formal model conformance** (`$HAS_FORMAL_MODELS == true`) — mechanical, independent of worker self-reports. Count test paths covered / total, invalid transitions tested / total, guard clauses present per REQUIRES / total, invariants checked / total, and produce:
     ```
     Model conformance:
       Test paths:           X/Y covered
       Invalid transitions:  X/Y tested
       Guard clauses:        X/Y present
       Invariants:           X/Y checked
     ```
   Below 80% in any category is a flagged gap in the PR body — a signal, not a gate.
8b. **Transition-map verification** (`$HAS_TRANSITION_MAP == true`) — one JSON run writes the map and feeds the summary (#324):
   ```bash
   "${CW_PY:-python3}" "$CW_HOME/scripts/verify_transitions.py" "$(git rev-parse --show-toplevel)" "$MODELS_DIR/state-machines.json" \
     --ticket "#$issue_number" --format json --output "$MODELS_DIR/transition-map.json" > "$TICKET_TMP/verify-transitions.json"
   git add "$MODELS_DIR/transition-map.json"
   ```
   Render the summary locally:
   ```bash
   jq -r '.outcome as $o | .summary as $s |
     "outcome: \($o) — \($s.covered)/\($s.total_model_transitions) covered, \($s.missing) missing, \($s.undocumented) undocumented",
     (.entities[] | .name as $e | (.transitions[] | select(.status=="missing") | "MISSING  \($e): \(.from) -> \(.to) (\(.event))"),
       ((.undocumented // []) | .[] | "UNDOCUMENTED  \($e): \(.from // "?") -> \(.to)"))' \
     "$TICKET_TMP/verify-transitions.json"
   ```
   `missing` = an implementation gap to fix before shipping; `undocumented` = update the model or remove the code. Include `.summary` in the PR body under "Model conformance".
9. **Quality check** — read the key files: idiomatic? error handling, security, dead code? follows existing patterns? would you ship it?

**Leave services running** — Steps 9 and 10 need them (#324); teardown is at the end of Step 10.

If any verification fails: fix it directly, or re-launch the implementation worker (contract: `docs/worker-contracts.md#implementation-worker`) with specific instructions. Do not ship until it passes.

**Log a real finding as an escape** — a bug this step catches that TDD, review, or static analysis should have (measures gate recall; no-op unless telemetry is on):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" bug --repo "$owner_repo" \
  --summary "..." --severity medium --missed-by code-review \
  --found-in implement-verify --ticket "$issue_number" --fixed
```

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" phase \
  --name step8_verification --since "$PHASE_T0" --ticket "$issue_number" || true
```

### Step 9: UX sanity + design-fidelity gate

The tested gate does the mechanical setup — frontend-impact detection, ui-spec design-binding check, reference-screenshot discovery, capture planning — and emits a manifest:

```bash
git diff "$DEFAULT_BRANCH"...HEAD --name-only > "$TICKET_TMP/changed.txt"
"${CW_PY:-python3}" "$CW_HOME/scripts/ux_gate.py" \
  --changed-files "$TICKET_TMP/changed.txt" \
  $(gh issue view "$issue_number" --repo "$owner_repo" --json labels -q '.labels[].name' | sed 's/^/--label /') \
  --ui-spec "$MODELS_DIR/ui-spec.json" --design-dir "$TARGET_REPO/docs/design" \
  --screenshot-dir "$TICKET_TMP/ux-screenshots" --markdown > "$TICKET_TMP/ux-manifest.md"
```

(Add `--have-browser-use` / `--have-playwright` per the target's tooling.)

**Skip** if `--skip-browser-use` was passed or `should_run_gate` is false. If the manifest is **blocked** (frontend ticket with a design contract, no screenshot tooling), resolve the tooling — never skip the gate silently.

This is the only step in the loop that *looks* at the result: functional tests pass while screens ship off-brand or numbered from 0. It is **visual only** — for a product with a realtime/audio/interaction surface, say in the PR which surfaces were observed and which were not (chief-wiggum#355); "Step 9 passed" never stands for "the loop looked at the result" there.

#### Phase 1: Capture screenshots

In priority order:

1. **browser-use** (`tests/browser-use/run.py` or similar):
   ```bash
   cd "$TARGET_REPO" && python3 tests/browser-use/run.py \
     --scenario "ux-sanity-#$issue_number" \
     --screenshot-dir "$TICKET_TMP/ux-screenshots/"
   ```
   If ad-hoc scenario strings aren't supported, write the AC steps to `$TICKET_TMP/ux-scenario.md` and pass that.

2. **Playwright** (`playwright.config.*` present):
   ```bash
   cd "$TARGET_REPO" && npx playwright screenshot --browser chromium "$START_URL" "$TICKET_TMP/ux-screenshots/00-initial.png"
   ```
   For multi-state flows, write a throwaway `$TICKET_TMP/ux-capture.spec.ts` that screenshots each state; run once, delete.

Cover each state transition in the AC or state machine — entry, each form empty and filled, post-submit, each error state — named sequentially (`00-entry.png`, `01-form-empty.png`, …). Services are still up from Step 8; leave them up for Step 10. If capture fails entirely, note the gap and move on.

#### Phase 1b: Mechanical token check (if a design contract exists)

Before the AI review: for each color token in `design.tokens.colors`, grep the frontend's styles for it. A primary brand color that appears nowhere means the frontend ignored the contract — a hard finding without any screenshot:

```bash
"${CW_PY:-python3}" -c "
import json, sys
spec = json.load(open('$MODELS_DIR/ui-spec.json'))
for name, value in spec.get('design', {}).get('tokens', {}).get('colors', {}).items():
    print(f'{name}\t{value}')
" | while IFS=$'\t' read -r name value; do
  grep -ri --include='*.css' --include='*.scss' --include='*.ts' --include='*.tsx' -F "$value" "$WORKTREE/ui/src" >/dev/null 2>&1 \
    && echo "token $name: BOUND" || echo "token $name: MISSING ($value not found in styles)"
done
```

#### Phase 2: UX + design-fidelity review

Launch a **review worker** (contract: `docs/worker-contracts.md#review-worker`). *Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "opus"`. Give it:

1. The screenshots in `$TICKET_TMP/ux-screenshots/`
2. The full ticket prose — title, description, user story, comments — not just AC bullets
3. The domain model for the pages touched: with formal models, `code_query.py state`/`contract` slices for the entities on screen (what states are meaningful, what data belongs where); for a prose-only epic, the relevant `contracts.md`/`state-machines.md`/`invariants.md` sections
4. The visual design contract (`ui-spec.json` → `design`): tokens, component-library binding, assets, voice, and any `reference-screenshot` assets whose `applies_to` covers these pages. If `docs/design/` exists, `docs/design/reference/` is the comparison baseline and `docs/design/mockups/*.html` the living reference — pass them regardless. Include the Phase 1b output.
5. The AC bullets, with the instruction: *"These define the floor, not the ceiling. Evaluate whether the screens make sense to a user pursuing the goal in the prose, and whether they honor the design contract."*

It evaluates information architecture, navigation coherence, field exposure (internal IDs, later-step fields), state legibility, contextual fit against the domain model, missing context, and design fidelity — tokens actually applied vs library defaults, match to reference screenshots, brand assets where `applies_to` says, copy matching voice, and the 2-second human catches (0-indexed lists, raw enum labels, placeholder copy, truncation, debug output).

It writes `$TICKET_TMP/ux-review.md` and returns findings by confidence: **high** (clear UX defects and clear contract violations — fail the ticket until fixed), **medium** (apply after a quick check), **low** (note, don't block).

#### Phase 3: Apply findings

High: fix directly. Medium: check the domain model or prose, apply if confirmed. Low: PR body under "UX observations". Re-run the relevant Playwright specs afterwards.

Add a `## UX & design fidelity` section to the PR body: the flow walked, findings fixed, low-confidence observations, token-check results, and the key screenshots (commit them or upload via `gh` — the reviewer sees what shipped, not just that tests passed).

No screenshots at all is a **blocker for frontend tickets with a design contract** — fix the tooling. Only a repo with neither design contract nor browser tooling gets "UX sanity: no frontend tooling available — skipped".

### Step 10: Browser-use validation

Skip only if `--skip-browser-use` was passed. Look for the target's setup:

```bash
ls "$TARGET_REPO/tests/browser-use/run.py" "$TARGET_REPO/e2e/" "$TARGET_REPO/tests/e2e/" "$TARGET_REPO/ui/tests/" 2>/dev/null
```

- **Playwright specs**: run the ones relevant to this ticket's feature area (`cd "$TARGET_REPO/ui" && npx playwright test <specs>`); fix failures.
- **browser-use**: run the relevant scenarios (`cd "$TARGET_REPO" && python3 tests/browser-use/run.py --scenario <ids>`); capture results and screenshots.
- Neither: note the gap in the final summary.

**Clean up** — stop the services you started (`docker compose down`), now that Steps 9 and 10 are done with them.

### Step 11: Ship PR

The PR is the final artifact — not before Steps 7–10 are complete.

1. Push the branch.
2. **Mermaid diagrams** — `draft_pr.py` requires at least one. Write the body (no `%%{init}%%` line; `draft_pr.py` injects the themed init block) to `$TICKET_TMP/architecture.mmd` using the shared palette:
   ```mermaid
   graph TD
       classDef existing fill:#003f5c,stroke:#2f4b7c,color:#fff
       classDef modified fill:#665191,stroke:#a05195,color:#fff
       classDef new fill:#d45087,stroke:#f95d6a,color:#fff
       classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
   ```
   (`existing` = infrastructure/dependencies, `modified` = changed components, `new` = added components, `entry` = user-facing entry points.) A component-relationship diagram at minimum; a sequence diagram (`--mermaid-sequence`) if data flow changed. Max ~15 nodes.

3. **Price the build** (`docs/ticket-cost.md`) — windowed to this ticket's build-start stamp:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" ingest-claude-transcripts \
  --repo "$owner_repo" --ticket "$issue_number" \
  --since-ts "$(cat "$TICKET_TMP/build-start-ts")"
"${CW_PY:-python3}" "$CW_HOME/scripts/ticket_cost.py" actual \
  --repo "$owner_repo" --ticket "$issue_number" \
  --cwd-prefix "$(git rev-parse --show-toplevel)" \
  --since-ts "$(cat "$TICKET_TMP/build-start-ts")" \
  --format markdown > "$TICKET_TMP/implementation-cost.md"
```

   Add `--estimate X.XX` if the issue body carries `Nominal cost: ~$X.XX`. Keep **Unmetered** or **UNCAPTURED** lines as printed — absence of telemetry is data, not a $0 build (chief-wiggum#345).

4. Draft the PR body with the tested helper (folds in verification evidence, review/UX/model-conformance manifests, cost section; themes the diagram; validates required sections; links the issue):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/draft_pr.py" \
  --issue "$issue_number" --title "$pr_title" --summary "$summary" \
  --change "Change 1" --change "Change 2" \
  --mermaid-file "$TICKET_TMP/architecture.mmd" \
  --verification "$TICKET_TMP/verify.json" \
  --review "$TICKET_TMP/reviews/review-manifest.json" \
  --model-conformance "$TICKET_TMP/model-conformance.md" \
  --implementation-cost "$TICKET_TMP/implementation-cost.md" \
  --require-cost \
  --base "$DEFAULT_BRANCH" --out "$TICKET_TMP/pr-body.md"
gh pr create --repo "$owner_repo" --title "$pr_title" --body-file "$TICKET_TMP/pr-body.md" --base "$DEFAULT_BRANCH"
```

   (Omit `--model-conformance` / `--review` when they don't apply.) The helper adds `Closes #N`.

5. **Record the calibration point** for future `/create-issue` estimates — same `--cwd-prefix`/`--since-ts` window as the `actual` call, or `record` silently reverts to a tag-match-only slice (chief-wiggum#345). `--effort` is the issue's `S|M|L|XL` label (omit if none); `--estimate` when the issue carried a nominal cost:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ticket_cost.py" record \
  --repo "$owner_repo" --ticket "$issue_number" --effort "$effort" \
  --cwd-prefix "$(git rev-parse --show-toplevel)" \
  --since-ts "$(cat "$TICKET_TMP/build-start-ts")"
```

### Step 12: Verify CI green

Not done until all checks pass.

1. `gh pr checks <pr_number> --repo "$owner_repo" --watch` (or run the checks locally if CI is unavailable)
2. Fix any failure — pre-existing ones included — push, re-check, repeat.
3. Then the final summary: what was implemented; CI status and TDD stats; review feedback addressed/deferred; checklist scorecard; browser-use results; pre-existing fixes; traceability update; lingering questions; the PR URL.

### Step 13: Update traceability matrix

With epic context, flip this ticket's rows with the tested updater (narrow with `--ac "<criterion text>"` for a partial ticket):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/traceability.py" update "$EPIC_DIR/traceability.md" \
  --ticket "$issue_number" --status covered
```

Commit the updated `traceability.md` (or comment on the epic milestone if other tickets are in flight).

**Journal the ratchet** (if `$QUALITY_DIR/ratchet.json` exists) once the PR is merged, so its passing tests enter the high-water mark; before merge, record with `--gate pass` and without `--merged` (documents the run, doesn't move the mark — `/implement-wave` does this per wave):

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ratchet.py" record --repo "$TARGET_REPO" \
  --event ticket --ref "#$issue_number" --merged --notes "<one line: what shipped>"
if [ "$CW_META_MODE" = "embedded" ]; then   # sidecar mode keeps the journal outside the target — nothing to commit in-tree
  git -C "$TARGET_REPO" add docs/quality && git -C "$TARGET_REPO" commit -m "chore: ratchet record for #$issue_number" && git -C "$TARGET_REPO" push
fi
```

**Sweep the worktree if merged** (#329) — `gc-worktrees` only removes a worktree whose branch is provably merged, so it's a safe no-op while review is pending:
```bash
git -C "$TARGET_REPO" fetch origin "$DEFAULT_BRANCH"
"${CW_PY:-python3}" "$CW_HOME/scripts/git_safety.py" gc-worktrees --repo "$TARGET_REPO" --default-branch "$DEFAULT_BRANCH"
```

Close the loop: ask if the issue should be updated with a comment linking to the PR.

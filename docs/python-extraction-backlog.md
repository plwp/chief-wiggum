# Python Extraction Backlog

This backlog captures workflow behavior that should move out of markdown command
prompts and into testable Python. The goal is not to replace agents or human
judgment. The goal is to make deterministic orchestration, gates, artifact
handling, and repository facts executable, reusable, and harness-neutral.

## Deep Dive Summary

The current repo already has useful Python primitives:

- Path and repo resolution: `scripts/env.py`, `scripts/repo.py`
- Dependency checks and secret checks: `scripts/check_deps.py`, `scripts/keychain.py`
- Provider config and direct provider calls: `scripts/providers.py`, `scripts/consult_ai.py`
- Formal model validation/rendering: `scripts/formal_models.py`, `scripts/render_models.py`
- Unresolved marker gates: `scripts/check_unresolved.py`
- Design token extraction: `scripts/extract_design.py`
- Stitch audit extraction/diff/provenance: `scripts/stitch_*.py`
- Transition verification: `scripts/verify_transitions.py`
- Delegated-worker file protocol: `scripts/delegates/task_protocol.py`

The largest remaining risk is that the high-level workflows are still mostly
executable prose in `.claude/commands/*.md`. The markdown commands repeat shell
snippets for session setup, GitHub calls, temp directories, model generation,
review orchestration, test execution, worktree safety, PR creation, and reporting.
Those parts are deterministic enough to move to Python.

Keep the markdown files as harness adapters and operator guidance. Move durable
workflow mechanics into Python modules and CLIs.

## Extraction Principles

- Extract facts, gates, state transitions, and file operations first.
- Keep human checkpoints, taste decisions, and open-ended code synthesis in the
  harness layer until the surrounding contracts are stable.
- Prefer small CLIs that emit structured JSON plus human text over one large
  monolithic orchestrator.
- Preserve current command behavior while replacing markdown shell snippets with
  calls to Python.
- Every extraction ticket should add tests for the Python behavior and update at
  least one command prompt to call the new helper.

## Proposed Module Shape

Use a package-like layout under `scripts/chief_wiggum/` while keeping thin
backward-compatible CLIs in `scripts/*.py` where useful.

```text
scripts/chief_wiggum/
  context.py          # CW_HOME, CW_TMP, target repo, epic paths
  github.py           # gh-backed GitHub client wrappers
  artifacts.py        # epic/design artifact discovery, copy, validation
  providers.py        # provider role execution, retries, output validation
  planning.py         # milestones, dependency blocks, wave planning
  review.py           # review prompt assembly and review synthesis inputs
  verification.py     # test/lint/build/service detection and execution
  ux.py               # frontend impact, token checks, screenshot capture
  shipping.py         # PR body assembly, mermaid blocks, gh PR creation
  gitops.py           # worktree, branch, staging merge, cleanliness checks
  reports.py          # structured report models and markdown rendering
```

Existing scripts can migrate incrementally into this package. For example,
`scripts/providers.py` can either move or become a compatibility wrapper around
`scripts/chief_wiggum/providers.py`.

## Tickets

### P0-1: Add Shared Workflow Context Resolver

**Summary:** Add a Python workflow context object that resolves Chief Wiggum
home, session temp directory, target repo, default branch, issue number, milestone,
epic slug, and artifact paths in one place.

**Why:** Nearly every command repeats the same shell setup:
`CW_HOME`, `CW_TMP`, `TARGET_REPO`, `DEFAULT_BRANCH`, `EPIC_SLUG`, `EPIC_DIR`,
and ticket-scoped temp paths. Bugs here cause temp collisions, wrong repo writes,
or hardcoded branch assumptions.

**Scope:**

- Add `scripts/chief_wiggum/context.py`.
- Add a CLI, for example `python3 scripts/workflow_context.py ...`, that can emit
  JSON and shell exports.
- Reuse `scripts/env.py` and `scripts/repo.py`; do not duplicate path logic.
- Support at least these modes:
  - repo context: `owner/repo`
  - ticket context: `owner/repo#42`
  - epic context: `owner/repo --epic "Epic: Name"`

**Acceptance criteria:**

- Unit tests cover env override, cwd discovery, temp dir creation, epic slugging,
  issue parsing, and default branch fallback.
- `.claude/commands/implement.md`, `architect.md`, `implement-wave.md`, and
  `close-epic.md` use the new helper instead of repeated shell setup.
- The helper never hardcodes local checkout paths.

**Likely files:**

- `scripts/chief_wiggum/context.py`
- `scripts/workflow_context.py`
- `tests/test_workflow_context.py`
- `.claude/commands/*.md`

### P0-2: Extract GitHub Issue, Milestone, and Dependency Metadata Client

**Summary:** Create a tested GitHub wrapper around the `gh` calls used by planning,
architecture, implementation, wave execution, and close-epic.

**Why:** Markdown currently embeds repeated `gh issue list`, `gh issue view`,
`gh api repos/.../milestones`, `gh issue edit`, `gh issue comment`, and `gh pr`
commands. The dependency block in milestone descriptions is a contract and should
be parsed by tested code, not ad hoc command instructions.

**Scope:**

- Add `scripts/chief_wiggum/github.py`.
- Provide typed-ish dataclasses for `Issue`, `Milestone`, `PullRequestSummary`,
  and `DependencyGraphMetadata`.
- Use `gh` as the transport initially. Do not introduce a GitHub API dependency
  unless needed.
- Include robust parsing for the `<!-- DEPENDENCIES ... -->` milestone block.

**Acceptance criteria:**

- Tests cover dependency block parsing, missing block behavior, malformed lines,
  issue suffix parsing, and JSON output normalization.
- `/plan-epic` delegates milestone creation/dependency block formatting to Python.
- `/implement-wave` delegates dependency block parsing to Python.
- Missing or malformed dependency metadata returns structured warnings, not
  unhandled tracebacks.

**Likely files:**

- `scripts/chief_wiggum/github.py`
- `scripts/epic_metadata.py`
- `tests/test_github_metadata.py`
- `.claude/commands/plan-epic.md`
- `.claude/commands/implement-wave.md`

### P0-3: Extract Wave Planning and Gating

**Summary:** Move `/implement-wave` dependency planning, topological sorting,
gated-ticket propagation, and failed-dependency rescheduling into Python.

**Why:** Wave planning is algorithmic and risky. A mistake can launch tickets
before dependencies have landed, or implement a ticket blocked by a `TBD:` marker.

**Scope:**

- Add `scripts/chief_wiggum/planning.py`.
- Input: issues, dependency graph, closed/open state, unresolved findings.
- Output: structured JSON wave plan plus markdown report.
- Include transitive blocking: if #43 is gated or failed, tickets depending on
  #43 are held back.

**Acceptance criteria:**

- Tests cover independent tickets, chains, diamonds, cycles, missing dependency
  references, already-closed dependencies, gated tickets, and transitive gates.
- CLI exits non-zero for dependency cycles.
- `/implement-wave` Step 2 becomes a call to the planner.
- Planner output includes `waves`, `gated`, `skipped`, `warnings`, and
  `integration_risks` fields.

**Likely files:**

- `scripts/chief_wiggum/planning.py`
- `scripts/plan_waves.py`
- `tests/test_plan_waves.py`
- `.claude/commands/implement-wave.md`

### P0-4: Upgrade Provider Role Execution to Parallel Quorum Runner

**Summary:** Extend provider execution so workflows can call one role runner with
parallel execution, required/optional semantics, retries, timeout handling, and
substantive-output validation.

**Why:** `scripts/consult_ai.py --role` exists, but command prompts still launch
provider calls with shell background jobs and hand-written output checks. That
behavior should be centralized and tested.

**Scope:**

- Add or extend `scripts/chief_wiggum/providers.py`.
- Add a `run-role` CLI path that:
  - runs required and optional providers concurrently
  - retries failed required providers up to a configured count
  - validates output file existence, minimum size, and no `Timeout:`/`Error:`
  - writes a manifest JSON with status per provider
- Keep `config/providers.json` as the source of roles.

**Acceptance criteria:**

- Tests cover required provider failure, optional provider failure, retry success,
  timeout output, disabled provider, and manifest content.
- `/implement`, `/architect`, `/close-epic`, `/seed`, `/design`, and
  `/stitch-audit` call the role runner instead of shell `& wait` blocks where
  provider roles fit.
- Existing one-provider calls still work for direct use.

**Likely files:**

- `scripts/consult_ai.py`
- `scripts/chief_wiggum/providers.py`
- `tests/test_consult_ai.py`
- `tests/test_provider_quorum.py`
- `config/providers.json`
- `.claude/commands/*.md`

### P0-5: Extract Artifact Discovery and Epic Context Loading

**Summary:** Add a helper that discovers epic artifacts, formal models, UI specs,
transition maps, design artifacts, and unresolved markers for a ticket or epic.

**Why:** `/implement`, `/architect`, `/implement-wave`, and `/close-epic` all
need to know what context exists and what gates apply. Today each command
describes this in prose or shell snippets.

**Scope:**

- Add `scripts/chief_wiggum/artifacts.py`.
- Input: target repo, optional issue number, optional epic name.
- Output: JSON inventory:
  - epic dir
  - available markdown artifacts
  - available model artifacts
  - design artifacts
  - unresolved findings
  - blocked tickets
  - flags equivalent to `HAS_FORMAL_MODELS`, `HAS_UI_SPEC`, `HAS_TRANSITION_MAP`

**Acceptance criteria:**

- Tests cover no milestone, milestone without docs, full epic docs, malformed
  model JSON, unresolved marker propagation, and missing optional artifacts.
- `/implement` Step 1 and `/implement-wave` Step 1 consume the inventory.
- Output can be rendered as concise markdown for user reports.

**Likely files:**

- `scripts/chief_wiggum/artifacts.py`
- `scripts/epic_inventory.py`
- `tests/test_epic_inventory.py`
- `.claude/commands/implement.md`
- `.claude/commands/implement-wave.md`
- `.claude/commands/close-epic.md`

### P1-6: Extract Formal Test Artifact Generation

**Summary:** Create one Python command that generates all model-derived test
artifacts for a ticket or wave.

**Why:** `/implement` and `/implement-wave` duplicate calls to `render_models.py`
and `formal_models.py`. That sequence should be one idempotent operation with a
manifest of generated files.

**Scope:**

- Add `scripts/generate_formal_test_artifacts.py`.
- Input: `models/` directory and output directory.
- Generate test paths, test plan, contract assertions, Hypothesis skeleton, and
  guard templates when the source model exists.
- Emit manifest JSON and markdown summary.

**Acceptance criteria:**

- Tests cover state-machine-only, contracts-only, both models, missing models,
  invalid models, and output overwrite behavior.
- `/implement` Step 5 and `/implement-wave` Step 4a call this helper.
- Generated manifest is suitable to pass into sub-agent prompts.

**Likely files:**

- `scripts/generate_formal_test_artifacts.py`
- `scripts/chief_wiggum/artifacts.py`
- `tests/test_generate_formal_artifacts.py`
- `.claude/commands/implement.md`
- `.claude/commands/implement-wave.md`

### P1-7: Extract Review Prompt Assembly and Review Run

**Summary:** Move code review prompt creation, diff capture, role execution, output
validation, and synthesis input generation into Python.

**Why:** `/implement` Step 7 is mandatory and repeated in wave sub-agent prompts.
It assembles templates, includes contracts, runs providers, checks outputs, and
synthesizes findings. This is deterministic enough to become a single helper.

**Scope:**

- Add `scripts/chief_wiggum/review.py`.
- Add CLI: `python3 scripts/run_review.py --ticket-context ... --worktree ...`.
- Inputs:
  - ticket title/body/AC
  - base branch
  - worktree path
  - optional epic artifacts
  - review role name, default `reviewer`
- Outputs:
  - diff file
  - review prompt
  - provider responses
  - synthesis prompt/report
  - manifest JSON

**Acceptance criteria:**

- Tests cover template substitution, missing AC, large diff path, missing optional
  epic artifacts, provider manifest integration, and synthesis inputs.
- `/implement` Step 7 calls the helper.
- The helper refuses to run outside a git repo or when base branch cannot be
  resolved.

**Likely files:**

- `scripts/chief_wiggum/review.py`
- `scripts/run_review.py`
- `scripts/synthesize_reviews.py`
- `tests/test_review_pipeline.py`
- `.claude/commands/implement.md`

### P1-8: Extract Worktree and Branch Safety Checks

**Summary:** Centralize checks that prevent sub-agents from modifying the main
checkout, creating rogue PRs, or merging from the wrong branch.

**Why:** The command prompts repeatedly warn sub-agents not to operate on
`$TARGET_REPO`, but enforcement is mostly prose. Worktree and branch safety should
be executable.

**Scope:**

- Add `scripts/chief_wiggum/gitops.py`.
- Provide helpers for:
  - repo cleanliness
  - current branch/default branch checks
  - worktree root validation
  - branch name validation
  - staging branch creation
  - fast-forward promotion checks
  - changed-file listing
- Provide a CLI suitable for sub-agent prompts:
  `python3 scripts/git_safety.py assert-worktree --main ...`.

**Acceptance criteria:**

- Tests cover clean/dirty repo parsing with mocked subprocess calls, branch name
  validation, worktree path equality rejection, and fast-forward failure mapping.
- `/implement` and `/implement-wave` replace prose-only worktree assertions with
  explicit helper calls.
- Helper never runs destructive git commands.

**Likely files:**

- `scripts/chief_wiggum/gitops.py`
- `scripts/git_safety.py`
- `tests/test_gitops.py`
- `.claude/commands/implement.md`
- `.claude/commands/implement-wave.md`

### P1-9: Extract Project Verification Runner

**Summary:** Move test, lint, build, service startup, and smoke-test detection into
a Python verification runner.

**Why:** `/implement`, `/implement-wave`, `/ship`, and `/close-epic` all repeat
heuristics for Go, Node, Python, Docker, Playwright, browser-use, and health
checks. This should produce structured evidence rather than terminal prose.

**Scope:**

- Add `scripts/chief_wiggum/verification.py`.
- Add CLI: `python3 scripts/run_verification.py --profile test,lint,build,smoke`.
- Detect:
  - `Makefile`/`make ci`
  - `go.mod`
  - `package.json`
  - `pyproject.toml`/`setup.py`
  - Docker compose
  - Playwright configs
- Emit JSON and markdown evidence with command, cwd, exit code, duration, and
  log tail.

**Acceptance criteria:**

- Tests cover project detection matrix and command planning without executing
  external build tools.
- A dry-run mode prints planned commands.
- `/ship` Step 3 and `/implement` Step 8 use the runner.
- Failures are machine-readable and include enough log tail for PR evidence.

**Likely files:**

- `scripts/chief_wiggum/verification.py`
- `scripts/run_verification.py`
- `tests/test_verification.py`
- `.claude/commands/implement.md`
- `.claude/commands/implement-wave.md`
- `.claude/commands/ship.md`

### P1-10: Extract UX and Design-Fidelity Mechanics

**Summary:** Move frontend-impact detection, design token checks, screenshot
capture planning, and UX artifact manifest generation into Python.

**Why:** `/implement` Step 9 is high-value but currently long, fragile prose with
inline shell and Python snippets. The judgment-heavy review can remain with an
agent, but artifact capture and cheap mechanical checks should be code.

**Scope:**

- Add `scripts/chief_wiggum/ux.py`.
- Add CLI: `python3 scripts/ux_gate.py ...`.
- Provide:
  - frontend impact detector from labels and diff paths
  - UI spec design-token binding check
  - reference screenshot discovery from `docs/design/` and `ui-spec.json`
  - screenshot capture plan based on browser-use or Playwright availability
  - UX review prompt input manifest

**Acceptance criteria:**

- Tests cover frontend path detection, label detection, no-frontend skip, token
  present/missing checks, design asset discovery, and manifest rendering.
- `/implement` Step 9 uses the helper for all mechanical setup before launching
  the UX reviewer.
- For frontend tickets with a design contract, missing screenshot tooling is
  reported as a structured blocker.

**Likely files:**

- `scripts/chief_wiggum/ux.py`
- `scripts/ux_gate.py`
- `tests/test_ux_gate.py`
- `.claude/commands/implement.md`

### P1-11: Extract PR Body and Mermaid Diagram Scaffolding

**Summary:** Create a shipping helper that assembles PR body sections, enforces
required sections, includes verification evidence, and provides reusable Mermaid
theme snippets.

**Why:** `/ship` and `/implement` both require PR bodies with diagrams, evidence,
contract compliance, model conformance, and UX results. The color palette and
template requirements are duplicated in markdown.

**Scope:**

- Add `scripts/chief_wiggum/shipping.py`.
- Add CLI: `python3 scripts/draft_pr.py ...`.
- Input manifests from review, verification, UX, formal conformance, and issue
  context.
- Output PR markdown, title suggestion, and optional `gh pr create` command.
- Include Mermaid theme helpers and section validators.

**Acceptance criteria:**

- Tests cover required section validation, issue linking, model conformance
  section inclusion/omission, UX section inclusion, and Mermaid theme injection.
- `/ship` uses this helper to draft the PR body.
- `/implement` Step 11 reuses the same helper.

**Likely files:**

- `scripts/chief_wiggum/shipping.py`
- `scripts/draft_pr.py`
- `tests/test_shipping.py`
- `templates/pr.md`
- `.claude/commands/ship.md`
- `.claude/commands/implement.md`

### P1-12: Extract Architecture Artifact Commit/Install Step

**Summary:** Move `/architect` artifact assembly, validation, copy-to-epic-dir,
derived-view generation, transition-map initialization, commit, and issue-comment
preparation into Python.

**Why:** `/architect` already has strong model generation and validation helpers,
but the final artifact installation step is still a long shell recipe. This step
touches target repo files and should be reliable.

**Scope:**

- Add `scripts/install_epic_artifacts.py`.
- Validate source artifacts from temp dir.
- Create target `docs/epics/<slug>/` layout.
- Copy prose and JSON artifacts.
- Generate machine/test views.
- Initialize transition map.
- Prepare issue comment bodies.
- Optionally commit, but support `--no-commit` for dry runs.

**Acceptance criteria:**

- Tests cover missing required artifacts, optional UI spec, generated view paths,
  transition-map command invocation, dry-run output, and issue comment rendering.
- `/architect` Step 7 and Step 8 call this helper.
- The helper refuses to commit on a dirty target repo unless explicitly allowed.

**Likely files:**

- `scripts/install_epic_artifacts.py`
- `scripts/chief_wiggum/artifacts.py`
- `scripts/chief_wiggum/gitops.py`
- `tests/test_install_epic_artifacts.py`
- `.claude/commands/architect.md`

### P2-13: Extract Traceability Matrix Parser and Updater

**Summary:** Add a parser/updater for `traceability.md` that can audit rows,
update status, and render reports.

**Why:** Traceability appears in `/architect`, `/implement`, `/implement-wave`,
and `/close-epic`, but status updates are still described as manual markdown
edits. This is central to the workflow contract.

**Scope:**

- Add `scripts/chief_wiggum/traceability.py`.
- Parse the markdown table generated by `/architect`.
- Support statuses: `pending`, `covered`, `passing`, `failing`, `missing`.
- Update rows by ticket number and AC text/test reference.
- Emit audit JSON and markdown.

**Acceptance criteria:**

- Tests cover normal table parsing, escaped pipes, missing columns, duplicate ACs,
  status updates, and report rendering.
- `/implement` Step 13 uses the updater.
- `/close-epic` Step 2 uses the parser for audit setup.

**Likely files:**

- `scripts/chief_wiggum/traceability.py`
- `scripts/traceability.py`
- `tests/test_traceability.py`
- `.claude/commands/implement.md`
- `.claude/commands/close-epic.md`

### P2-14: Extract Close-Epic Audit Orchestrator

**Summary:** Build a close-epic audit runner that coordinates deterministic audit
steps and emits a final report manifest.

**Why:** `/close-epic` is mostly deterministic audit coordination: traceability,
transition map, unresolved markers, stitch audit, mutation tooling availability,
invariant report slots, and final report assembly. The exploratory parts can
still launch agents, but the audit state should be structured.

**Scope:**

- Add `scripts/close_epic_audit.py`.
- Coordinate existing helpers:
  - `check_unresolved.py`
  - `verify_transitions.py`
  - `stitch_extract.py`
  - `stitch_diff.py`
  - `stitch_provenance.py`
  - future traceability parser
  - future verification runner
- Emit `close-epic-manifest.json` and `close-epic-report.md`.

**Acceptance criteria:**

- Tests cover no formal models, formal models present, unresolved findings,
  stitch findings, missing mutation tooling, and report rendering.
- `/close-epic` Steps 2, 2b, 2c, 4, 7, and 11 call the audit runner or consume
  its manifest.
- Critical integration test failure remains a workflow-level stop condition.

**Likely files:**

- `scripts/close_epic_audit.py`
- `scripts/chief_wiggum/reports.py`
- `tests/test_close_epic_audit.py`
- `.claude/commands/close-epic.md`

### P2-15: Extract Design Artifact Assembly

**Summary:** Move `/design` artifact assembly, token validation, styleguide
rendering, reference asset path validation, and commit preparation into Python.

**Why:** The creative design direction generation should stay agent/human driven,
but the final `docs/design/` assembly is deterministic and should be checked.

**Scope:**

- Add `scripts/install_design_artifacts.py`.
- Input chosen direction directory, screenshot directory, `design.json`, and
  target repo.
- Validate `design.json`.
- Render styleguide.
- Copy mockups and screenshots to stable paths.
- Verify `reference-screenshot` assets point to committed files.
- Optionally commit, with dry-run support.

**Acceptance criteria:**

- Tests cover missing screenshots, invalid design JSON, broken asset references,
  styleguide generation, and dry-run manifest.
- `/design` Step 5 and Step 6 call this helper.
- The helper reports surviving `TBD:` markers with blocked frontend-ticket impact.

**Likely files:**

- `scripts/install_design_artifacts.py`
- `scripts/extract_design.py`
- `tests/test_install_design_artifacts.py`
- `.claude/commands/design.md`

### P2-16: Extract Setup Profile Recommendation

**Summary:** Make `/setup` ask Python which dependency profiles apply to a
requested workflow or harness instead of listing shell commands manually.

**Why:** Dependency profiles already exist in `scripts/check_deps.py`; setup
should use that as the source of truth for profile recommendations.

**Scope:**

- Add a `list-profiles` or `recommend` mode to `scripts/check_deps.py`.
- Given workflow names or provider roles, return the required profiles.
- Include provider role to dependency profile mapping.

**Acceptance criteria:**

- Tests cover role-to-provider profile expansion and existing profile behavior.
- `/setup` uses the recommendation output.
- Documentation in `README.md` and `docs/harnesses.md` stays consistent.

**Likely files:**

- `scripts/check_deps.py`
- `tests/test_check_deps.py`
- `.claude/commands/setup.md`
- `README.md`
- `docs/harnesses.md`

### P3-17: Add a Thin `cw` CLI Facade

**Summary:** Add a single optional CLI facade for discoverability once the smaller
helpers exist.

**Why:** Do not start with a giant orchestrator. After the durable helpers exist,
a `cw` facade can make local use easier and reduce adapter-specific command
duplication.

**Scope:**

- Add console entrypoint or script wrapper with subcommands that call existing
  helpers:
  - `cw context`
  - `cw plan-waves`
  - `cw epic-inventory`
  - `cw run-review`
  - `cw run-verification`
  - `cw draft-pr`
- Keep subcommand implementation thin.

**Acceptance criteria:**

- CLI help lists all available helpers.
- Existing script entrypoints remain valid.
- Tests cover argument dispatch only; business logic remains tested in modules.

**Likely files:**

- `scripts/cw.py`
- `pyproject.toml`
- `tests/test_cw_cli.py`

## Things Not To Extract Yet

### Do Not Extract Full `/implement` Code Writing

The actual code-writing loop still needs an agent because it requires local
pattern recognition, judgment, and iterative debugging. Extract the gates around
it first: context, test artifact generation, review, verification, UX, shipping,
and traceability.

### Do Not Extract Human Taste Selection in `/design`

Direction generation, taste selection, and qualitative design critique should
remain human/agent work. Extract artifact validation and installation only.

### Do Not Extract Architecture Synthesis as Deterministic Code

The generation of contracts, state machines, ADRs, and integration-test ideas is
still model-assisted synthesis. The Python layer should validate, render, install,
and report the artifacts, not pretend it can infer the correct architecture
without an agent.

### Do Not Extract Mermaid Diagram Authoring Fully

Python can enforce required sections and provide theme snippets. The diagram
content still benefits from diff-aware agent judgment. A later helper can validate
syntax or section presence, but full automatic diagram generation is lower value.

## Suggested Sequencing

1. P0-1 context resolver
2. P0-2 GitHub metadata client
3. P0-3 wave planner
4. P0-4 provider quorum runner
5. P0-5 artifact inventory
6. P1-6 formal test artifact generator
7. P1-7 review runner
8. P1-8 git/worktree safety
9. P1-9 verification runner
10. P1-10 UX/design mechanics
11. P1-11 PR drafting
12. P1-12 architecture artifact installer
13. P2-13 traceability parser/updater
14. P2-14 close-epic audit runner
15. P2-15 design artifact installer
16. P2-16 setup profile recommendation
17. P3-17 `cw` facade

This order pulls the most repeated and highest-risk shell/prose contracts into
Python first, while leaving agent judgment intact.

## First Epic Candidate

If these tickets are grouped into one initial epic, use:

**Epic: Portable Python Orchestration Core**

Goal: Make command workflows harness-neutral by extracting deterministic setup,
metadata, planning, provider execution, artifact inventory, and verification
gates from markdown into tested Python helpers.

Initial ticket set:

- P0-1 Shared Workflow Context Resolver
- P0-2 GitHub Issue, Milestone, and Dependency Metadata Client
- P0-3 Wave Planning and Gating
- P0-4 Provider Role Execution to Parallel Quorum Runner
- P0-5 Artifact Discovery and Epic Context Loading

These five tickets create the foundation needed for the remaining extraction
work without prematurely building a monolithic orchestrator.

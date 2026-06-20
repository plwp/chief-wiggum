# Worker Contracts

Chief Wiggum workflows delegate sub-tasks to **workers**. A worker is described
by a harness-neutral *contract*, not by a Claude Code parameter. Any harness
(Claude Code sub-agents, Codex, another orchestrator) can run a worker as long
as it satisfies the contract; the Claude `subagent_type`/`model`/`isolation`
values are one adapter's way of realizing it (inventoried in
`docs/harness-adapters.md`).

Where a harness supports sub-agents it may spawn them; otherwise the main
orchestrator runs the worker contract locally or via a provider adapter. This
makes the workflow text portable — it does not require parallel worker execution
in every harness.

## Contract fields

Every worker launch defines:

- **Role** — what the worker is for.
- **Inputs** — the artifacts/paths it is given.
- **Output artifact paths** — where it must write its result. The orchestrator
  reads these; a conversational reply is not the contract.
- **Write scope** — which files/directories it may modify.
- **Isolation** — a required *behavior*, not a parameter. A code worker MUST
  operate in its own checkout and never touch the main checkout (enforce with
  `scripts/git_safety.py assert-worktree --main "$TARGET_REPO"`).
- **Stop condition** — when the worker is done, and when it must stop and report
  instead of guessing.

## Completion signalling

A worker's completion is signalled through its **output artifact** (a file at a
known path) and, for background workers, a status file (`done`/`error` with a
reason) — not through a harness-specific event stream. An orchestrator polls
for / reads those files. Claude Code's background-agent notifications are one
adapter for the same "the artifact now exists" signal; the file-based
delegated-worker protocol in `scripts/delegates/` is the portable form. Do not
rely on Claude Agent notifications as the portable completion contract.

## Claude Code adapter mapping

| Contract concept | Claude Code adapter |
|------------------|---------------------|
| explorer worker | `subagent_type: "Explore"` (or `"general-purpose"`), `thoroughness:` |
| any worker, model tier | `model: "opus" \| "sonnet"` — prefer a provider role where portability matters |
| worktree isolation | `isolation: "worktree"` |
| async completion | `run_in_background` + task-notification → a status file |

When a workflow names these Claude parameters, they appear on a
`Claude Code adapter:` note — the portable description (role / inputs / outputs /
scope / isolation / stop) is what another harness implements.

## Standard workers

### read-only-explorer-worker

- **Role**: explore the target repo / context; read-only.
- **Inputs**: a focus area (ticket description, labels, paths to investigate).
- **Output artifact paths**: a findings file (e.g. `$TICKET_TMP/codebase-context.md`)
  plus, when run in the background, a status file under `$TICKET_TMP/workers/`.
- **Write scope**: its own output/status artifacts only.
- **Isolation**: none required (read-only); must not modify repo files.
- **Stop condition**: findings artifact written (status `done`); on failure,
  status `error` with a concise reason.

### approach-worker

- **Role**: propose an implementation approach for the ticket.
- **Inputs**: the approach prompt (ticket + AC + orientation context).
- **Output artifact paths**: an approach file (e.g. `$TICKET_TMP/approach-<id>.md`).
- **Write scope**: its own output artifact only.
- **Isolation**: none required.
- **Stop condition**: approach artifact written.

### implementation-worker

- **Role**: write failing tests then implementation from the approved plan.
- **Inputs**: `$TICKET_TMP/implementation-plan.md`, ticket details, epic context,
  generated test artifacts, the target repo path.
- **Output artifact paths**: the feature branch + worktree (committed code),
  test results, and `$TICKET_TMP/impl-diff.txt`.
- **Write scope**: its own git worktree and `$TICKET_TMP` only — never the main
  checkout; never `gh pr create`/`merge` (the orchestrator owns shipping).
- **Isolation**: required — operate in a dedicated git worktree and assert it is
  not the main checkout with `scripts/git_safety.py assert-worktree`.
- **Stop condition**: tests green, lint clean, acceptance verified; or a blocking
  error reported after the retry budget is exhausted.

### review-worker

- **Role**: review the diff / run the reviewer quorum and synthesize findings.
- **Inputs**: the diff, ticket context, optional epic artifacts.
- **Output artifact paths**: review responses + a synthesis/report file under
  `$TICKET_TMP/reviews/`.
- **Write scope**: its review/output artifacts only.
- **Isolation**: none required (reads the diff).
- **Stop condition**: synthesis report written.

### synthesis-worker

- **Role**: reconcile multiple inputs (approaches, reviews) into one artifact.
- **Inputs**: the artifacts to reconcile (e.g. the approach files + codebase context).
- **Output artifact paths**: the synthesized artifact (e.g.
  `$TICKET_TMP/implementation-plan.md`).
- **Write scope**: its own output artifact only.
- **Isolation**: none required.
- **Stop condition**: synthesized artifact written.

### verification-worker

- **Role**: execute integration tests / browser journeys — starts services, runs
  tests, captures evidence. Distinct from review-worker (which only reads a diff).
- **Inputs**: the test scenarios / integration-test spec, the target repo.
- **Output artifact paths**: a results/evidence file (pass/fail summary,
  screenshots, manifests) under `$CW_TMP/`.
- **Write scope**: its evidence artifacts only; it may start/stop services but
  must not modify repo source.
- **Isolation**: none required against the repo; runs against the target repo.
- **Stop condition**: every scenario executed and the evidence artifact written;
  on a critical failure, stop and report instead of continuing.

### issue-authoring-worker

- **Role**: create GitHub issues/labels from a planned set.
- **Inputs**: the planned issues and labels.
- **Output artifact paths**: the created issue numbers / a summary artifact.
- **Write scope**: GitHub issues/labels via `gh` only; it must not modify repo
  files.
- **Isolation**: none required (no repo writes).
- **Stop condition**: every planned issue/label created and reported.

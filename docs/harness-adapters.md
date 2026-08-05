# Claude Code Adapter Surface

Chief Wiggum's workflow contracts are harness-neutral (see `AGENTS.md`), but the
Claude Code adapter under `.claude/commands/` invokes them through Claude-only
mechanisms. This file inventories those Claude-specific surfaces, names the
portable concept each one stands for, and marks what is Claude-only.

A non-Claude harness should map each surface to its own equivalent, or fail
clearly when it has none — never silently inherit a broken Claude assumption.

## Inventory

| Claude surface | Where | Portable concept |
|----------------|-------|------------------|
| `subagent_type: "general-purpose"` | `implement.md:166,202,238,266,294,472`; `close-epic.md:139,204,237`; `architect.md:86`; `seed.md:170`; `implement-wave.md:217`; `ux-review.md:65,83` | A **worker** with a role, inputs, output artifact paths, and a write scope (see `AGENTS.md` → worker contracts). |
| `subagent_type: "Explore"` + `thoroughness:` | `implement.md:171`; `architect.md:55`; `seed.md:43` | A **read-only explorer worker** that returns a findings artifact. |
| `model: "opus" \| "sonnet"` | `implement.md:166,202,238,266,294,472`; `architect.md:86`; `close-epic.md:139,204,237`; `implement-wave.md:217`; `ux-review.md:65,83` | A **provider role** (`config/providers.json`), not a hard-coded model name. Use roles where portability matters; the model tier is an adapter hint. |
| `isolation: "worktree"` | `implement.md:238,246,266`; `implement-wave.md:217` | A required **isolation behavior**: the worker writes only inside its own checkout, never the main checkout (enforce with `scripts/git_safety.py assert-worktree`). |
| `run_in_background` + Agent completion notifications | `implement.md:171`; `implement-wave.md:217,244` | **Asynchronous worker completion** signalled through files / a harness-neutral status, not Claude's task-notification stream (see `scripts/delegates/`). |
| `/keep-going`, `CronCreate`, `CronDelete` | `keep-going.md:7,26,35` | **Claude-only.** Session keep-alive via Claude Code cron. No portable equivalent is required; other harnesses run the loop their own way or omit it. |
| Claude-in-Chrome tools (`form_input`, `read_page`) + Artifact publishing / `artifact-design` | `ux-review.md:39,65,87` | **Claude-only** browser driving and report rendering, marked as adapter notes in place. Portable concepts: drive the product's own Playwright/browser-use harness, and emit the report as markdown (`$UX_TMP/report.md`) when no artifact surface exists. |
| Slash-command invocation (`/implement`, `/architect`, …) | all command files | The **Claude Code adapter's** invocation syntax. Portable skills are invoked by the host harness; workflow text describing portable behavior should not assume slash-command syntax. |

## Rules for keeping workflows portable

- **Describe workers by contract, not by Claude parameter.** When portability
  matters, say "launch a read-only explorer worker that writes findings to
  `$TICKET_TMP/...`", and add the `subagent_type`/`model`/`isolation` values as
  a Claude Code adapter note — not as the only description.
- **Prefer provider roles over model names.** `config/providers.json` roles
  (`reviewer`, `architecture_critic`, `design_critic`, …) are the portable
  selector; `opus`/`sonnet` are Claude tiers.
- **Express isolation and completion as behaviors.** "Work only in your
  worktree" and "signal completion by writing `<file>`" are portable; the
  Claude `isolation`/notification mechanisms are how the adapter realizes them.
- **Mark Claude-only surfaces explicitly.** `/keep-going` and the `Cron*` tools
  are Claude Code only. An unsupported harness should fail clearly (e.g. "this
  step requires Claude Code cron") rather than assume the behavior exists.

This inventory is the basis for the deeper rewrites tracked in the Harness
Generalization epic: harness-neutral worker contracts (#24) and portable skill
packaging (#25).

## Optional operator hooks CW does not ship

Some harness capabilities would help a CW mechanism but are deliberately left
as a documented, opt-in operator choice rather than a shipped default — they
are harness-specific enough that shipping them would mean re-deriving an
equivalent per harness for a marginal convenience gain.

- **Claude Code `SessionEnd` hook, for automatic Claude-layer cost ingest**
  (chief-wiggum#345). `factory_log.py ingest-claude-transcripts` already runs
  as a catch-up step inside `/implement`, `/implement-wave`, and `/reflect`
  (see `docs/ticket-cost.md`), which covers the ticket/build path without any
  hook. An operator who wants EVERY session's turns ingested the moment it
  ends — not just at the next workflow's catch-up step — can wire their own
  `SessionEnd` hook to run the same command. CW does not install this for
  three reasons: it is Claude Code-specific (the portability gate above
  exists precisely to keep harness-only mechanisms out of adapter-neutral
  prose); it would mutate the operator's own Claude Code settings, which no
  chief-wiggum script does today (see "Secrets never touch env vars" and the
  transcript-route rationale in `docs/factory-telemetry.md`); and it fires
  *after* the workflow that would have used its output has already run, so it
  adds nothing to the ticket-costing path itself. Example, for an operator who
  wants it:
  ```json
  {
    "hooks": {
      "SessionEnd": [{
        "hooks": [{
          "type": "command",
          "command": "python3 $HOME/repos/chief-wiggum/scripts/factory_log.py ingest-claude-transcripts --since-days 1"
        }]
      }]
    }
  }
  ```
  in `~/.claude/settings.json` — the operator's own file, never written by CW.

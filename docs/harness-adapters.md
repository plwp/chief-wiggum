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
| `subagent_type: "general-purpose"` | `implement.md:166,202,238,266,294,472`; `close-epic.md:139,204,237`; `architect.md:86`; `seed.md:170`; `implement-wave.md:217` | A **worker** with a role, inputs, output artifact paths, and a write scope (see `AGENTS.md` → worker contracts). |
| `subagent_type: "Explore"` + `thoroughness:` | `implement.md:171`; `architect.md:55`; `seed.md:43` | A **read-only explorer worker** that returns a findings artifact. |
| `model: "opus" \| "sonnet"` | `implement.md:166,202,238,266,294,472`; `architect.md:86`; `close-epic.md:139,204,237`; `implement-wave.md:217` | A **provider role** (`config/providers.json`), not a hard-coded model name. Use roles where portability matters; the model tier is an adapter hint. |
| `isolation: "worktree"` | `implement.md:238,246,266`; `implement-wave.md:217` | A required **isolation behavior**: the worker writes only inside its own checkout, never the main checkout (enforce with `scripts/git_safety.py assert-worktree`). |
| `run_in_background` + Agent completion notifications | `implement.md:171`; `implement-wave.md:217,244` | **Asynchronous worker completion** signalled through files / a harness-neutral status, not Claude's task-notification stream (see `scripts/delegates/`). |
| `/keep-going`, `CronCreate`, `CronDelete` | `keep-going.md:7,26,35` | **Claude-only.** Session keep-alive via Claude Code cron. No portable equivalent is required; other harnesses run the loop their own way or omit it. |
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

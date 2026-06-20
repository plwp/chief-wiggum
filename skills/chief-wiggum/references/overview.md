# Chief Wiggum — Portable Concept Model

The Chief Wiggum skill is harness-portable: the same workflow contract runs from
Claude Code, Codex, or another skill-aware harness. This reference defines the
portable concepts so a harness can map them to its own mechanisms.

> **Paths** — `scripts/`, `docs/`, and `config/` mentioned here live at the
> **Chief Wiggum checkout root**, not inside `skills/chief-wiggum/`. The skill
> installs as a symlink back into the checkout; workflows resolve the checkout
> root at runtime via `scripts/env.py home` (`$CW_HOME`).

## Concepts

- **Skill** — the umbrella entrypoint (`SKILL.md`) plus its `references/`,
  `agents/` (harness metadata), and the shared `scripts/`.
- **Reference** — a workflow body under `references/workflows/<id>.md`, loaded
  only when that stage runs (progressive disclosure). Each reference is the
  canonical workflow procedure (it resolves to the same body the Claude Code
  `.claude/commands/<id>.md` adapter uses — single source of truth).
- **Script** — a tested Python helper in `scripts/` that performs deterministic
  mechanics (context resolution, planning, verification, PR drafting, …). The
  `cw` facade lists them.
- **Provider** — an AI backend selected by **role** (`config/providers.json`),
  not by model name. Roles: `reviewer`, `architecture_critic`, `design_critic`,
  `explorer`, `implementer`, `risky_diff_review`.
- **Worker** — a delegated sub-task described by a harness-neutral contract in
  `docs/worker-contracts.md` (role, inputs, output artifact paths, write scope,
  isolation, stop condition). A harness that supports sub-agents may spawn them;
  otherwise the orchestrator runs the contract locally or via a provider adapter.

## Adapters

Claude Code-specific surfaces (sub-agent parameters, `Cron*` tools, slash-command
invocation) are inventoried in `docs/harness-adapters.md`. Harness-specific
metadata lives beside the skill (`agents/openai.yaml` for Codex) and is optional
— it never carries workflow semantics.

## Running

Workflows operate on a **target repo** (resolved/cloned via `gh`), not on the
Chief Wiggum checkout. Install paths (Claude Code command dirs, Codex skill
symlink, generic skill-aware harness) are in the top-level `README.md` and
`docs/harnesses.md`.

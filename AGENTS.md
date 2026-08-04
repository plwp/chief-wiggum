# Chief Wiggum Agent Instructions

Chief Wiggum is a harness-portable SDLC orchestration layer. Treat the workflow contract as separate from the harness used to execute it.

## Portable Core

- Workflow intent lives in issue/milestone artifacts, formal models, `templates/`, `scripts/`, and portable skills under `skills/`.
- Claude Code slash commands under `.claude/commands/` are an adapter for Claude Code, not the only source of truth.
- Provider roles live in `config/providers.json`; use role names such as `reviewer`, `architecture_critic`, `design_critic`, and `risky_diff_review` instead of hard-coding vendor/model names in new workflow code.
- Delegated workers should use the shared file protocol in `scripts/delegates/README.md` when a durable handoff is needed.

## Harness Adapters

The Claude Code adapter invokes the portable workflows through Claude-only mechanisms (sub-agent `subagent_type`/`model`/`isolation` parameters, `Cron*` tools, slash-command syntax). `docs/harness-adapters.md` inventories these surfaces with file/line references and names the portable concept each stands for — read it before assuming a behavior is portable. Claude model names map to provider roles in `config/providers.json` where portability matters; `/keep-going` and the `Cron*` tools are Claude-only.

- Claude Code: use `.claude/commands/` and `CLAUDE.md`.
- Codex or other skill-aware harnesses: install portable skills from `skills/` and use any harness-specific metadata as optional UI/config data.
- Claude interactive delegation: use `$claude-interactive-delegate` only as an optional provider configured through `config/providers.json`.

## Working Rules

- Resolve paths through `scripts/env.py` and `scripts/repo.py`; do not hardcode local checkout paths.
- Resolve a target's CW meta location (embedded vs sidecar, domain scope) through `scripts/artifacts.py` — never assume `docs/quality`; `/status` (`scripts/status.py`) shows the resolved state live. See `docs/sidecar.md`.
- Run dependency checks with the narrowest profile that matches the workflow, for example `python3 scripts/check_deps.py --for core --provider codex`.
- Treat provider output as advisory unless the workflow explicitly defines it as a generated artifact. Verify code, tests, screenshots, and repository facts locally.
- Keep new instructions harness-neutral unless they are explicitly inside a harness adapter.
- Every commit, PR body, and issue body CW generates carries an AI-authorship disclosure (`chief_wiggum.ai_disclosure`, chief-wiggum#317) — see `docs/ai-act-posture.md` for CW's own determination under the EU AI Act (a distinct subject from the products CW builds, tracked by `docs/compliance/ai-act.json` per target repo, chief-wiggum#316).

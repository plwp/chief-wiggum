# Harness Installation and Adapter Guide

Chief Wiggum has a portable workflow core and harness-specific adapters.

For agents reading the repository directly, start with `AGENTS.md`. For Claude Code sessions, `CLAUDE.md` adds adapter-specific operating instructions.

## Concepts

- **Portable workflow contract**: skills, references, scripts, templates, formal models, provider roles, and delegated-worker task protocol.
- **Harness adapter**: the mechanism a specific agent runtime uses to invoke those workflows, such as Claude Code slash commands or Codex skill metadata.
- **Provider**: an AI backend or delegate used by a workflow role, configured in `config/providers.json`.

## Claude Code

Claude Code uses the existing slash-command adapter:

```json
{
  "commandDirs": ["~/repos/chief-wiggum/.claude/commands"]
}
```

Run from the target repo:

```bash
claude /setup
claude /plan-epic owner/repo
claude /implement owner/repo#42
```

`CLAUDE.md` remains the Claude Code adapter instructions file. Keep Claude-specific session behavior there or under `.claude/commands/`.

## Portable Skills

Portable skills live under `skills/`. They use the `SKILL.md` plus `scripts/`, `references/`, and `assets/` layout where possible. Harness-specific metadata can live beside the portable skill, but should not be required to understand the core workflow.

Every portable skill should keep durable behavior in files a generic agent can read: `SKILL.md` for instructions, `references/` for protocol details, `scripts/` for executable helpers, and `assets/` for reusable artifacts. Harness metadata may improve discovery or UI, but it should not be the only place a required instruction appears.

Example Codex install:

```bash
ln -sfn ~/repos/chief-wiggum/skills/claude-interactive-delegate ~/.codex/skills/claude-interactive-delegate
```

Other skill-aware harnesses should install or symlink the same `skills/<name>` directory using their native discovery path.

If a harness does not have a skill system, use a thin adapter that loads `SKILL.md` and any referenced files, then invokes the same scripts and delegated-worker protocol. Avoid forking workflow logic into harness-specific prompt copies unless the harness syntax requires a small wrapper.

## Provider Roles

AI backends are selected by role:

```bash
python3 scripts/consult_ai.py --role reviewer prompt.md --output-dir "$CW_TMP/reviews"
```

Configure roles in `config/providers.json`. Required providers must succeed. Optional providers may be disabled or fail without blocking the role quorum.

## Dependency Profiles

Check the narrowest profile required by the active harness and workflow:

```bash
python3 scripts/check_deps.py --for core
python3 scripts/check_deps.py --for core --provider claude-code
python3 scripts/check_deps.py --for core --provider codex --provider gemini
python3 scripts/check_deps.py --for core --provider claude-interactive
python3 scripts/check_deps.py --for transcription
python3 scripts/check_deps.py --for browser-validation
```

## Claude Interactive Delegate

`$claude-interactive-delegate` drives a persistent interactive Claude Code tmux session through a file-based task handoff. It is an optional provider, not a required harness dependency.

Use it when a workflow role benefits from a Claude Code review or critique:

```yaml
providers:
  claude-interactive:
    type: delegate
    delegate: claude-interactive
    enabled: true
roles:
  risky_diff_review:
    required: [codex, gemini]
    optional: [claude-interactive]
```

Do not use terminal output as the result contract. Use the delegated-worker files documented in `scripts/delegates/README.md`.

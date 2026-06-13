---
name: claude-interactive-delegate
description: Delegate bounded tasks from Codex to a persistent interactive Claude Code terminal session using tmux/PTY instead of claude -p. Use when Codex should ask Claude Code to act as an optional reviewer, architecture critic, design critic, implementation advisor, or Chief Wiggum provider through an interactive session with file-based task handoff and completion sentinels.
---

# Claude Interactive Delegate

## Overview

Use this skill to drive an existing or newly started interactive Claude Code session as a delegated worker. Codex sends the task through a real terminal session, Claude writes the durable result to files, and Codex reads those files back into the main workflow.

This skill is for bounded delegation, not for replacing Codex's local verification. Codex remains responsible for deciding whether the delegated output is useful and for independently checking any code, tests, or claims before shipping.

## Requirements

- `claude` must be installed and logged in for interactive Claude Code use.
- `tmux` must be installed so the Claude session persists independently of the current shell.
- The delegated task must be expressible as a prompt file and have a clear output file.

Do not use `claude -p` for this workflow. The driver starts or reuses an interactive `claude` session in tmux.

## Quick Start

Start or verify the delegate session. By default, the session command is `claude --dangerously-skip-permissions`; set `CLAUDE_DELEGATE_CMD` or pass `--claude-cmd` to use a different mode such as `claude --permission-mode auto`.

```bash
python3 ~/.codex/skills/claude-interactive-delegate/scripts/claude_delegate.py start
python3 ~/.codex/skills/claude-interactive-delegate/scripts/claude_delegate.py status
```

Delegate a prompt file:

```bash
python3 ~/.codex/skills/claude-interactive-delegate/scripts/claude_delegate.py submit \
  --prompt-file /path/to/prompt.md \
  --cwd /path/to/target/repo \
  --wait
```

Read the returned `result.md` path from the command output, then evaluate it before acting on it.

## Workflow

1. Create a bounded task prompt.
   - State the role: reviewer, architecture critic, design critic, implementation advisor, etc.
   - Include exact files, diffs, screenshots, or artifact paths Claude should inspect.
   - Ask for a concrete artifact, usually findings or a recommendation, not open-ended exploration.

2. Submit the task with `scripts/claude_delegate.py submit`.
   - Use `--cwd` when Claude should work from a target repo.
   - Use `--task-id` only when a stable ID is useful for later lookup.
   - Use `--wait` for short review tasks; omit it for long-running work and poll with `wait`.

3. Monitor status.
   - Use `status` to check whether the tmux session exists.
   - Use `capture` to inspect the latest terminal pane if the task appears stuck.
   - Use `wait --task-id <id>` to wait for `DONE` or `ERROR`.

4. Consume the result.
   - Read `result.md` only after `DONE` exists.
   - If `ERROR` exists, inspect it and decide whether to retry with a narrower prompt.
   - Treat Claude's answer as advisory input. Run local verification for code, tests, screenshots, and repository facts.

## Task Directory Contract

The driver creates task directories under `~/.chief-wiggum/delegates/claude/` by default:

```text
task-id/
├── prompt.md       # task prompt written by Codex
├── result.md       # final answer written by Claude
├── DONE            # completion sentinel written by Claude
└── ERROR           # blocked/error sentinel written by Claude
```

Codex should never parse terminal output as the result contract. Terminal capture is for debugging only.

For full protocol details, read `references/protocol.md`.

## Boundaries

- Do not automate account login, billing changes, subscription changes, API-credit consent, or payment prompts. If the interactive session reaches one of these states, stop and surface it to the user.
- Do not let the delegate create or merge PRs unless the current workflow explicitly requires it and Codex will verify the outcome.
- Do not trust self-reported test results. Codex must run or inspect verification independently.
- Do not leave completed delegate sessions open indefinitely if they are no longer needed.
- Use `scripts/claude_delegate.py stop` before changing the delegate permission mode, then start a fresh session.

## Chief Wiggum Provider Use

When wiring this into Chief Wiggum, treat it as an optional provider:

```yaml
providers:
  claude-interactive:
    enabled: true
    session: cw-claude
    task_root: ~/.chief-wiggum/delegates/claude
    roles:
      - architecture_critic
      - design_critic
      - risky_diff_review
```

Use it for high-value critique and synthesis, not as a required path for every ticket. The workflow should proceed when optional Claude delegation is unavailable unless the user explicitly requested Claude review.

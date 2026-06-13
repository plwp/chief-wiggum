# Claude Interactive Delegate Protocol

## Session Model

The delegate is a named tmux session running the normal interactive Claude Code CLI:

```bash
tmux new-session -d -s cw-claude 'claude --dangerously-skip-permissions'
```

The driver uses `tmux load-buffer`, `tmux paste-buffer`, and `tmux send-keys Enter` to send prompts into the session. This keeps the session interactive while avoiding `claude -p`.

Set `CLAUDE_DELEGATE_CMD` or pass `--claude-cmd` to use another interactive mode, for example `claude --permission-mode auto`.

## Task Handoff

Each delegated task has a unique directory:

```text
~/.chief-wiggum/delegates/claude/<task-id>/
├── prompt.md
├── result.md
├── DONE
└── ERROR
```

Codex writes `prompt.md`. Claude must write either:

- `result.md` and `DONE`, or
- `ERROR` with a short reason.

Terminal output is not the task API. Use `capture` only to diagnose a stuck session.

## Prompt Shape

A good delegated prompt includes:

- Role: reviewer, critic, implementation advisor, or synthesizer.
- Inputs: exact repo path, files, diffs, screenshots, logs, or artifact paths.
- Required output format.
- Clear stop condition.
- Instruction to write `result.md` and touch `DONE`.

Keep prompts bounded. If the task needs broad exploration, ask for findings and recommended next steps rather than asking Claude to own the whole delivery.

## Blocking Conditions

The delegate should be treated as blocked if the session requires:

- Login or account selection.
- Billing, subscription, payment, or API-credit consent.
- Tool permission decisions that were not preconfigured for the session.
- Interactive clarification that cannot be answered from the task prompt.

In these cases, Codex should inspect `capture` output, report the issue to the user, and avoid faking an answer.

## Verification

Claude delegate output is advisory. Codex must independently verify:

- Tests and lint results.
- File paths and line references.
- Screenshots or rendered UI claims.
- Security, billing, and account-state claims.
- Any recommended code change before applying it.

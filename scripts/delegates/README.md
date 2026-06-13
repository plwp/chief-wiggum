# Delegated Worker Task Protocol

Chief Wiggum delegates bounded work to providers through a shared file-based task contract. Use this protocol for harness subagents, subprocess reviewers, interactive terminal delegates, or future provider adapters.

## Task Directory

Each task lives under a provider-specific root:

```text
~/.chief-wiggum/delegates/<provider>/<task-id>/
├── prompt.md       # input prompt written by the orchestrator
├── result.md       # final answer written by the worker
├── DONE            # success sentinel
├── ERROR           # blocked/error sentinel with a concise reason
├── worker.log      # optional provider log
└── metadata.json   # optional structured metadata
```

The orchestrator reads `result.md` only after `DONE` exists. If `ERROR` exists, the orchestrator treats the task as blocked and decides whether to retry with a narrower prompt.

Terminal output is diagnostic only. Do not parse terminal UI or stdout as the primary result contract when a provider can write files.

## Worker Prompt Requirements

Worker prompts should include:

- role and stop condition
- exact input paths or artifacts
- required output format
- `result.md` path
- `DONE` and `ERROR` sentinel paths
- boundaries such as no PR creation or no billing/account consent

## Verification

Worker results are advisory unless the workflow explicitly defines them as generated artifacts. The orchestrator must independently verify tests, lint, screenshots, file references, and repository state.

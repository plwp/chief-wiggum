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
├── worker.log      # diagnostic JSONL written by the trusted adapter
└── metadata.json   # schema-valid structured execution metadata
```

The orchestrator reads `result.md` only after `DONE` exists. If `ERROR` exists, the orchestrator treats the task as blocked and decides whether to retry with a narrower prompt.

`DONE` and `ERROR` are mutually exclusive terminal commit markers. Trusted
adapters publish artifacts with same-directory temporary files and place the
terminal marker last while holding the task's terminal lock. Readers treat
both markers as a protocol conflict; they never prefer success. Artifact files
are owner-only and metadata binds the prompt/result/log/config with SHA-256.

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

## Agentic OpenRouter worker

`openrouter_worker.py` is an execution adapter, not an extension of the
prompt-only OpenRouter consultation path. It runs Codex's open-source agent
loop against a configured Responses endpoint in an explicit non-main git
worktree. Execution providers are disabled by default and are not members of
workflow roles; a bounded experiment must opt in with `--admit-disabled`.

Create the task first, then run a read-only compatibility probe before any
write-enabled experiment:

```bash
python3 scripts/delegates/openrouter_worker.py probe \
  --provider openrouter-preview-worker \
  --task-root "$CW_TMP/delegates" --task-id "$TASK_ID" \
  --worktree "$WORKTREE" --main "$TARGET_REPO" \
  --admit-disabled --timeout-seconds 120
```

The prompt must name a harmless repository read and an expected shell-tool
observation. A successful probe still does not authorize writes: promotion is
read-only probe → disposable write fixture → bounded shadow ticket → explicit
routing/experiment admission.

Authentication is fetched by the trusted parent from the system keyring. A
private helper can consume a random local capability once; it cannot read the
keyring, the broker disappears after pre-turn authentication, and the adapter
actively verifies that the helper cannot authenticate again. A failed canary
rejects the run as `CREDENTIAL_BOUNDARY_UNSAFE`.

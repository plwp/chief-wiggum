---
name: chief-wiggum
description: Harness-portable agentic SDLC orchestration. Routes to the design, plan-epic, architect, implement, implement-wave, and close-epic workflows that turn vague tickets into a disciplined delivery loop — requirements capture, epic planning, architecture with executable contracts, test-first implementation, structured multi-AI review, and PR-ready output. Use when an orchestrator (Claude Code, Codex, or another skill-aware harness) should run any stage of the SDLC loop against a target repo.
---

# Chief Wiggum

Portable SDLC orchestration. The workflow "brains" are reference files loaded
on demand (progressive disclosure); the deterministic mechanics are tested
Python helpers under `scripts/`; AI backends are selected by **provider role**,
not hard-coded model names. Sub-tasks are run by **workers** described by
harness-neutral contracts (`docs/worker-contracts.md`).

Run workflows from the **target repo**, not from the Chief Wiggum checkout.

## Workflows

Load the reference for the stage you need from `references/workflows/`:

- **`design`** — product design: divergent rendered mockups → human choice → `docs/design/` tokens. Runs once per product.
- **`plan-epic`** — group issues into an epic with a dependency graph.
- **`architect`** — define contracts, invariants, state machines, and integration tests before implementation.
- **`implement`** — TDD implementation loop for one ticket (consult → test-first → implement → review → verify → ship).
- **`implement-wave`** — parallel implementation of an epic in dependency-ordered waves.
- **`close-epic`** — epic-level quality gate.

Supporting: `seed`, `create-issue`, `ship`, `transcribe`, `setup`, `update`, `stitch-audit` (also under `references/workflows/`).

## Conventions

- **Provider roles** (`config/providers.json`): `reviewer`, `architecture_critic`, `design_critic`, … — not model names.
- **Workers**: each delegated sub-task names a contract in `docs/worker-contracts.md` (role, inputs, output paths, write scope, isolation, stop condition).
- **Scripts** are Python helpers in `scripts/` (a `cw` facade lists them: `python3 scripts/cw.py`).
- **Harness adapters**: Claude Code-specific surfaces are inventoried in `docs/harness-adapters.md`; harness metadata lives beside the skill (e.g. `agents/openai.yaml`), never in this core contract.

See `references/overview.md` for the portable concept model, and `docs/harnesses.md` for install/adapter details.

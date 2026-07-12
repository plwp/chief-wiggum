# Apply Pattern — Install a Registry Pattern's Contract Pack

Installs a specified registry pattern's **invariant cluster** (its contract pack) into a target repo, so `/architect` can fold the connected requirements in by stable id instead of re-deriving them. See `docs/patterns-registry.md`.

This installs the **contract pack** (invariant cluster + adoption record + protected-path registration), not code — scaffold stamping is deferred until patterns ship a `scaffold/`.

## When to use

- During product/epic design, once you've chosen which registry patterns a product realizes (the `/seed` "select" moment).
- Before `/architect`, so the epic's `invariants.md` is folded from a real cluster rather than invented.

## Usage

```
/apply-pattern <owner/repo> --pattern <id> [--param k=v]... [--dry-run]
```

## Parameters

- `owner/repo`: Target GitHub repository (resolved + cloned via `repo.py`).
- `--pattern <id>`: A `status: specified` pattern id from `patterns/registry.json`.
- `--param k=v`: Bind a pattern parameter (repeatable). Unbound **required** params are written as `TBD:` markers.
- `--dry-run`: Print the plan without writing.

## Autonomy

Run to completion. The one judgement call is **parameter binding** — resolve each parameter from the target repo's context where you can (its billing vendor, its tenant key, its build command); leave the rest as `TBD:` for a human. Never guess a required parameter — a `TBD:` that the unresolved gate catches is correct; a wrong guess is not.

---

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 2: Pick the pattern and inspect its cluster

List specified patterns and read the chosen one's manifest so you know its parameters and invariant cluster:

```bash
python3 -c "import json; d=json.load(open('$CW_HOME/patterns/registry.json')); [print(p['id'], '—', p.get('invariants','')) for p in d['patterns']]"
cat "$CW_HOME/patterns/<id>/manifest.json"
```

If the id is a **candidate** (not specified), stop: it has no manifest to install. Offer to specify it first.

### Step 3: Bind parameters from the target repo

Read `manifest.json`'s `parameters`. For each, resolve a value from the **target repo's** context (grep its config, its stack, its build commands). Assemble `--param k=v` flags for what you can confidently bind; leave required params you can't confirm unbound (they become `TBD:`).

### Step 4: Install

```bash
python3 "$CW_HOME/scripts/apply_pattern.py" <id> \
  --target-dir "$TARGET_REPO" \
  --param resource=subscription --param projected_field=plan   # example
```

Dry-run first (`--dry-run`) to preview. This writes, in the target repo:
- `docs/patterns/<id>/invariants.md` — the cluster as a stable-id contract pack.
- `docs/patterns/adopted.json` — the adoption record (id, version, bound params, provenance, cluster ids).
- registers `docs/patterns/**` into `docs/quality/ratchet.json` protected paths.

### Step 5: Verify and hand off

Confirm the unresolved gate sees any `TBD:` markers (so dependent work can't build on a guess):

```bash
python3 "$CW_HOME/scripts/check_unresolved.py" "$TARGET_REPO/docs/patterns/<id>/invariants.md"
```

Then report the adopted cluster and point the user at the next step: **`/architect`** folds this cluster into the epic's `invariants.md` by stable id, and the traceability / single-writer / ratchet gates hold it from there.

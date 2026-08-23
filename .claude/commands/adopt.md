# Adopt — Brownfield Entry for Repos CW Didn't Build

The missing entry arrow: survey the repo's shape, elect a footprint, baseline the ratchet with a REAL test run, baseline the debt inventory, grandfather the pre-existing findings (with expiry), and write the adoption record — the switch `/architect` and the scope-discipline machinery (#216) read. Deliberately lightweight: **it does not infer contracts** (see the deferral below). Full semantics: `docs/adopt.md`.

## When to use

- Pointing chief-wiggum at an existing codebase for the first time (legacy, client, or imported code)
- Re-baselining is NOT this command — the adoption baseline is recorded once; later movement is the ratchet's business

## Usage

```
/adopt owner/repo                       # full sequence, defaults (sidecar, whole-repo scope)
/adopt owner/repo --mode embedded       # embed the meta in the target tree instead
/adopt owner/repo --scope-from-codeowners
```

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# Pin the interpreter CW scripts run under. A bare `python3` is whatever
# the shell resolves, so a Homebrew bump silently strands keyring /
# jsonschema / google-genai and kills consults mid-phase (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
```

### Step 2: Run the adoption sequence

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" run "$owner_repo"
# or step by step (same defaults; elect FIRST — the election decides where
# the survey and every later artifact persist):
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" elect "$owner_repo" --mode sidecar [--scope-from-codeowners]
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" survey "$owner_repo"
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" baseline "$owner_repo"
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" grandfather "$owner_repo" [--expiry-days 90] [--owner NAME]
"${CW_PY:-python3}" "$CW_HOME/scripts/adopt.py" record "$owner_repo"
```

What each step writes (all under the #213-resolved meta root — in sidecar mode the target tree gains **nothing**):

| Step | Writes | Content |
|------|--------|---------|
| `survey` | `<meta root>/adoption/survey.json` | age, size, languages, test presence, a REAL coverage-baseline attempt (the detected test command actually runs; no runner detected is said, never papered over), CI presence, and a per-gate applicability verdict (applicable / report-only / inapplicable) for the 7 shipped gates + the debt inventory |
| `elect` | `~/.chief-wiggum/meta/<id>/election.json` (+ optional `scope.json`) | footprint mode (default: sidecar) + domain scope (default: whole repo; `--scope-from-codeowners` seeds it from CODEOWNERS when present, skips with a note when absent) |
| `baseline` | ratchet state + `debt.json` in the resolver quality dir | `ratchet init` + a **real scored test run** (never `--no-tests`) journaled as a merged `baseline` record — from day one, debt cannot grow; the #214 debt inventory as the adoption baseline |
| `grandfather` | `<meta root>/adoption/grandfathered.json` | every baseline finding as a JUSTIFIED-waiver-shaped entry (`reason`/`owner`/`expiry`, default +90d) — grandfathered findings stay IN the inventory, labeled; only NEW findings are gate-eligible; expiry is visible pressure, not amnesty |
| `record` | `<meta root>/adoption/adoption.json` | the adoption record: `brownfield: true`, mode, scope, survey verdicts, baseline refs, grandfather ref — the switch #216 and `/architect` read |

### Step 3: Verify and present

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/status.py" "$owner_repo"
```

`/status` now shows the Adoption section (brownfield flag, grandfather counts, nearest expiry, EXPIRED warnings). Relay the survey verdicts and the baseline outcome honestly — if the test run failed because dependencies are missing, that IS the baseline; say so.

Next steps from here: `/architect` per feature epic (contracts arrive organically, epic by epic), or `/plan-epic --from-debt` (#216).

**Contract inference is deferred — do not attempt it here.** Trigger: the first cross-team breakage at a domain seam on an adopted repo, or an explicit operator request. When it fires: start at **domain-boundary contracts** (what our scope exposes to / consumes from the rest of the repo) — the smallest, highest-value, already-implicitly-agreed surface. Never attempt whole-repo inference; inferred contracts can canonize existing bugs as intended behavior.

No pausing between steps; surface genuine blockers (e.g. the target isn't a git repo) instead of guessing.

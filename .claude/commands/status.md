# Status — Live Chief-Wiggum State for a Target

One screen, derived live at call time — never a hand-maintained doc: footprint mode + meta root, domain-scope summary, gate ledger (validation verdict + journaled wired state per gate), ratchet high-water state, adopted patterns, and debt counts by severity once the inventory exists. The operator-facing sibling of `code_query orient`.

## When to use

- Orienting on a target before `/architect`, `/implement-wave`, or `/close-epic`
- Checking which gates are currently validated/wired (blocking) on a repo
- Verifying a footprint-mode election (embedded vs sidecar) landed where expected

## Usage

```
/status [owner/repo]
/status --repo <path>
/status                     # defaults to the current repo
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

### Step 2: Run the status script

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/status.py" "$owner_repo"          # owner/repo (resolved via repo.py)
# or, for a local path:
"${CW_PY:-python3}" "$CW_HOME/scripts/status.py" --repo "$TARGET_REPO"
# machine-readable:
"${CW_PY:-python3}" "$CW_HOME/scripts/status.py" --repo "$TARGET_REPO" --format json
```

The script derives everything live from the target's resolved meta locations (`scripts/artifacts.py`) and **never writes anything**. Gate verdicts come from `check_gate_validation.check` (never the record's own status field); wired state comes from the tamper-evident ratchet journal's gate-authority events.

### Step 3: Present the output

Relay the screen as-is (it is already one screen). Flag anything actionable plainly:

- A gate showing `failing` while `wired (blocking)` is a stale-while-blocking demotion candidate — point at `check_gate_validation.py <gate>` for the detail.
- `no ratchet config` on a repo expected to be under the ratchet means `ratchet.py init` was never run.
- `PARTIAL COVERAGE: N high-water case(s) quarantined` means coverage is deliberately below the high-water mark — check the nearest expiry. A `WARNING: … EXPIRED quarantine(s)` line means those cases are blocking again (#278; see `docs/ratchet.md`).
- An unexpected `sidecar`/`embedded` mode means the election (`artifacts.py elect`) doesn't match expectations — surface it, don't change it.

No pausing, no checkpoints — this is a read-only report.

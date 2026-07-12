# Reflect — Factory Self-Assessment from a Built Repo

Looks at a repo the CW factory built, mines the evidence CW leaves behind (git + PR history, the ratchet journal, TBD markers, adopted-pattern records, retrospectives), and reasons about **how well the factory served it** — which gates add value, what slipped through, how well assumptions got filled — then drafts improvement issues **in the CW repo**.

This is the improvement-loop pattern turned back on the factory. Every signal here is trusted (it's CW's own output), so this runs autonomously to a proposed issue set — but **filing issues is a human checkpoint** (see Autonomy).

## When to use

- After an epic or a few epics land in a product repo — reflect on how the factory performed.
- Periodically, to keep a CW-improvement backlog grounded in real production evidence rather than intuition.
- Point it at CW's own repo too (dogfood): CW should be held to the standards it imposes.

## Usage

```
/reflect <owner/repo> [--commits N] [--create-issues]
```

## Parameters

- `owner/repo`: The built repo to reflect on (resolved via `repo.py`). Use `plwp/chief-wiggum` to reflect on the factory itself.
- `--commits N`: How far back to scan (default 400).
- `--create-issues`: File the confirmed issues without a second confirmation (default: draft + confirm).

## Autonomy

Run the analysis to completion autonomously. **Filing issues is the one checkpoint** — present the drafted issues and get confirmation before `gh issue create`, because they're outward-facing writes to the CW repo. Never file a vague issue; every issue must cite the specific evidence (commit, marker, journal record) that motivates it.

---

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 2: Collect the evidence

Fetch merged-PR history (review outcomes + bodies carry gate signal) and run the miner:

```bash
gh --repo "$owner_repo" pr list --state merged --limit 200 \
  --json number,title,body,reviewDecision,labels > "$CW_TMP/prs.json" 2>/dev/null || echo '[]' > "$CW_TMP/prs.json"
python3 "$CW_HOME/scripts/reflect.py" "$TARGET_REPO" --prs "$CW_TMP/prs.json" --commits "${commits:-400}" --format json > "$CW_TMP/reflection.json"
```

The report has: `commit_kinds`, `gate_mentions`, `force_bypasses`, `slippage_commits`, `assumptions` (TBD markers), `ratchet` health, `pattern_coverage`, `retrospectives`, and mechanical `findings` seeding your analysis.

### Step 3: Reason across the dimensions

The mechanical findings are a starting point, not the analysis. Reason over the evidence:

- **Which gates add value?** A gate that appears in the record alongside a fix (it caught something) is earning its keep. A gate with **zero mentions** across many commits/PRs is either never firing here or its value is invisible — worth logging. **This is where the factory-telemetry log (`docs/quality/factory-log.jsonl`, when present) is authoritative** over git archaeology — prefer it for gate value/noise/token-cost.
- **Which are noise?** `--force`/`--no-verify` bypasses, or a gate repeatedly fixed with trivial/annotation-only commits, signal a gate that's noisy on real code — the operator is routing around it, which erodes trust in *every* gate (see `docs/gate-rollout.md`).
- **What slipped through?** For each `fix(`/`revert`/`hotfix` commit, ask: *what class of bug is this, and should a gate have caught it?* A slippage with no covering gate is a candidate for a **new** gate or a strengthened contract. Read the actual diff of the top slippage commits to classify them.
- **How well are assumptions filled?** Unresolved `TBD:` markers still in `docs/` mean the factory guessed and never confirmed. Adopted patterns with `missing` invariants (promised but never folded into an epic) mean the contract pack didn't fully land.
- **Factory-log health.** Forced ratchet merges = the bar was lowered under override. Retrospective "what the loop caught" vs "deferred/not done" sections are first-person factory signal — read them.

### Step 4: Draft CW-improvement issues

For each substantiated finding, draft an issue **for the CW repo** (not the target). Each must have:
- A specific title (the improvement, not the symptom).
- A body citing the **evidence** (commit hashes/subjects, marker locations, journal records, PR numbers) and the **dimension** (gate-value / slippage / assumption-fill / pattern-coverage).
- A concrete proposal (new gate, strengthen an existing gate, report-only→blocking promotion, a pattern to specify, a telemetry point to add).
- Appropriate labels (`enhancement`, `gate`, `reflection`).

Prefer a few high-signal issues over many speculative ones. Group related slippage into one "gap" issue.

### Step 5: Confirm and file

Present the drafted issues (titles + one-line rationale). On confirmation (or `--create-issues`):

```bash
gh --repo "$(python3 "$CW_HOME/scripts/repo.py" home >/dev/null; echo plwp/chief-wiggum)" \
  issue create --title "<title>" --body-file "$CW_TMP/issue-<n>.md" --label reflection --label enhancement
```

Report the filed issue URLs and a one-paragraph factory-health summary (what's working, what's slipping, the single highest-leverage improvement).

### Step 6: Record validation telemetry

`/reflect` is itself a validation — record its value (findings) so the cost↔value verdict can judge it. After filing, emit one gate event with the finding count (no-op unless telemetry is enabled; never blocks):

```bash
python3 "$CW_HOME/scripts/factory_log.py" emit --event gate --name reflect \
  --result "$([ "$n_findings" -gt 0 ] && echo fail || echo pass)" --caught "$n_findings" --repo "$owner_repo"
```

This is the convention every LLM validation should follow (see `docs/factory-telemetry.md`): after producing findings, `factory_log.py emit --event gate --name <validation> --caught <N> --repo <owner/repo>`. Its **cost** already flows (consults + Claude Code OTEL, attributed by skill name); this supplies the **value** side (`caught`) so `aggregate`'s verdict can rate the validation `earning` vs `demote-candidate`. See [chief-wiggum#143](https://github.com/plwp/chief-wiggum/issues/143).

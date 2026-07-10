# Code Metrics — Literature-Grounded Code-Quality Analysis

Runs a battery of code-quality metric engines over a repo and produces a consolidated, **cited** report: churn/attribution, cyclomatic + cognitive complexity, maintainability index, history trend, code survival (2-week churn), production duplication, and process metrics (change coupling, entropy, ownership/bus-factor, fix ratio).

Every metric is reported **with its caveat**. The load-bearing truth from the literature is that most product metrics are *size in disguise* — the signals that are **not** just SLOC (relative churn, duplication, code survival) are the trustworthy ones. This skill grades output the way the research says to, not by vibes.

## When to use

- Auditing what an AI/agentic pipeline actually produced (the primary use case)
- Comparing a repo's churn/duplication against the GitClear pre-AI vs AI-assisted baselines
- Periodic health check of a codebase's complexity/test-discipline trend over time
- Prioritising refactoring targets (churn × complexity hotspots)

## Usage

```
/code-metrics [owner/repo]
/code-metrics --repo <path>
/code-metrics                       # defaults to the current git repo
```

Flags (passed through to `scripts/quality_metrics.py`):

- `--repo <path>`: analyse a direct local path instead of `owner/repo`
- `--out <dir>`: output directory (defaults to a session temp dir)
- `--top <N>`: number of churn hotspots to keep (default 25)
- `--trend-n <N>`: history sample points for the trend engine (default 10)
- `--skip-trend`: skip history-sampling trend (fastest; avoids worktree churn)
- `--skip-survival`: skip git-of-theseus code-survival analysis
- `--skip-duplication`: skip jscpd duplication analysis
- `--venv <venv>` / `--gobin <dir>`: point complexity engines at a specific toolchain

## Autonomy

**Run to completion without pausing.** This is an analysis skill — no human-in-the-loop checkpoints. Present the final report and let the user decide what to act on. Optional engines (survival, duplication) self-skip when their external tool is absent; report the skip honestly rather than pretending the number is zero.

---

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
```

If given `owner/repo`, resolve it (clones/pulls into the cache); otherwise the orchestrator defaults to the current repo or uses `--repo`:

```bash
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")   # only for owner/repo
```

### Step 2: Run the metric battery

```bash
python3 "$CW_HOME/scripts/quality_metrics.py" "$owner_repo" --out "$CW_TMP/metrics"
# or, for a local path:
python3 "$CW_HOME/scripts/quality_metrics.py" --repo "$TARGET_REPO" --out "$CW_TMP/metrics"
```

The orchestrator runs each engine, writes per-engine JSON (`churn.json`, `complexity.json`, `trend.json`, `survival.json`, `process.json`, `duplication.json`), builds `combined.json`, renders PNG charts, and writes `report.md`. It prints the `report.md` path on stdout.

Always-run engines (pure Python + git + lizard): **churn, complexity, trend, process**. Optional engines run only when their tool is present: **survival** (git-of-theseus), **duplication** (jscpd/node). A missing tool yields `{"skipped": ...}` in that engine's JSON — the run never crashes.

For a fast pass on a large repo, add `--skip-trend --skip-survival --skip-duplication`.

### Step 3: Read the generated report

Read `$CW_TMP/metrics/report.md`. Cross-reference against `combined.json` for the flattened summary. Note which engines were skipped and why (tool absent vs `--skip-*`).

### Step 4: Present a concise findings summary

Summarise for the user, leading with the trustworthy signals and stating caveats plainly:

- **Scale & discipline**: LOC, test:code ratio, conventional-commit / ticket-ref / PR-merge %.
- **Churn (strongest signal)**: rework ratio (del:add), churn per commit, top hotspots. This is where AI-generated code degrades most clearly — but only *relative/normalised* churn is predictive.
- **Complexity**: mean/max cyclomatic, % functions CCN>10, % long methods, cognitive max — **always read alongside SLOC** (CC is ~0.7–0.9 collinear with size at file level). MI is directional only.
- **Process metrics**: change entropy, bus-factor (with the agentic-repo caveat), commit-size distribution, fix-commit %.
- **AI-slop signals vs GitClear**: 14-day survival and production duplication against the pre-AI (96.9% / 8.3%) and AI-assisted (94.3% / 12.3%) bands — noting GitClear is a **vendor** series (direction credible, multiples framing-dependent).

Attach or reference the rendered charts (`trends.png`, `ai_slop_signals.png`) if present. Be explicit about what was skipped. Do not over-interpret a single high metric; the honest verdict is a weighted read across signals, weighted toward the non-size-confounded ones.

---

## Metrics & literature

Full citations and thresholds live in `docs/quality-metrics.md`. Summary of each metric, its source, and its caveat:

- **Cyclomatic complexity** (McCabe 1976; NIST SP 500-235): >10 moderate / >15 high / >20 very high. **Caveat:** ~0.7–0.9 collinear with SLOC at file level (Jay 2009; Herraiz & Hassan 2010) — a "high CC" file may just be big; softer at method level. Report CC *alongside* size.
- **Cognitive complexity** (Campbell/SonarSource 2018; Muñoz Barón 2020): proxies comprehension **effort/time** (r≈0.54), **not correctness** (r≈−0.13, not significant). **Caveat:** no validated threshold; essentially no evidence above ~15.
- **Maintainability Index** (Oman & Hagemeister 1992; van Deursen 2014): radon 0–100, <65 flagged. **Caveat:** coefficients fit once on a tiny 1980s HP dataset, never recalibrated — **directional only**, never an authoritative score.
- **Code churn** (Nagappan & Ball 2005): **the strongest, best-replicated defect/AI-slop signal** (relative churn R²≈0.8). **Caveat:** *relative/normalised* churn predicts; absolute churn is weak.
- **Hotspots = churn × complexity** (Tornhill; Tornhill & Borg 2022): the right *prioritisation* lens. **Caveat:** the 15×/124× impact multiples are **vendor-derived** (CodeScene), not independently replicated.
- **Code survival / 2-week churn** (GitClear 2024–2025; DORA 2024): 14-day survival vs GitClear pre-AI 96.9% / AI 94.3% (5.7% churn). **Caveat:** GitClear is a **vendor** series — direction corroborated by DORA, exact figures framing-dependent.
- **Production duplication** (GitClear): % duplicated tokens vs pre-AI 8.3% / AI 12.3% bands, tests excluded. **Caveat:** vendor bands; jscpd token % is not identical to GitClear's block metric — compare direction, not decimals.
- **Process metrics** (Rahman & Devanbu 2013; Hassan 2009; Bird et al. 2011): change coupling, change entropy (HCM), ownership/bus-factor, commit size, fix ratio. **Process metrics outperform product metrics** for defect prediction and are more stable across releases. **Caveat:** bus-factor/ownership assume human authorship — in an agentic repo the author collapses to one operator identity, so read bus-factor as a signal about the attribution model, not team resilience.

**The load-bearing caveat:** most of these metrics are size in disguise. If a repo differs from a baseline mainly in SLOC, much of the CC/MI/Halstead delta follows mechanically. The findings that are **not** just size — relative churn, duplication/copy-paste ratios, and survival — are the trustworthy process signals.

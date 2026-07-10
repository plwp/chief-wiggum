# Code-Quality Metrics: what we compute, what it means, and the honest caveats

The `/code-metrics` skill (`scripts/quality_metrics.py` + the `scripts/quality/`
package) computes a battery of code-quality metrics over a repo and emits a cited
report. This doc is the reference: for each metric, **what it measures**, the
**thresholds** we use, the **evidence strength**, and the **caveat** that keeps
the number honest.

**The load-bearing caveat for the whole analysis:** most product metrics are
**size in disguise**. If a repo differs from a baseline mainly in SLOC, much of
the cyclomatic / MI / Halstead delta follows mechanically. The findings that are
*not* just size — **relative churn, duplication/copy-paste ratios, and code
survival** — are the trustworthy process signals. Always report size as a
covariate.

**Evidence-strength convention:** `[REPLICATED]` confirmed across independent
datasets; `[SINGLE STUDY]` one strong study, not yet replicated; `[VENDOR]`
self-published by a party with commercial interest — direction may be
informative, treat multiples skeptically.

---

## 1. Cyclomatic complexity (McCabe)

**Measures** the number of linearly independent paths through a function's
control-flow graph (decision points + 1). Computed by `lizard` across
Python/Go/TS/JS. We report the distribution: mean, max, p90/p95, and % of
functions over 10 / 15 / 20, plus function length (% > 60 lines).

**Thresholds** — McCabe's own (1976) >10 = split candidate; NIST SP 500-235
(1996) endorsed 10 with a 15 caveat. The 1–10 / 11–20 / 21–50 / >50
"simple/moderate/high/untestable" bands are an SEI C4 *characterization*, not an
empirical result.

**Caveat** `[REPLICATED]` — cyclomatic is **~0.7–0.9 collinear with SLOC** at
file/module level (Jay et al. 2009, >1.2M files; Herraiz & Hassan 2010, Spearman
≈0.72; Shepperd 1988). A "high CC" file may just be a big file. It is *softer* at
method level, so CC is not fully redundant there — but never present it as
independent of size. It also does not generalize as a cross-project defect
predictor (Nagappan, Ball & Zeller 2006).

> McCabe (1976), *IEEE TSE* SE-2(4). · NIST SP 500-235 (Watson & McCabe 1996).
> Jay et al. (2009). · Herraiz & Hassan (2010), *Making Software*. · Shepperd (1988).

## 2. Cognitive complexity (Campbell / SonarSource)

**Measures** understandability, not testability: ignores the method-entry +1,
counts `switch`+cases as one, and adds a **nesting penalty**. Computed by
`gocognit` (Go) and `complexipy` (Python); TS has no maintained CLI (n/a).

**Validation** `[SINGLE meta-study]` — Muñoz Barón, Wyrich & Wagner (ESEM 2020,
Best Paper) pooled 10 datasets: cognitive complexity correlates with
comprehension **time** (r≈**+0.54**) and perceived difficulty (r≈−0.29), but
**not** with correctness of understanding (r≈−0.13, CI crosses 0) and **not**
with physiological load.

**Caveat** — it is a proxy for comprehension **effort/time, not correctness**.
There is **no validated threshold** and essentially no evidence about behavior
above ~15 (only 2 of 10 datasets had snippets that high). Do not over-interpret
high values.

> Campbell (2018), SonarSource white paper + TechDebt'18. · Muñoz Barón et al. (2020),
> *ESEM*, DOI 10.1145/3382494.3410636.

## 3. Maintainability Index (Oman & Hagemeister)

**Measures** a three-metric polynomial `MI = 171 − 5.2·ln(V) − 0.23·G −
16.2·ln(LOC)`. Computed by `radon` (Python only), 0–100 scale, <65 flagged.

**Caveat** `[strong critique]` — **report directionally only, never as an
authoritative score.** Coefficients were fit **once** on a tiny ~35-year-old HP
C/Pascal dataset and **never recalibrated** (van Deursen 2014). Thresholds are
self-described "rules of thumb." Sjøberg et al. (2013) found plain file size
predicts real maintenance effort at least as well. Even radon's own docs call it
"very experimental." Show MI's raw inputs (LOC, CC, Halstead V) beside it.

> van Deursen (2014), "Think Twice Before Using the Maintainability Index." ·
> Sjøberg et al. (2013), *IEEE TSE* 39(8), DOI 10.1109/TSE.2012.89.

## 4. Code churn

**Measures** volume/rate of lines added/deleted over history (from `git log`
diffs). We report absolute added/deleted, net, del:add **rework ratio**, and
per-month churn. Hotspots rank files by total churn.

**Evidence** `[REPLICATED]` — **the strongest, best-replicated defect signal we
compute.** Nagappan & Ball (2005), Windows Server 2003, 2,465 binaries: absolute
churn is a poor predictor (R²≈0.05) but **relative churn is highly predictive**
of defect density (R²≈0.81–0.84). Sits under the replicated result that
process/change metrics beat static product metrics (Rahman & Devanbu 2013).

**Caveat** — **normalize churn** (per LOC / per file / per commit); absolute
churn is weak. This is also the single metric where AI-generated code shows the
clearest degradation (GitClear: 2-week churn 3.1%→5.7%).

> Nagappan & Ball (2005), *ICSE*. · Hassan (2009), *ICSE*. · Moser et al. (2008).

## 5. Hotspots (churn × complexity)

**Measures** files high in *both* change frequency and complexity — the
highest-priority refactoring targets. Complexity only matters where code
actually changes.

**Caveat** `[SINGLE, vendor-adjacent]` — the framework (Tornhill, *Your Code as a
Crime Scene*) is a practitioner heuristic; the "15× more defects / 124% more
time" impact multiples come from Tornhill & Borg (TechDebt 2022) using
CodeScene's proprietary "Code Health" score — **vendor-derived, not
independently replicated.** Use hotspots to *rank* targets; cite the multiples as
vendor-adjacent.

> Tornhill, *Your Code as a Crime Scene* (2015/2024). · Tornhill & Borg (2022),
> arXiv:2203.04374.

## 6. Code survival / 2-week churn

**Measures** the inverse of churn: of the lines a commit added, what fraction are
still alive after age Δt? Computed from `git-of-theseus` line-survival output,
line-weighted, reported at 14/30/60 days with a half-life estimate.

**Baselines** `[VENDOR]` — GitClear longitudinal series: **pre-AI 2020 ~96.9%**
of lines survive 2 weeks; **AI-assisted 2024 ~94.3%** (5.7% churn). The
convergent AI-code thesis: assistants optimize for *producing plausible new
lines fast* rather than *integrating/refactoring* — "written to be added, not
reused."

**Caveat** — GitClear is a **vendor** (sells git analytics; full PDFs gated). The
*direction* is corroborated by the best independent bridge, **DORA 2024**
(delivery stability ↓~7.2% per 25% AI adoption). Exact multiples are
framing-dependent; editions use different base years and are not stackable.

> GitClear (2024 "Coding on Copilot", 2025 "AI Copilot Code Quality"). ·
> DORA / *Accelerate State of DevOps 2024*.

## 7. Production duplication (copy/paste)

**Measures** % of duplicated tokens across **production** code only (tests,
`node_modules`, docs, vendor, build output excluded) via `jscpd`. Reported
against GitClear bands.

**Baselines** `[VENDOR]` — GitClear: copy/paste **8.3% (pre-AI) → 12.3% (AI)**;
duplicated blocks "increased eightfold" / "4× growth in code clones" (different
metrics — don't conflate).

**Caveat** — jscpd's token % is **not identical** to GitClear's block metric;
compare *direction*, not decimals. Tests are excluded so the figure reflects
shipped code.

## 8. Process / history metrics

Rahman & Devanbu (2013), 85 repos: **process metrics significantly outperform
code metrics**, which are "unstable" across releases `[REPLICATED]`. From
`git log`:

- **Change (temporal) coupling** — files that change together (co-changes ≥4,
  with confidence). Cross-directory coupling is a design-smell signal (Tornhill).
- **Change entropy (HCM)** — Shannon entropy of how modifications scatter across
  files, normalized 0–1. Better fault predictor than prior-faults/-modifications
  (Hassan 2009). *(Do not cite fabricated accuracy percentages — the paper's
  claim is relative, not a specific %.)*
- **Ownership / bus-factor** — top-owner share and minor-contributor counts
  predicted defects *independent of size and churn* (Bird et al. 2011,
  "Don't Touch My Code!"). We report distinct authors, authors-for-50%-of-churn,
  and top-author share.
- **Commit size** — median/p90 churn and file count; % large commits (>400 LOC).
- **Fix ratio** — % of commits whose subject matches `fix|bugfix|hotfix`, plus
  fix-touched hotspots. An SZZ-lite defect proxy (Śliwerski et al. 2005; noisy).

**Caveat for agentic repos** — bus-factor and ownership metrics **assume human
authorship**. When one operator drives an AI pipeline, the git author collapses
to a single identity, so a bus-factor of 1 reflects the **attribution model**,
not team fragility. Also mind `git blame` attribution noise (last toucher ≠
original author) — mitigate with `-M`/`-C` and `.git-blame-ignore-revs`.

> Rahman & Devanbu (2013), *ICSE*. · Hassan (2009). · Bird et al. (2011),
> ESEC/FSE. · Śliwerski, Zimmermann & Zeller (2005), *MSR* (SZZ).

---

## Interpreting the numbers

1. **Always report CC alongside SLOC** — a "high CC" file may just be big.
2. **Use CC thresholds as bands, not verdicts** — >10 is an attention point, the
   higher SEI bands are characterizations, not validated defect thresholds.
3. **Cognitive complexity = effort, not correctness** — no validated threshold.
4. **Relative churn is the strongest signal** — normalize it; absolute churn is weak.
5. **Treat MI as directional only** — never an authoritative score.
6. **Hotspots prioritise; the impact multiples are vendor-derived.**
7. **Distinguish vendor from peer-reviewed** — GitClear (churn/duplication) is a
   vendor; DORA 2024 is the semi-independent corroboration; the peer-reviewed
   AI-code studies (Pearce 2022 security ~40%, Nguyen & Nadi 2022 correctness
   27–57%) test 2021-era Copilot.
8. **Control for size and survivorship** — mine history, not HEAD, to avoid
   undercounting reverted/churned code. Correlation ≠ causation; the pipeline is
   one of many confounders (team experience, file age, ownership).
9. **The load-bearing caveat:** most of these metrics are size in disguise — the
   non-size signals (relative churn, duplication, survival) are the trustworthy ones.

### Evidence-strength summary

| Finding | Strength |
|---|---|
| CC ≈ SLOC (adds little at file level) | `[REPLICATED]` Shepperd '88, Jay '09, Herraiz/Hassan '10 |
| Cognitive complexity ↔ comprehension time (not correctness) | `[SINGLE meta-study]` Muñoz Barón '20 |
| Relative churn strongly predicts defect density | `[REPLICATED]` Nagappan/Ball '05; Rahman/Devanbu '13 |
| Process metrics > static code metrics | `[REPLICATED]` Moser '08, Rahman/Devanbu '13 |
| Hotspot impact multipliers (15×/124×) | `[SINGLE, vendor]` Tornhill/Borg '22 |
| MI coefficients unvalidated; size predicts effort as well | `[strong critique]` van Deursen '14; Sjøberg '13 |
| AI code → rising churn/duplication, declining reuse | `[VENDOR longitudinal]` GitClear '24–'26 |
| AI adoption → delivery-stability decline | `[large-N semi-independent survey]` DORA '24 |
| Size & ownership confound metric attribution | `[REPLICATED]` El Emam '01; Bird et al. '11 |

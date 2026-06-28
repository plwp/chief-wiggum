# Chief Wiggum on E2EDev — Benchmark Report

**Benchmark:** [E2EDev](https://github.com/SCUNLP/E2EDev) ([arXiv 2510.14509](https://arxiv.org/abs/2510.14509)) — 46 single-page web-app tasks. Each task gives a self-contained `prompt.txt` and is graded by **held-out Selenium/Behave BDD tests** keyed on exact `data-testid` / `id` / text values. Hard by design: the held-out tests check selectors the prompt under-specifies (across the suite, ~47% of tested selectors never appear in the prompt).

**Date:** 2026-06 · **Protocol:** black-box (framework sees `prompt.txt` only; tests held out; graded once).

## Headline result

| Config | Test Acc | Req Acc | Balanced |
|---|---|---|---|
| **Chief Wiggum** — general principles, Claude sonnet builder | **67.1%** (472/703) | **52.9%** (129/244) | **59.2%** |
| Best published — Claude-Haiku 4.5 + GPT-Engineer | 69.4% | 53.8% | 60.0% |
| Best GPT-4o framework | ~61% (Test) | 42.7–50.8% | — |
| Qwen2.5-72B / 7B frameworks | — | 10–43% | — |

- **Test Accuracy** = fraction of all test cases passing. **Requirement Accuracy** = fraction of requirements where *every* associated test case passes. **Balanced** = harmonic mean.
- Chief Wiggum is **within ~1pp of the best published configuration on all three metrics** and ahead of every GPT-4o / Qwen result. Caveat: backbones differ (sonnet vs Haiku 4.5), so this is directional, not a controlled head-to-head.
- 9/46 tasks fully solved (100%); 7 of those at a perfect test-case score.

## How we got here (the engine lesson)

| Configuration | Score | Takeaway |
|---|---|---|
| Stock `/implement` (literal interpretation) | ~18% (Bench_08) | the problem |
| **+ general frontend principles** (shipped) | **67.1%** suite | the fix |
| + speculative "tag every element" nudge | 69.0% suite | rejected — see below |

The 18%→67% jump came from three **general** engineering principles, not benchmark tricks:

1. **No native browser dialogs** — render user-facing messages into on-page elements. Native `window.alert()` blocks Selenium *and* is poor UX/inaccessible. This was the single biggest lever (it also unblocked a cascade where a modal alert broke unrelated tests).
2. **Match conventions the spec demonstrates** — if the prompt shows kebab-case `data-testid`s by example, apply that style consistently to the elements you build.
3. **Build the complete idiomatic component** — a navbar gets its brand + links; a table gets headers — rather than the literal minimum.

These are now in `/implement` Step 6 (PR #78).

### Why the "nudge" was rejected

A benchmark-aware variant that *speculatively* carpet-tagged every element with guessed `data-testid`s scored 69.0% vs 67.1% — but an ablation showed this **+1.8pp is noise, not signal**:

- The entire net difference (13 test cases) came from **one** high-variance task (Bench_03, a seat-grid where the two independent builds diverged 3/20 vs 16/20). **Excluding it, general-only and the nudge are identical (469/683 each).**
- On a controlled 10-task sample the nudge looked like +8pp, but that sample over-represented structural tasks; at suite scale it vanishes.
- On 9 tasks general-only *beat* the nudge; on Bench_48 the nudge actively hurt.

Carpet-tagging is benchmark-specific gaming with ~no real value, so it was deliberately **not** shipped.

## Honest caveats

- **Backbone differs** from the paper's rows (Claude sonnet builder vs Haiku 4.5); not a controlled comparison.
- **One benchmark bug:** task 5.0 of Bench_08 has malformed step code (0 newlines → Python syntax error) and is unscorable for any framework.
- **Un-inferable ids:** some held-out selectors (e.g. `alert-user`) are idiosyncratic strings no framework can derive from the prompt — a real ceiling on black-box scores.
- **Per-task build variance** is real (independent LLM generations); single-task swings (Bench_03) can move suite aggregates by ~2pp.
- **Toolchain note:** the eval ran on system Python 3.9.6 (Homebrew's 3.12/3.14 are broken on this macOS via a `pyexpat` symbol gap) with chromedriver pinned to the installed Chrome major. The eval deps live in a venv; chief-wiggum-side deps should also be venv-isolated (cleanup owed).

## Reproduce

Harness: `scripts/e2edev_harness.py` (`list` / `issue <bench>` / `stage <bench> --from <dir>` / `grade <bench>...`).

```bash
# 1. clone benchmark into ~/.chief-wiggum/e2edev/E2EDev
# 2. build each task's app from prompt.txt (black-box) into builds/<bench>/
# 3. stage + grade
python3 scripts/e2edev_harness.py stage <bench> --from builds/<bench>
python3 scripts/e2edev_harness.py grade <bench>          # runs held-out Behave suite
```

Per-task results land in `<warehouse>/_behave_results/<bench>_behave.json`; aggregate Test/Req accuracy from those.

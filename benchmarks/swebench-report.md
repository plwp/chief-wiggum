# Chief Wiggum on SWE-bench Verified — Benchmark Report

**Benchmark:** [SWE-bench Verified](https://www.swebench.com/) — real GitHub issues from popular Python projects (django, sympy, sphinx, astropy, matplotlib, xarray, …). The task: given the issue text and the repo at a base commit, produce a patch that makes the held-out `FAIL_TO_PASS` tests pass without breaking `PASS_TO_PASS`. Graded by the official harness in per-instance Docker containers.

**Date:** 2026-06 · **Protocol:** black-box (the solver sees only the issue text + repo source; it never sees the tests or the gold patch). Graded once by the official `swebench` harness.

## Result

| Set | Resolved | Rate |
|---|---|---|
| **Random 20-instance subset of Verified** (seed 0) | **15 / 20** | **75%** |
| of cleanly-graded instances | 15 / 19 | 78.9% |
| Pipeline-validation pair (small, hand-picked) | 2 / 2 | — |

- The 20 instances were drawn by a fixed random seed from the 500-task Verified set (not cherry-picked for ease); the repo mix (django 8, sympy 6, sphinx 3, astropy/matplotlib/xarray 1 each) tracks Verified's own distribution.
- **Resolved (15):** django-13821, django-14434, django-15268, django-15561, django-15851, django-16082, django-16899, django-17029, xarray-2905, sphinx-11510, sympy-13372, sympy-16766, sympy-19495, sympy-24066, sympy-24213.
- **Unresolved (4):** matplotlib-22865, sphinx-8265, sphinx-9711, sympy-16597 — patches applied and tests ran, but `FAIL_TO_PASS` did not fully pass.
- **Infra-errored (1):** astropy-8707 — environment image build OOM-killed under x86 emulation (see Caveats); not a solver failure.

For context, leading SWE-bench Verified agents report roughly 60–75% on the full 500-task set; our 75% is on a 20-instance subset, so treat it as **indicative, not definitive** — at N=20 the 95% confidence interval is wide (~±19pp).

## Method

1. **Solve (local, black-box):** for each instance, clone the repo at its `base_commit`, hand a chief-wiggum solver agent (Claude opus) the `problem_statement` only. It explores the repo, locates the root cause, and edits **non-test** source files. No access to tests, gold patch, or the internet.
2. **Collect:** `git diff` of each checkout → a `predictions.jsonl` in SWE-bench format.
3. **Grade (Docker):** the official `swebench.harness.run_evaluation` applies each patch and runs the held-out tests in the instance's container.

Adapter: `scripts/swebench_harness.py` (`prep` / `collect`; grading is the official harness).

## Caveats (honest)

- **Subset, not full 500.** A complete Verified run is compute/disk-bound on a laptop under x86 emulation (each instance is a multi-GB image build + emulated test run). The harness scales to the full set on an x86 or higher-memory host; this report is a representative random subset.
- **x86 emulation OOM.** SWE-bench images are x86_64; on this arm64 Mac they run under emulation, where `conda` env builds are memory-hungry. Running the grader with `--max_workers 3` OOM-killed 14 env builds (exit 137); re-running serially (`--max_workers 1`) recovered 13 of them. One (astropy-8707) persistently OOM'd. Lesson: grade serially (or on a higher-memory/x86 host).
- **Backbone:** Claude opus solver agents under chief-wiggum's solve loop. Comparisons to published rows that use other backbones are directional.
- **Grades are real:** every "resolved" is the official harness confirming the held-out `FAIL_TO_PASS` + `PASS_TO_PASS` tests pass.

## Reproduce

```bash
# prep checkouts for a random/seeded subset
python3 scripts/swebench_harness.py prep --subset verified --ids <id,id,...>
# (solve each work/<id>/repo with a chief-wiggum solver agent — edits non-test source)
python3 scripts/swebench_harness.py collect          # -> work/predictions.jsonl
# grade (in the swebench venv; serial to avoid emulation OOM)
python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path work/predictions.jsonl --instance_ids <ids> \
  --run_id cw --namespace '' --max_workers 1
```

#!/usr/bin/env python3
"""E2EDev <-> Chief Wiggum benchmark adapter.

Bridges the E2EDev benchmark (SCUNLP/E2EDev) and the Chief Wiggum pipeline so
a full seed -> architect -> implement -> close-epic run can be scored by
E2EDev's own Behave/Selenium BDD grader.

Each E2EDev task is a single-page web app described by a self-contained
`prompt.txt` and graded by Gherkin scenarios keyed on `data-testid` selectors.
This adapter does three mechanical jobs; the actual building is done by the
Chief Wiggum skills against an ad-hoc target repo.

  issue   <bench>            emit a GitHub-issue body derived from prompt.txt
  stage   <bench> --from D   copy a built app (index.html/js/css/assets) into
                             the Behave warehouse under <bench>/
  grade   <bench>...         run E2EDev's grader on staged benches; print and
                             persist per-test-case pass/fail + a pass rate
  list                       list bench ids with requirement / test-case counts

Paths (override via env):
  E2EDEV_ROOT     default ~/.chief-wiggum/e2edev/E2EDev   (the cloned benchmark)
  CW_WAREHOUSE    default ~/.chief-wiggum/e2edev/cw_warehouse
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()
E2EDEV_ROOT = Path(os.environ.get("E2EDEV_ROOT", HOME / ".chief-wiggum/e2edev/E2EDev"))
WAREHOUSE = Path(os.environ.get("CW_WAREHOUSE", HOME / ".chief-wiggum/e2edev/cw_warehouse"))
DATA = E2EDEV_ROOT / "E2EDev_data"


def _bench_dir(bench: str) -> Path:
    d = DATA / bench
    if not d.is_dir():
        sys.exit(f"[e2edev] no such bench: {bench} (looked in {DATA})")
    return d


def _tests_json(bench: str) -> dict:
    p = _bench_dir(bench) / "requirment_with_tests.json"
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_list(_args) -> None:
    benches = sorted(d.name for d in DATA.iterdir() if d.is_dir())
    print(f"{len(benches)} benches in {DATA}\n")
    for b in benches:
        try:
            fg = _tests_json(b).get("finegrained_rewith_test", {})
            n_req = len(fg)
            n_tc = sum(len(v.get("test_cases", [])) for v in fg.values())
            print(f"  {b:22s} reqs={n_req:2d}  test_cases={n_tc:2d}")
        except Exception as e:  # noqa: BLE001
            print(f"  {b:22s} (unreadable: {e})")


def cmd_issue(args) -> None:
    bench = args.bench
    prompt = (_bench_dir(bench) / "prompt.txt").read_text(encoding="utf-8").strip()
    title = f"[{bench}] Implement E2EDev single-page web app"
    body = (
        f"## Source\n"
        f"E2EDev benchmark task `{bench}` (SCUNLP/E2EDev). This is a single-page "
        f"web application (HTML + JavaScript + CSS) graded by an external "
        f"Behave/Selenium BDD suite keyed on the exact `data-testid`, `id`, and "
        f"text values in the spec below. **Do not rename or omit any selector.**\n\n"
        f"## Acceptance\n"
        f"The built app must satisfy every requirement below. Output must live at "
        f"the repo root as `index.html` (plus its JS/CSS), loadable via `file://` "
        f"with no build step or server.\n\n"
        f"## Specification (verbatim from E2EDev prompt.txt)\n\n"
        f"```\n{prompt}\n```\n"
    )
    print(title)
    print("---8<--- body ---8<---")
    print(body)


def cmd_stage(args) -> None:
    bench = args.bench
    src = Path(args.frm).expanduser().resolve()
    if not src.is_dir():
        sys.exit(f"[e2edev] --from is not a dir: {src}")
    htmls = list(src.rglob("index.html")) or list(src.rglob("*.html"))
    if not htmls:
        sys.exit(f"[e2edev] no HTML file found under {src}")
    app_root = htmls[0].parent
    dest = WAREHOUSE / bench
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # Copy the web app's own code + assets (skip VCS / node noise). The grader
    # pulls non-code assets from the E2EDev source itself, but we copy the built
    # app's html/js/css and any local assets it ships.
    skip_dirs = {".git", "node_modules", "features", ".claude", "docs"}
    copied = []
    for p in app_root.rglob("*"):
        if any(part in skip_dirs for part in p.relative_to(app_root).parts):
            continue
        if p.is_file():
            rel = p.relative_to(app_root)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
            copied.append(str(rel))
    print(f"[e2edev] staged {bench}: {len(copied)} files -> {dest}")
    for c in sorted(copied)[:20]:
        print(f"    {c}")


def cmd_grade(args) -> None:
    if not E2EDEV_ROOT.is_dir():
        sys.exit(f"[e2edev] E2EDEV_ROOT not found: {E2EDEV_ROOT}")
    logs = WAREHOUSE / "_behave_logs"
    results = WAREHOUSE / "_behave_results"
    logs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    # Import E2EDev's own grader and reuse its main(); it iterates every bench in
    # E2EDev_data and skips ones absent from the warehouse, so staging only the
    # requested benches naturally scopes the run.
    sys.path.insert(0, str(E2EDEV_ROOT))
    import run_behave_test  # type: ignore  # noqa: E402

    want = set(args.benches)
    # Temporarily hide unstaged benches by pointing the grader at our warehouse;
    # it already skips benches missing from the warehouse.
    if want:
        staged = {d.name for d in WAREHOUSE.iterdir() if d.is_dir() and not d.name.startswith("_")}
        missing = want - staged
        if missing:
            sys.exit(f"[e2edev] not staged (run `stage` first): {sorted(missing)}")

    run_behave_test.main(str(WAREHOUSE), str(logs), str(results))

    # Aggregate results for the requested benches.
    total = passed = 0
    summary = {}
    for res in sorted(results.glob("*_behave.json")):
        bench = res.name.replace("_behave.json", "")
        if want and bench not in want:
            continue
        data = json.loads(res.read_text(encoding="utf-8"))
        b_total = b_pass = 0
        for _req, cases in data.items():
            for _idx, verdict in cases.items():
                b_total += 1
                b_pass += verdict == "pass"
        summary[bench] = {"passed": b_pass, "total": b_total,
                          "rate": round(b_pass / b_total, 4) if b_total else 0.0}
        total += b_total
        passed += b_pass

    out = {"per_bench": summary,
           "overall": {"passed": passed, "total": total,
                       "rate": round(passed / total, 4) if total else 0.0}}
    (WAREHOUSE / "_summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\n=== E2EDev grade ===")
    for bench, s in summary.items():
        print(f"  {bench:22s} {s['passed']:2d}/{s['total']:2d}  ({s['rate']*100:.1f}%)")
    print(f"  {'OVERALL':22s} {passed:2d}/{total:2d}  ({out['overall']['rate']*100:.1f}%)")
    print(f"\nwrote {WAREHOUSE / '_summary.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="E2EDev <-> Chief Wiggum benchmark adapter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    pi = sub.add_parser("issue"); pi.add_argument("bench"); pi.set_defaults(func=cmd_issue)
    ps = sub.add_parser("stage"); ps.add_argument("bench"); ps.add_argument("--from", dest="frm", required=True); ps.set_defaults(func=cmd_stage)
    pg = sub.add_parser("grade"); pg.add_argument("benches", nargs="*"); pg.set_defaults(func=cmd_grade)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

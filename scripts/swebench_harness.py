#!/usr/bin/env python3
"""SWE-bench <-> Chief Wiggum adapter.

SWE-bench gives a real GitHub issue + a repo at a base commit; the task is to
produce a patch that makes the held-out FAIL_TO_PASS tests pass (without breaking
PASS_TO_PASS). This adapter does the mechanical glue; the actual fix is produced
by a chief-wiggum solver agent working in the prepared checkout.

  prep   --subset <name> [--n N] [--ids id,id]   clone each repo at its base
                                                  commit into WORKDIR/<id>/repo,
                                                  write problem.md + meta.json
  collect                                         git-diff each checkout vs its
                                                  base commit -> predictions.jsonl
  list   --subset <name> [--n N]                  list selected instance ids

Grading is the official harness (run in the swebench venv):
  python -m swebench.harness.run_evaluation --predictions_path <preds.jsonl> \
    --dataset_name <ds> --run_id <id> --namespace '' --max_workers K

Paths (env overrides): SWE_WORKDIR (default ~/.chief-wiggum/swebench/work)
Dataset names: verified -> princeton-nlp/SWE-bench_Verified, lite -> ..._Lite
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
WORKDIR = Path(os.environ.get("SWE_WORKDIR", HOME / ".chief-wiggum/swebench/work"))
REPOS = WORKDIR / "_repos"
MODEL_NAME = "chief-wiggum"
DATASETS = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
    "full": "princeton-nlp/SWE-bench",
}


def _load(subset: str):
    from datasets import load_dataset  # swebench venv only
    return load_dataset(DATASETS[subset], split="test")


def _select(args):
    ds = _load(args.subset)
    rows = list(ds)
    if args.ids:
        want = set(args.ids.split(","))
        rows = [r for r in rows if r["instance_id"] in want]
    else:
        # smallest gold patch first = easiest/smallest changes, good for a smoke
        rows.sort(key=lambda r: len(r.get("patch", "")))
        if args.n:
            rows = rows[: args.n]
    return rows


def _run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def cmd_list(args):
    for r in _select(args):
        print(f"{r['instance_id']:45s} {r['repo']:28s} patch={len(r.get('patch',''))} diff={r.get('difficulty','')}")


def cmd_prep(args):
    rows = _select(args)
    REPOS.mkdir(parents=True, exist_ok=True)
    done = []
    for r in rows:
        iid, repo, base = r["instance_id"], r["repo"], r["base_commit"]
        mirror = REPOS / (repo.replace("/", "__") + ".git")
        if not mirror.exists():
            print(f"[prep] mirroring {repo} ...")
            _run(["git", "clone", "--bare", f"https://github.com/{repo}.git", str(mirror)])
        else:
            _run(["git", "--git-dir", str(mirror), "fetch", "--all", "-q"], check=False)
        dst = WORKDIR / iid / "repo"
        if dst.exists():
            _run(["git", "-C", str(dst), "checkout", "-f", base], check=False)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "clone", "-q", "--no-checkout", str(mirror), str(dst)])
            _run(["git", "-C", str(dst), "checkout", "-f", base])
        # write the task for the solver agent
        (WORKDIR / iid / "problem.md").write_text(r["problem_statement"], encoding="utf-8")
        (WORKDIR / iid / "meta.json").write_text(json.dumps(
            {"instance_id": iid, "repo": repo, "base_commit": base,
             "version": r.get("version", ""), "hints": r.get("hints_text", "")[:2000]},
            indent=2), encoding="utf-8")
        done.append(iid)
        print(f"[prep] ready: {iid}  ({repo}@{base[:8]}) -> {dst}")
    print(f"\n[prep] {len(done)} instances ready under {WORKDIR}")


def cmd_collect(args):
    preds = []
    for d in sorted(WORKDIR.iterdir()):
        repo = d / "repo"
        if not (repo / ".git").exists():
            continue
        diff = _run(["git", "-C", str(repo), "diff"], check=False).stdout
        preds.append({"instance_id": d.name, "model_name_or_path": MODEL_NAME, "model_patch": diff})
        status = "patch" if diff.strip() else "EMPTY"
        print(f"[collect] {d.name}: {status} ({len(diff)} chars)")
    out = WORKDIR / "predictions.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for p in preds:
            f.write(json.dumps(p) + "\n")
    nonempty = sum(1 for p in preds if p["model_patch"].strip())
    print(f"\n[collect] wrote {len(preds)} predictions ({nonempty} non-empty) -> {out}")


def main():
    ap = argparse.ArgumentParser(description="SWE-bench <-> Chief Wiggum adapter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "prep"):
        p = sub.add_parser(name)
        p.add_argument("--subset", default="verified", choices=list(DATASETS))
        p.add_argument("--n", type=int, default=0)
        p.add_argument("--ids", default="")
        p.set_defaults(func=cmd_list if name == "list" else cmd_prep)
    pc = sub.add_parser("collect")
    pc.set_defaults(func=cmd_collect)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

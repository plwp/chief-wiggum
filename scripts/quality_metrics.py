#!/usr/bin/env python3
"""quality_metrics.py — orchestrator for the /code-metrics skill.

Runs the literature-grounded code-quality engines over one repo and emits
per-engine JSON + a consolidated report.md + charts into a session out dir.

Always-run engines (pure Python + git + lizard): churn, complexity, trend,
process. Optional engines run only when their external tool is present:
survival (git-of-theseus), duplication (jscpd/node). Every engine degrades
gracefully — a missing tool yields ``{"skipped": ...}``, never a crashed run.

Target resolution mirrors the other skills:
  - ``owner/repo``  -> resolved & cloned via scripts/repo.py
  - ``--repo PATH`` -> a direct local path
  - neither         -> the current git repo (git rev-parse --show-toplevel)

Usage:
    python3 scripts/quality_metrics.py [owner/repo] [--repo PATH] \\
        [--out DIR] [--top N] [--trend-n N] \\
        [--skip-trend] [--skip-survival] [--skip-duplication] \\
        [--venv VENV] [--gobin GOBIN]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quality import churn, complexity, duplication, process, report, survival, trend  # noqa: E402


def _current_repo_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def resolve_target(owner_repo: str | None, repo_path: str | None) -> str:
    """Resolve the target repo to a local absolute path."""
    if repo_path:
        p = Path(repo_path).expanduser().resolve()
        if not (p / ".git").exists():
            print(f"Error: {p} is not a git repository", file=sys.stderr)
            sys.exit(1)
        return str(p)
    if owner_repo:
        from repo import resolve_repo  # local import: only needed for owner/repo
        return str(resolve_repo(owner_repo))
    root = _current_repo_root()
    if not root:
        print("Error: not inside a git repo; pass owner/repo or --repo PATH", file=sys.stderr)
        sys.exit(1)
    return root


def _write(out_dir: str, name: str, data: dict) -> None:
    with open(os.path.join(out_dir, f"{name}.json"), "w") as fh:
        json.dump(data, fh, indent=2)


def run(args: argparse.Namespace) -> str:
    target = resolve_target(args.owner_repo, args.repo)
    if args.out:
        out_dir = args.out
    else:
        import env  # session temp dir under ~/.chief-wiggum/tmp — never the target repo
        out_dir = os.path.join(str(env.create_tmp()), "code-metrics", Path(target).name)
    os.makedirs(out_dir, exist_ok=True)
    print(f"[code-metrics] target: {target}", file=sys.stderr)
    print(f"[code-metrics] out:    {out_dir}", file=sys.stderr)

    engines: dict = {}

    print("[code-metrics] churn...", file=sys.stderr)
    engines["churn"] = churn.analyze(target, top_n=args.top, no_merges=True)
    _write(out_dir, "churn", engines["churn"])

    print("[code-metrics] complexity...", file=sys.stderr)
    engines["complexity"] = complexity.analyze(target, venv=args.venv, gobin=args.gobin)
    _write(out_dir, "complexity", engines["complexity"])

    print("[code-metrics] process...", file=sys.stderr)
    engines["process"] = process.analyze(target)
    _write(out_dir, "process", engines["process"])

    if args.skip_trend:
        engines["trend"] = {"skipped": "--skip-trend"}
    else:
        print("[code-metrics] trend...", file=sys.stderr)
        engines["trend"] = trend.analyze(
            target, workdir=os.path.join(out_dir, "wt"),
            n=args.trend_n, venv=args.venv, gobin=args.gobin,
        )
    _write(out_dir, "trend", engines["trend"])

    if args.skip_survival:
        engines["survival"] = {"skipped": "--skip-survival"}
    else:
        print("[code-metrics] survival...", file=sys.stderr)
        engines["survival"] = survival.analyze(
            target, workdir=os.path.join(out_dir, "survival"),
        )
    _write(out_dir, "survival", engines["survival"])

    if args.skip_duplication:
        engines["duplication"] = {"skipped": "--skip-duplication"}
    else:
        print("[code-metrics] duplication...", file=sys.stderr)
        engines["duplication"] = duplication.analyze(
            target, workdir=os.path.join(out_dir, "dup"),
        )
    _write(out_dir, "duplication", engines["duplication"])

    print("[code-metrics] report...", file=sys.stderr)
    report_path = report.write_report(engines, out_dir)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="code-quality metrics orchestrator")
    parser.add_argument("owner_repo", nargs="?", default=None,
                        help="owner/repo to resolve+clone (optional)")
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--top", type=int, default=25, help="churn hotspots to keep")
    parser.add_argument("--trend-n", type=int, default=10, help="trend sample points")
    parser.add_argument("--skip-trend", action="store_true", help="skip history trend sampling")
    parser.add_argument("--skip-survival", action="store_true", help="skip git-of-theseus survival")
    parser.add_argument("--skip-duplication", action="store_true", help="skip jscpd duplication")
    parser.add_argument("--venv", default=None, help="virtualenv with lizard/radon/complexipy")
    parser.add_argument("--gobin", default=None, help="dir containing gocognit/gocyclo")
    args = parser.parse_args()

    report_path = run(args)
    print(report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

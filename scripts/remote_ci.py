#!/usr/bin/env python3
"""Remote CI is the floor: wait for GitHub Actions on a pushed sha and say red.

A local pre-merge floor is not CI. One product CW built had ``main`` red on
GitHub for 11 days while ~35 tickets merged on a green local floor, because
nobody read the remote run. ``/implement-wave`` and ``/implement`` call this
after every push to the default branch, and before the next merge, and stop
the line on anything but green.

    remote_ci.py wait --repo owner/repo --sha <sha> [--branch main]
    remote_ci.py wait --repo owner/repo --branch main        # the branch tip

Exit codes (each one printed as a single loud verdict line):
  0  GREEN    every workflow run for the sha completed successfully
  1  RED      at least one run failed / was cancelled / timed out — stop the line
  2  PENDING  no verdict within --timeout, or no run appeared for the sha even
              though the repo has workflows (not green: CI did not run on it)
  3  NO_CI    the repo defines no GitHub Actions workflows — say so in the
              report; never treat it as green
  4  ERROR    gh failed (auth, network, unknown repo)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable

GREEN, RED, PENDING, NO_CI, ERROR = 0, 1, 2, 3, 4
OK_CONCLUSIONS = {"success", "skipped", "neutral"}

Gh = Callable[[list[str]], str]


class GhError(Exception):
    pass


def run_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def workflow_count(repo: str, gh: Gh) -> int:
    return int(gh(["api", f"repos/{repo}/actions/workflows", "--jq", ".total_count"]).strip() or 0)


def branch_tip(repo: str, branch: str, gh: Gh) -> str:
    return gh(["api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]).strip()


def runs_for(repo: str, sha: str, branch: str | None, gh: Gh) -> list[dict]:
    args = ["run", "list", "--repo", repo, "--commit", sha, "--limit", "50",
            "--json", "databaseId,workflowName,status,conclusion,url,headBranch"]
    if branch:
        args += ["--branch", branch]
    return json.loads(gh(args) or "[]")


def verdict(runs: list[dict]) -> int:
    """RED as soon as any completed run failed (no need to wait for the rest);
    GREEN only when every run completed OK; otherwise PENDING."""
    if any(r.get("status") == "completed" and r.get("conclusion") not in OK_CONCLUSIONS
           for r in runs):
        return RED
    if runs and all(r.get("status") == "completed" for r in runs):
        return GREEN
    return PENDING


def wait(repo: str, sha: str | None, branch: str | None, *, timeout: float = 1800,
         appear_timeout: float = 300, poll: float = 20, gh: Gh = run_gh,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic, out=sys.stdout) -> int:
    try:
        if workflow_count(repo, gh) == 0:
            print(f"remote CI: NO_CI — {repo} defines no GitHub Actions workflows. Nothing "
                  "checks this push remotely; state that in the report, do not call it green.",
                  file=out)
            return NO_CI
        if not sha:
            if not branch:
                raise GhError("need --sha or --branch")
            sha = branch_tip(repo, branch, gh)
        start = clock()
        while True:
            runs = runs_for(repo, sha, branch, gh)
            v = verdict(runs)
            if v == RED:
                bad = [r for r in runs if r.get("status") == "completed"
                       and r.get("conclusion") not in OK_CONCLUSIONS]
                print(f"remote CI: RED on {sha[:12]} — STOP THE LINE. No further merges "
                      "until this is green:", file=out)
                for r in bad:
                    print(f"  {r.get('workflowName')}: {r.get('conclusion')} {r.get('url')}",
                          file=out)
                return RED
            if v == GREEN:
                print(f"remote CI: GREEN on {sha[:12]} ({len(runs)} run(s))", file=out)
                return GREEN
            elapsed = clock() - start
            if not runs and elapsed >= appear_timeout:
                print(f"remote CI: PENDING — no workflow run appeared for {sha[:12]} within "
                      f"{int(appear_timeout)}s although {repo} has workflows. CI did not run "
                      "on this commit; that is not green.", file=out)
                return PENDING
            if elapsed >= timeout:
                print(f"remote CI: PENDING — {sha[:12]} still running after {int(timeout)}s; "
                      "not green, do not merge on top of it.", file=out)
                return PENDING
            sleep(poll)
    except (GhError, ValueError, json.JSONDecodeError) as exc:
        print(f"remote CI: ERROR — {exc}", file=out)
        return ERROR


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("wait", help="wait for the Actions verdict on a sha (or a branch tip)")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--sha", help="commit sha (default: tip of --branch)")
    p.add_argument("--branch", help="branch the push went to (e.g. main)")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--appear-timeout", type=float, default=300)
    p.add_argument("--poll", type=float, default=20)
    args = ap.parse_args(argv)
    if not args.sha and not args.branch:
        ap.error("wait needs --sha or --branch")
    return wait(args.repo, args.sha, args.branch, timeout=args.timeout,
                appear_timeout=args.appear_timeout, poll=args.poll)


if __name__ == "__main__":
    sys.exit(main())

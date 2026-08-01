#!/usr/bin/env python3
"""Evaluate the deferred-rigor index's build triggers (chief-wiggum#171/#161).

``docs/deferred-rigor.json`` records every DEFERRED system-layer decision with
its settled design notes and a concrete build trigger. This script evaluates
the MECHANICAL half of those triggers and reports, per item:

- ``FIRED``       — a mechanical check is true; file the full issue from the
                    item's ``settled_notes`` (do not relitigate the design).
- ``CANDIDATE``   — soft evidence found (e.g. a telemetry bug event matching
                    the trigger's keywords); a human confirms before filing.
- ``QUIET``       — mechanical checks evaluated and none fired.
- ``UNEVALUATED`` — the trigger needs human judgment (no mechanical checks,
                    or a required input like ``--repo`` wasn't provided);
                    surfaced, never silently skipped.

This is a **permanently report-only scrutiny cue, never a gate** (exit 0
always, same doctrine as the high-water test-file cue): the trigger table is
about not silently dropping deferred work, not about blocking anything.
``/reflect`` runs it each pass and converts FIRED rows into issues.

Check kinds:
- ``file_count_gt``: git-tracked file count in ``--repo`` exceeds ``value``.
- ``module_count_gt``: heuristic — directories (depth <= 2, excluding
  tests/vendor/node_modules/hidden) containing source files in ``--repo``.
- ``human_contributors_gte``: distinct non-bot author emails on ``--repo``'s
  history (heuristic: excludes emails containing bot/noreply markers).
- ``validated_gates_gt``: gate-validation records in
  ``<cw>/docs/quality/validation/*.json``.
- ``telemetry_whole_repo_queries_gte``: factory-log ``query`` events named
  writers/governs/map (the whole-repo shapes #161's trigger names).
- ``telemetry_bug_keyword``: factory-log ``bug`` events whose summary matches
  a keyword — always CANDIDATE, never FIRED (a human reads the bug).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CW_ROOT = HERE.parent
INDEX = CW_ROOT / "docs" / "deferred-rigor.json"

SOURCE_SUFFIXES = {".py", ".go", ".ts", ".tsx", ".js", ".rs", ".java", ".rb"}
EXCLUDED_DIRS = {"tests", "test", "vendor", "node_modules", "dist", "build"}
BOT_EMAIL_MARKERS = ("bot", "noreply", "no-reply", "actions@github.com")


def _git_files(repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files"],
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def check_file_count_gt(repo: Path | None, value: int) -> tuple[str, str]:
    if repo is None:
        return "UNEVALUATED", "needs --repo"
    n = len(_git_files(repo))
    return ("FIRED" if n > value else "QUIET"), f"{n} tracked files (threshold {value})"


def check_module_count_gt(repo: Path | None, value: int) -> tuple[str, str]:
    """Heuristic (what counts as a 'module' is judgment) — CANDIDATE at most,
    never FIRED: a fuzzy definition must not mechanically demand an issue."""
    if repo is None:
        return "UNEVALUATED", "needs --repo"
    dirs: set[str] = set()
    for f in _git_files(repo):
        p = Path(f)
        if p.suffix not in SOURCE_SUFFIXES:
            continue
        parts = [seg for seg in p.parent.parts if seg]
        if any(seg in EXCLUDED_DIRS or seg.startswith(".") for seg in parts):
            continue
        dirs.add("/".join(parts[:2]) or ".")
    n = len(dirs)
    return ("CANDIDATE" if n > value else "QUIET"), \
        f"{n} source modules, heuristic (threshold {value}) — human confirms module definition"


def check_human_contributors_gte(repo: Path | None, value: int) -> tuple[str, str]:
    """Heuristic (one human commonly commits under several emails) — CANDIDATE
    at most, never FIRED: email identity must be confirmed by a human."""
    if repo is None:
        return "UNEVALUATED", "needs --repo"
    out = subprocess.run(["git", "-C", str(repo), "log", "--format=%ae"],
                         capture_output=True, text=True, check=True)
    humans = sorted({e.strip().lower() for e in out.stdout.splitlines() if e.strip()
                     and not any(m in e.lower() for m in BOT_EMAIL_MARKERS)})
    n = len(humans)
    return ("CANDIDATE" if n >= value else "QUIET"), \
        f"{n} distinct non-bot author email(s) {humans} (threshold {value}) — may be one human under several emails"


def check_validated_gates_gt(value: int) -> tuple[str, str]:
    records = sorted((CW_ROOT / "docs" / "quality" / "validation").glob("*.json"))
    n = len(records)
    return ("FIRED" if n > value else "QUIET"), f"{n} gate-validation records (threshold {value})"


def _factory_events(log_path: Path) -> list[dict]:
    if not log_path.is_file():
        return []
    events = []
    for line in log_path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def check_telemetry_whole_repo_queries_gte(log_path: Path, value: int) -> tuple[str, str]:
    if not log_path.is_file():
        return "UNEVALUATED", f"no factory log at {log_path}"
    n = sum(1 for e in _factory_events(log_path)
            if e.get("event") == "query" and e.get("name") in ("writers", "governs", "map"))
    return ("FIRED" if n >= value else "QUIET"), f"{n} whole-repo query events (threshold {value})"


def check_telemetry_bug_keyword(log_path: Path, keywords: list[str]) -> tuple[str, str]:
    if not log_path.is_file():
        return "UNEVALUATED", f"no factory log at {log_path}"
    hits = []
    for e in _factory_events(log_path):
        if e.get("event") != "bug":
            continue
        summary = str(e.get("summary", "")).lower()
        if any(k.lower() in summary for k in keywords):
            hits.append(e.get("summary"))
    if hits:
        return "CANDIDATE", f"{len(hits)} bug event(s) match {keywords}: {hits[:3]} — human confirms before filing"
    return "QUIET", f"no bug events matching {keywords}"


def evaluate_item(item: dict, repo: Path | None, log_path: Path) -> dict:
    checks = item.get("checks", [])
    results = []
    for chk in checks:
        kind = chk.get("kind")
        if kind == "file_count_gt":
            status, detail = check_file_count_gt(repo, chk["value"])
        elif kind == "module_count_gt":
            status, detail = check_module_count_gt(repo, chk["value"])
        elif kind == "human_contributors_gte":
            status, detail = check_human_contributors_gte(repo, chk["value"])
        elif kind == "validated_gates_gt":
            status, detail = check_validated_gates_gt(chk["value"])
        elif kind == "telemetry_whole_repo_queries_gte":
            status, detail = check_telemetry_whole_repo_queries_gte(log_path, chk["value"])
        elif kind == "telemetry_bug_keyword":
            status, detail = check_telemetry_bug_keyword(log_path, chk["keywords"])
        else:
            status, detail = "UNEVALUATED", f"unknown check kind {kind!r}"
        results.append({"kind": kind, "status": status, "detail": detail})

    if not results:
        overall = "UNEVALUATED"
    elif any(r["status"] == "FIRED" for r in results):
        overall = "FIRED"
    elif any(r["status"] == "CANDIDATE" for r in results):
        overall = "CANDIDATE"
    elif all(r["status"] == "UNEVALUATED" for r in results):
        overall = "UNEVALUATED"
    else:
        overall = "QUIET"
    return {"id": item["id"], "title": item["title"], "source_issue": item.get("source_issue"),
            "trigger": item.get("trigger"), "status": overall, "checks": results}


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--index", type=Path, default=INDEX,
                    help=f"deferred-rigor index (default: {INDEX})")
    ap.add_argument("--repo", type=Path, default=None,
                    help="target repo for repo-shaped checks (module/file/contributor counts)")
    ap.add_argument("--factory-log", type=Path,
                    default=Path.home() / ".chief-wiggum" / "factory-log.jsonl",
                    help="factory telemetry log for telemetry-shaped checks")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    index = json.loads(args.index.read_text())
    results = [evaluate_item(item, args.repo, args.factory_log)
               for item in index.get("items", [])]

    if args.format == "json":
        print(json.dumps({"items": results}, indent=2))
    else:
        for r in results:
            print(f"[{r['status']:>11}] {r['id']} — {r['title']}")
            print(f"              trigger: {r['trigger']}")
            for c in r["checks"]:
                print(f"              - {c['kind']}: {c['status']} ({c['detail']})")
            if not r["checks"]:
                print("              - no mechanical checks: human judgment (surfaced, not silent)")
        fired = [r["id"] for r in results if r["status"] == "FIRED"]
        cand = [r["id"] for r in results if r["status"] == "CANDIDATE"]
        if fired:
            print(f"\nFIRED: {', '.join(fired)} — file the full issue(s) from settled_notes "
                  "(designs are quorum-settled; do not relitigate)")
        if cand:
            print(f"CANDIDATE: {', '.join(cand)} — human confirms the evidence before filing")
    return 0  # permanently report-only: a scrutiny cue, never a gate


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""hotspot_discovery.py — CLI for Tornhill-style hotspot discovery (#187).

Composes the existing ``scripts/quality/`` engines (churn, complexity,
process — see ``quality/hotspots.py`` docstring) into ``docs/quality/
hotspots.json``: an explicitly OBSERVATIONAL, rebuildable artifact. It carries
NO stable IDs, is referenced by NO ``@cw-trace`` link, and NEVER gates —
``code_query.py orient`` surfaces it only as a ``measured`` fact ranked below
every ``direct`` and ``inferred`` fact (INV-fh-007).

Target resolution mirrors the other skills:
  - ``owner/repo``  -> resolved & cloned via scripts/repo.py
  - ``--repo PATH`` -> a direct local path
  - neither         -> the current git repo (git rev-parse --show-toplevel)

Usage:
    python3 scripts/hotspot_discovery.py [owner/repo] [--repo PATH] \\
        [--out PATH] [--top N] [--min-co N] [--no-trend] \\
        [--venv VENV] [--gobin GOBIN] [--format text|json]

    python3 scripts/hotspot_discovery.py --repo PATH --check
        # verify docs/quality/hotspots.json is not stale (git_sha + window_days
        # match what a regenerate would derive right now). Exit 1 if stale or
        # missing. NEVER writes. NEVER blocks anything by itself — a workflow
        # that wants a gate wraps this in its own --gate flag, same convention
        # as every other report-only checker in this repo.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quality import hotspots  # noqa: E402


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


def _default_out(target: str) -> str:
    return str(Path(target) / "docs" / "quality" / "hotspots.json")


def render_text(result: dict) -> str:
    lines = [
        f"hotspots ({result['schema']}) @ {result.get('git_sha') or 'HEAD'} "
        f"— window {result.get('window_days', 0)}d, no_merges={result.get('no_merges')}",
        "",
        result.get("authority", ""),
    ]
    if result.get("note"):
        lines += ["", f"NOTE: {result['note']}"]
    hs = result.get("hotspots", [])
    if not hs:
        lines += ["", "(no rankable files)"]
        return "\n".join(lines) + "\n"
    lines += ["", f"top {min(20, len(hs))} of {len(hs)} ranked file(s):", ""]
    lines.append(f"{'file':<50} {'score':>8} {'decile':>6} {'churn':>7} {'complexity':>11} {'trend':>8}")
    for h in hs[:20]:
        lines.append(
            f"{h['file']:<50} {h['score']:>8.4f} {h['decile']:>6} {h['churn']:>7} "
            f"{h['complexity']:>11} {(h.get('trend') or '-'):>8}"
        )
        for c in h.get("coupled_with", [])[:3]:
            lines.append(f"    coupled: {c['file']} (confidence={c['confidence']}, co_changes={c['co_changes']})")
    return "\n".join(lines) + "\n"


def run_check(target: str, out_path: str, no_merges: bool) -> int:
    p = Path(out_path)
    if not p.exists():
        print(f"Error: {p} does not exist — run without --check to generate it", file=sys.stderr)
        return 1
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: {p} is not valid JSON ({e})", file=sys.stderr)
        return 1

    current_sha = hotspots.head_sha(target)
    if doc.get("git_sha") != current_sha:
        print(
            f"Stale: recorded git_sha={doc.get('git_sha')} != current HEAD={current_sha} "
            f"— run `python3 scripts/hotspot_discovery.py --repo {target}` to refresh",
            file=sys.stderr,
        )
        return 1

    current_window = hotspots.window_days_at(target, no_merges=doc.get("no_merges", no_merges))
    if current_window != doc.get("window_days"):
        print(
            f"Stale: recorded window_days={doc.get('window_days')} != re-derived "
            f"window_days={current_window} at the same sha — history changed shape "
            f"(rebase/rewrite?); regenerate.",
            file=sys.stderr,
        )
        return 1

    print(f"{p} is up to date (git_sha={current_sha}, window_days={current_window}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="hotspot discovery: churn x complexity + coupling")
    parser.add_argument("owner_repo", nargs="?", default=None, help="owner/repo to resolve+clone (optional)")
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--out", default=None, help="output path (default: <target>/docs/quality/hotspots.json)")
    parser.add_argument("--top", type=int, default=hotspots.DEFAULT_TOP_N, help="max ranked files to emit")
    parser.add_argument("--min-co", type=int, default=hotspots.DEFAULT_MIN_CO, help="min co-changes for coupling")
    parser.add_argument(
        "--coupled-top-n", type=int, default=hotspots.DEFAULT_COUPLED_TOP_N,
        help="max coupled partners recorded per hotspot",
    )
    parser.add_argument("--include-merges", action="store_true", help="include merge commits (default: excluded)")
    parser.add_argument("--no-trend", action="store_true", help="skip the recent-vs-expected trend computation")
    parser.add_argument("--venv", default=None, help="virtualenv with lizard")
    parser.add_argument("--gobin", default=None, help="dir containing go tools (unused by hotspots, forwarded for parity)")
    parser.add_argument(
        "--check", action="store_true",
        help="verify the artifact at --out is not stale (git_sha + re-derived window_days); never writes, exit 1 if stale/missing",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", help="stdout report format")
    args = parser.parse_args()

    target = resolve_target(args.owner_repo, args.repo)
    out_path = args.out or _default_out(target)
    no_merges = not args.include_merges

    if args.check:
        return run_check(target, out_path, no_merges)

    result = hotspots.discover(
        target,
        no_merges=no_merges,
        min_co=args.min_co,
        top_n=args.top,
        coupled_top_n=args.coupled_top_n,
        venv=args.venv,
        gobin=args.gobin,
        trend=not args.no_trend,
    )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    print(f"[hotspot-discovery] wrote {out_path}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

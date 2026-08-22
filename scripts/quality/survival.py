#!/usr/bin/env python3
"""survival.py — code-survival / 2-week churn from git-of-theseus output.

Literature signal (GitClear/DORA): AI-generated code shows elevated churn —
lines reverted/reworked soon after authoring. Code SURVIVAL is the inverse: of
the lines a commit added, what fraction are still alive after age dt? GitClear
baselines: pre-AI 2020 ~96.9% survive 2 weeks; AI-assisted 2024 ~94.3% (5.7%
churn). This engine reports survival at 14/30/60 days so a repo can be placed
against those bands.

git-of-theseus survival.json = {commit_hash: [[unix_ts, lines_alive], ...]}
(monotonic non-increasing). We anchor each commit to its author time, convert
each snapshot to an AGE in days, normalise by the commit's initial line count,
and aggregate a line-weighted survival curve across all commits.

Requires the ``git-of-theseus-analyze`` CLI (pip install git-of-theseus). If it
is absent, ``analyze`` returns ``{"skipped": ...}`` rather than raising.

As a module:
    from quality.survival import analyze
    result = analyze("/path/to/repo", workdir="/tmp/survival")

As a CLI:
    python3 -m quality.survival <repo> --workdir <dir>
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import os
import shutil
import subprocess
import sys

from . import cache

AGES = [7, 14, 30, 60, 90]


def _current_branch(repo: str) -> str | None:
    """The checked-out branch name, or None on a detached HEAD."""
    proc = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    name = proc.stdout.strip()
    return name or None


def _short_sha(repo: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() or None


@contextlib.contextmanager
def _analysable_branch(repo: str):
    """Yield a real branch name for git-of-theseus, making one if needed.

    git-of-theseus verifies its --branch with `git show-ref refs/heads/<name>`
    and falls back to GitPython's `active_branch`, which RAISES on a detached
    HEAD. So `--branch HEAD` never actually worked: on a normal checkout
    refs/heads/HEAD does not exist either, and every run silently took the
    warn-and-fallback path. On a detached checkout, which is what
    `git worktree add --detach` produces, the fallback throws and the whole
    survival signal is lost.

    A detached HEAD therefore gets a throwaway branch, deleted afterwards, so
    the analysis works on historical refs instead of requiring the operator to
    attach one by hand.
    """
    branch = _current_branch(repo)
    if branch:
        yield branch
        return
    sha = _short_sha(repo)
    if not sha:
        # Not a git repo, or an empty one. Let the caller's error path report
        # it rather than inventing a branch here.
        yield None
        return
    temporary = f"cw/survival-{sha}"
    created = subprocess.run(
        ["git", "-C", repo, "branch", "--force", temporary, "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if created.returncode != 0:
        yield None
        return
    try:
        yield temporary
    finally:
        subprocess.run(
            ["git", "-C", repo, "branch", "-D", temporary],
            capture_output=True, text=True, check=False,
        )


def _run_git_of_theseus(repo: str, outdir: str) -> tuple[str | None, dict | None]:
    """Run git-of-theseus-analyze into outdir. Returns ``(survival_path, problem)``.

    ``problem`` is ``None`` on success, else a ``{"status": ...}`` dict —
    mirrors ``duplication.run_jscpd``'s skipped/crashed split (#289): a tool
    that is not installed is a declared limitation (``"skipped"``); a tool
    that IS present and was expected to run but died is a broken instrument
    (``"crashed"``), never silently indistinguishable from the former.

    ``outdir`` may be reused across runs (the caller's workdir convention), so
    a ``survival.json`` left by an EARLIER successful run is unlinked before
    this run starts — otherwise a run that crashes without writing anything
    would let ``os.path.exists(survival)`` see the STALE file and report it as
    this run's fresh output (the exact #289 defect: "a crashed rerun parses
    the previous run's numbers as fresh"). The subprocess's returncode is also
    checked explicitly — previously only "does survival.json now exist" was
    checked, so a non-zero exit that happened to leave a fresh-looking file in
    place (or the stale one, pre-fix) was never caught.
    """
    # Prefer a tool co-located with the running interpreter (venv install),
    # then fall back to PATH — mirrors quality.complexity._tool discovery.
    sibling = os.path.join(os.path.dirname(sys.executable), "git-of-theseus-analyze")
    tool = sibling if os.path.exists(sibling) else shutil.which("git-of-theseus-analyze")
    if not tool:
        return None, {
            "status": "skipped",
            "skipped": "git-of-theseus not found",
            "note": "code survival requires git-of-theseus (pip install git-of-theseus)",
        }
    os.makedirs(outdir, exist_ok=True)
    survival_path = os.path.join(outdir, "survival.json")
    try:
        os.unlink(survival_path)
    except FileNotFoundError:
        pass
    with _analysable_branch(repo) as branch:
        command = [tool, repo, "--outdir", outdir, "--interval", "604800"]
        if branch:
            # weekly interval → enough resolution for 14/30-day survival bins
            command += ["--branch", branch]
        proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        return None, {
            "status": "crashed",
            "crashed": f"git-of-theseus-analyze exited {proc.returncode}",
            "skipped": f"git-of-theseus-analyze exited {proc.returncode}",
            "note": (proc.stderr or proc.stdout or "").strip()[:400],
            "exit_code": proc.returncode,
        }
    if not os.path.exists(survival_path):
        return None, {
            "status": "crashed",
            "crashed": "git-of-theseus-analyze produced no survival.json",
            "skipped": "git-of-theseus-analyze produced no survival.json",
            "exit_code": proc.returncode,
        }
    return survival_path, None


def analyze_survival_json(survival_path: str, repo: str, name: str | None = None) -> dict:
    """Analyze an existing git-of-theseus survival.json against author times."""
    name = name or repo.rstrip("/").split("/")[-1]
    with open(survival_path) as fh:
        surv = json.load(fh)

    hashes = list(surv)
    authored: dict[str, int] = {}
    if hashes:
        out = subprocess.run(
            ["git", "-C", repo, "show", "-s", "--format=%H %at", *hashes],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if " " in line:
                h, at = line.split()
                authored[h] = int(at)

    agg = {a: {"num": 0.0, "den": 0.0} for a in AGES}
    curve: list[tuple[float, float, int]] = []
    total_lines = 0
    for h, series in surv.items():
        ct = authored.get(h)
        if ct is None or not series:
            continue
        L0 = series[0][1]
        if L0 <= 0:
            continue
        total_lines += L0
        pts = [((ts - ct) / 86400.0, min(1.0, lines / L0)) for ts, lines in series]
        pts = [(a, f) for a, f in pts if a >= 0]
        if not pts:
            continue
        ages = [a for a, _ in pts]
        fracs = [f for _, f in pts]
        max_age = ages[-1]
        for A in AGES:
            if max_age >= A:  # commit has lived at least A days -> can measure
                i = bisect.bisect_left(ages, A)
                if i == 0:
                    f = fracs[0]
                elif i >= len(ages):
                    f = fracs[-1]
                else:
                    a0, a1 = ages[i - 1], ages[i]
                    f0, f1 = fracs[i - 1], fracs[i]
                    f = f0 + (f1 - f0) * ((A - a0) / (a1 - a0)) if a1 > a0 else f1
                agg[A]["num"] += f * L0
                agg[A]["den"] += L0
        for a, f in pts:
            curve.append((a, f, L0))

    result: dict = {
        "repo": name, "total_lines_tracked": total_lines,
        "survival_by_age_days": {},
    }
    for A in AGES:
        d = agg[A]["den"]
        result["survival_by_age_days"][A] = {
            "survival_pct": round(100 * agg[A]["num"] / d, 1) if d else None,
            "lines_old_enough": int(d),
        }

    # half-life: line-weighted survival across weekly age bins; first bin < 50%
    bins: dict[int, list[float]] = {}
    for a, f, w in curve:
        b = int(a // 7) * 7
        bins.setdefault(b, [0.0, 0.0])
        bins[b][0] += f * w
        bins[b][1] += w
    curve_pts = sorted((b, num / den) for b, (num, den) in bins.items() if den)
    half_life = None
    for b, s in curve_pts:
        if s < 0.5:
            half_life = b
            break
    result["half_life_days"] = half_life if half_life is not None else ">observed"
    result["weekly_survival_curve"] = [
        {"age_days": b, "survival": round(s, 3)} for b, s in curve_pts
    ]
    result["baselines"] = {
        "gitclear_pre_ai_2020_14d": 96.9,
        "gitclear_ai_2024_14d": 94.3,
        "note": "GitClear [VENDOR] longitudinal churn baselines — direction, not gospel.",
    }
    return result


def analyze(repo: str, workdir: str, name: str | None = None) -> dict:
    """Run git-of-theseus then analyze survival. Degrades gracefully if absent
    (``status: "skipped"``); a present-but-failing tool reports
    ``status: "crashed"`` (#289) rather than being silently indistinguishable
    from either "not installed" or a stale prior run's numbers.

    #328: git-of-theseus walks COMMITTED history only — it never reads the
    working tree — so the result is a pure function of HEAD alone. Cached on
    disk keyed by ``head_sha`` (unlike jscpd/trend, which read file bytes and
    need the dirty-worktree-aware ``manifest_key`` instead): a second run at
    the same HEAD, even from a later process, skips the tool entirely.
    ``CW_QUALITY_NO_CACHE=1`` bypasses both the read and the write. Only a
    genuinely successful measurement is memoized; ``head_sha`` returning
    ``None`` (not a git repo) makes this uncacheable, never a crash."""
    name = name or repo.rstrip("/").split("/")[-1]
    head = cache.head_sha(repo)
    cached = cache.load(repo, "survival", head) if head else None
    if cached is not None:
        return cached
    survival_path, problem = _run_git_of_theseus(repo, workdir)
    if problem is not None:
        return {"repo": name, **problem}
    result = analyze_survival_json(survival_path, repo, name=name)
    if head:
        cache.store(repo, "survival", head, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="code survival / 2-week churn")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for git-of-theseus output")
    parser.add_argument("--name", default=None, help="display name for the repo")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="force a fresh git-of-theseus run, bypassing the HEAD-keyed result cache (#328)",
    )
    args = parser.parse_args()
    if args.no_cache:
        os.environ[cache.NO_CACHE_ENV] = "1"
    print(json.dumps(analyze(args.repo, args.workdir, name=args.name), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

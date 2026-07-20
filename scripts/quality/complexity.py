#!/usr/bin/env python3
"""complexity.py — current-state code-quality metric battery for one repo.

Literature-anchored product metrics, computed uniformly across languages:
  - Cyclomatic complexity (McCabe 1976): distribution, %>10/15/20, max, mean —
    via lizard (Py/Go/TS/JS in one parser). Report ALONGSIDE SLOC: CC is
    ~0.7-0.9 collinear with size at file level (Jay 2009), so a "high CC" file
    may just be a big file.
  - Function length (long-method smell): mean NLOC, %>60 lines.
  - Cognitive complexity (Campbell/SonarSource 2018): Go via gocognit, Python
    via complexipy. A proxy for comprehension EFFORT, not correctness
    (Munoz Baron 2020); no validated threshold. (No maintained TS CLI -> n/a.)
  - Maintainability Index (Oman-Hagemeister; disputed — van Deursen 2014):
    Python via radon, reported DIRECTIONALLY only.
  - Scale + test ratio: tracked source LOC, test/non-test split.

Only git-TRACKED source files are measured; docs/, vendor/, generated, and
contract-DSL files are excluded so metrics reflect product code.

Tools are auto-discovered (venv/bin then PATH). Any missing tool degrades to a
``None`` sub-metric with a note — the battery never crashes.

As a module:
    from quality.complexity import analyze
    result = analyze("/path/to/repo", venv=None, gobin=None)

As a CLI:
    python3 -m quality.complexity <repo> [--venv <venv>] [--gobin <gobin>]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

EXT_LANG = {
    ".py": "python", ".go": "go", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
}
TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|e2e)/|(_test\.go$)|"
    r"(\.(test|spec)\.[tj]sx?$)|(^|/)test_[^/]+\.py$|_test\.py$",
    re.IGNORECASE,
)
# non-product source: docs/DSL, infra models, migrations autogen, node stuff
EXCLUDE_RE = re.compile(
    r"(^|/)(docs|node_modules|dist|build|out|\.next|vendor|\.venv|venv|"
    r"__pycache__|coverage|migrations|\.git)/|"
    r"(^|/)models/.*\.(go|py)$",  # contract-DSL guard files under */models/
    re.IGNORECASE,
)


def run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _tool(name: str, venv: str | None = None, gobin: str | None = None) -> str | None:
    """Resolve a tool: prefer <venv>/bin, then <gobin>, then the running
    interpreter's own bin dir, then PATH. Returns None if absent."""
    if venv:
        cand = os.path.join(venv, "bin", name)
        if os.path.exists(cand):
            return cand
    if gobin:
        cand = os.path.join(gobin, name)
        if os.path.exists(cand):
            return cand
    # If invoked via <somevenv>/bin/python, sibling tools live next to it.
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(name)


def tracked_files(repo: str) -> list[str]:
    out = run("git", "-C", repo, "ls-files").stdout.splitlines()
    return [f for f in out if not EXCLUDE_RE.search(f)]


def bucket(repo: str) -> dict:
    """Return {lang: {'src': [abs], 'test': [abs]}}."""
    b: dict = defaultdict(lambda: {"src": [], "test": []})
    for rel in tracked_files(repo):
        ext = os.path.splitext(rel)[1].lower()
        lang = EXT_LANG.get(ext)
        if not lang:
            continue
        absf = os.path.join(repo, rel)
        if not os.path.exists(absf):
            continue
        (b[lang]["test"] if TEST_RE.search(rel) else b[lang]["src"]).append(absf)
    return b


def lizard_ccn(files: list[str], lizard_bin: str | None) -> list[dict]:
    """Per-function CCN + length via lizard --csv. Returns list of dicts.

    Each row also carries the source ``file`` lizard reported it against
    (the CSV's 7th column) — added for #187's ``hotspots.py``, which needs
    per-FILE complexity (grouping these rows by ``file``) rather than the
    repo/language-wide distribution ``dist()`` computes. ``dist()`` and every
    existing caller ignore unknown dict keys, so this is additive: reusing
    ``lizard_ccn`` itself (CTR-fh-030's "complexity.lizard_ccn" reuse target)
    rather than a second lizard invocation path.
    """
    if not files or not lizard_bin:
        return []
    rows: list[dict] = []
    for i in range(0, len(files), 400):  # chunk to avoid arg limits
        chunk = files[i:i + 400]
        r = run(lizard_bin, "--csv", *chunk)
        for line in csv.reader(io.StringIO(r.stdout)):
            # lizard csv: nloc,ccn,token,param,length,location,file,func,longname,start,end
            if len(line) < 7:
                continue
            try:
                rows.append({
                    "nloc": int(line[0]), "ccn": int(line[1]), "length": int(line[4]),
                    "file": line[6],
                })
            except ValueError:
                continue
    return rows


def dist(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    ccn = sorted(r["ccn"] for r in rows)
    lens = [r["length"] for r in rows]
    n = len(ccn)
    return {
        "functions": n,
        "ccn_mean": round(sum(ccn) / n, 2),
        "ccn_max": ccn[-1],
        "ccn_p90": ccn[int(0.9 * (n - 1))],
        "ccn_p95": ccn[int(0.95 * (n - 1))],
        "pct_ccn_gt10": round(100 * sum(c > 10 for c in ccn) / n, 1),
        "pct_ccn_gt15": round(100 * sum(c > 15 for c in ccn) / n, 1),
        "pct_ccn_gt20": round(100 * sum(c > 20 for c in ccn) / n, 1),
        "len_mean": round(sum(lens) / n, 1),
        "pct_len_gt60": round(100 * sum(length > 60 for length in lens) / n, 1),
    }


def radon_mi(files: list[str], radon_bin: str | None) -> dict | None:
    if not files or not radon_bin:
        return None
    r = run(radon_bin, "mi", "-j", *files)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    mis = [v["mi"] for v in data.values() if isinstance(v, dict) and "mi" in v]
    if not mis:
        return None
    mis.sort()
    return {
        "files": len(mis), "mi_mean": round(sum(mis) / len(mis), 1),
        "mi_min": round(mis[0], 1),
        "pct_mi_lt65": round(100 * sum(m < 65 for m in mis) / len(mis), 1),
        "pct_mi_lt20": round(100 * sum(m < 20 for m in mis) / len(mis), 1),
    }


def gocognit_dist(files: list[str], gocognit_bin: str | None) -> dict | None:
    if not files or not gocognit_bin:
        return None
    vals: list[int] = []
    for i in range(0, len(files), 200):
        r = run(gocognit_bin, "-json", *files[i:i + 200])
        try:
            for item in json.loads(r.stdout or "[]"):
                vals.append(item["Complexity"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return {
        "functions": n, "cognitive_mean": round(sum(vals) / n, 2),
        "cognitive_max": vals[-1], "cognitive_p95": vals[int(0.95 * (n - 1))],
        "pct_gt15": round(100 * sum(v > 15 for v in vals) / n, 1),
    }


def complexipy_dist(files: list[str], complexipy_bin: str | None) -> dict | None:
    if not files or not complexipy_bin:
        return None
    vals: list[int] = []
    for i in range(0, len(files), 200):
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
            outp = tf.name
        run(complexipy_bin, "-q", "--output-format", "json", "--output", outp, *files[i:i + 200])
        try:
            with open(outp) as fh:
                for item in json.load(fh):
                    vals.append(item["complexity"])
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass
        finally:
            try:
                os.unlink(outp)
            except OSError:
                pass
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return {
        "functions": n, "cognitive_mean": round(sum(vals) / n, 2),
        "cognitive_max": vals[-1], "cognitive_p95": vals[int(0.95 * (n - 1))],
        "pct_gt15": round(100 * sum(v > 15 for v in vals) / n, 1),
    }


def loc_counts(files: list[str]) -> int:
    tot = 0
    for f in files:
        try:
            with open(f, "rb") as fh:
                tot += sum(1 for _ in fh)
        except OSError:
            pass
    return tot


def analyze(repo: str, venv: str | None = None, gobin: str | None = None) -> dict:
    """Compute the complexity/scale battery. Missing tools degrade gracefully."""
    lizard_bin = _tool("lizard", venv, gobin)
    radon_bin = _tool("radon", venv, gobin)
    gocognit_bin = _tool("gocognit", venv, gobin)
    complexipy_bin = _tool("complexipy", venv, gobin)

    if not lizard_bin:
        return {
            "repo": repo.rstrip("/").split("/")[-1],
            "skipped": "lizard not found",
            "note": "cyclomatic complexity requires lizard (pip install lizard)",
        }

    b = bucket(repo)
    langs: dict = {}
    src_loc_total = test_loc_total = 0
    for lang, sets in b.items():
        src_loc = loc_counts(sets["src"])
        test_loc = loc_counts(sets["test"])
        src_loc_total += src_loc
        test_loc_total += test_loc
        entry = {
            "src_files": len(sets["src"]), "test_files": len(sets["test"]),
            "src_loc": src_loc, "test_loc": test_loc,
            "cyclomatic_src": dist(lizard_ccn(sets["src"], lizard_bin)),
        }
        if lang == "python":
            entry["maintainability_index"] = radon_mi(sets["src"], radon_bin)
            entry["cognitive_src"] = complexipy_dist(sets["src"], complexipy_bin)
        elif lang == "go":
            entry["cognitive_src"] = gocognit_dist(sets["src"], gocognit_bin)
        langs[lang] = entry

    return {
        "repo": repo.rstrip("/").split("/")[-1],
        "src_loc_total": src_loc_total,
        "test_loc_total": test_loc_total,
        "test_ratio": round(test_loc_total / src_loc_total, 2) if src_loc_total else 0,
        "languages": langs,
        "tools": {
            "lizard": bool(lizard_bin), "radon": bool(radon_bin),
            "gocognit": bool(gocognit_bin), "complexipy": bool(complexipy_bin),
        },
        "notes": {
            "ccn_threshold": "McCabe: >10 moderate risk, >15 high, >20 very high; "
                             "CC is ~0.7-0.9 collinear with SLOC (Jay 2009) — read with size.",
            "mi_caveat": "Maintainability Index disputed (van Deursen 2014, Sjoberg 2013); "
                         "Python only, radon 0-100 scale, <65 flagged. Directional only.",
            "cognitive": "Campbell/SonarSource cognitive complexity; Go=gocognit, "
                         "Python=complexipy; TS has no maintained CLI (n/a). "
                         "Proxy for effort not correctness (Munoz Baron 2020).",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="code-quality complexity battery")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--venv", default=None, help="virtualenv with lizard/radon/complexipy")
    parser.add_argument("--gobin", default=None, help="dir containing gocognit")
    args = parser.parse_args()
    result = analyze(args.repo, venv=args.venv, gobin=args.gobin)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

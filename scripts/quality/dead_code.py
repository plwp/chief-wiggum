#!/usr/bin/env python3
"""dead_code.py — unused exports/symbols, per-language tier (#214).

Per-language tiers, mirroring the emitters layer's tiered posture:

  - **Python**: ``vulture`` when importable (the precise tier); else a
    conservative built-in AST pass. The built-in tier flags only module-level
    functions/classes in in-scope PRODUCTION files whose identifier appears
    exactly once (its own ``def``/``class`` line) across the identifier tokens
    of the ENTIRE repo population — pre-scope, tests included (detection
    repo-wide, authority in-scope: a use from a scope-excluded file is a use). Deliberate precision limits
    (conservative = fewer false positives, more misses):
      * any other mention counts as a use — strings, comments, ``__all__``
        entries, re-exports — so dynamically-dispatched code is under-flagged,
        never over-flagged;
      * decorated defs are skipped entirely (decorators routinely register
        the symbol with a framework — routes, CLI commands, fixtures);
      * underscore-prefixed and dunder names are skipped (private-by-
        convention symbols are not "exports"), as is ``main``.
  - **Go**: ``staticcheck`` when on PATH (its ``U1000``-class "unused"
    diagnostics, parsed from ``-f json``); else the Go tier is skipped and
    Go files are reported as unscanned.
  - **TypeScript/JavaScript**: ``knip`` when on PATH (unused exports/files,
    ``--reporter json``); else skipped/unscanned. ``tsc`` alone cannot report
    unused *exports* without per-repo config, so there is no tsc fallback.

Unscanned languages are REPORTED as unscanned file counts, never silently
empty (the code_query posture: absence of knowledge and proof of absence are
different answers). Findings carry ``file``, ``line``, ``symbol``, and the
``tier`` that produced them.

As a module:
    from quality.dead_code import analyze
    result = analyze("/path/to/repo")

As a CLI:
    python3 -m quality.dead_code <repo>
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import complexity, population

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SKIP_NAMES = {"main"}
STATICCHECK_TIMEOUT = 600
KNIP_TIMEOUT = 600


# --- Python tier --------------------------------------------------------------


def _python_candidates(rel: str, text: str) -> list[dict] | None:
    """Module-level def/class symbols eligible for the built-in dead check.
    None when the file doesn't parse (counted as unscanned by the caller)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.decorator_list:
            continue  # decorators routinely register the symbol externally
        name = node.name
        if name.startswith("_") or name in SKIP_NAMES:
            continue
        out.append({
            "file": rel,
            "line": node.lineno,
            "symbol": name,
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
        })
    return out


def _builtin_python_pass(
    repo: str, py_prod: list[str], corpus: list[str]
) -> tuple[list[dict], list[str]]:
    """Conservative AST pass: (findings, unparsable_files). A symbol is dead
    only when its identifier token appears exactly once (the definition)
    across the WHOLE corpus — any other mention, in any file, in any context,
    counts as a use."""
    token_counts: Counter = Counter()
    texts: dict[str, str] = {}
    for rel in corpus:
        try:
            texts[rel] = (Path(repo) / rel).read_text(errors="replace")
        except OSError:
            continue
        token_counts.update(IDENT_RE.findall(texts[rel]))

    findings: list[dict] = []
    unparsable: list[str] = []
    for rel in py_prod:
        text = texts.get(rel)
        if text is None:
            continue
        candidates = _python_candidates(rel, text)
        if candidates is None:
            unparsable.append(rel)
            continue
        for c in candidates:
            if token_counts.get(c["symbol"], 0) == 1:
                findings.append({**c, "tier": "builtin-ast"})
    return findings, unparsable


def _vulture_pass(repo: str, py_prod: list[str]) -> list[dict] | None:
    """The precise Python tier: vulture over the production .py population.
    None when vulture is not importable (caller falls back to builtin-ast)."""
    try:
        import vulture  # noqa: PLC0415 — optional tier dependency
    except ImportError:
        return None
    v = vulture.Vulture()
    v.scavenge([str(Path(repo) / rel) for rel in py_prod])
    findings: list[dict] = []
    for item in v.get_unused_code(min_confidence=60):
        rel = Path(os.path.relpath(str(item.filename), repo)).as_posix()
        findings.append({
            "file": rel,
            "line": int(item.first_lineno),
            "symbol": str(item.name),
            "kind": str(item.typ),
            "confidence": int(item.confidence),
            "tier": "vulture",
        })
    return findings


# --- Go tier ------------------------------------------------------------------


def _go_module_roots(repo: str) -> list[str]:
    """Tracked, non-vendored go.mod locations (repo-relative dirs; "." for the
    root). Go repos routinely keep the module in a subdir (backend/, cmd/…) —
    running ``staticcheck ./...`` from the repo root there yields only a
    'directory prefix . does not contain main module' compile diagnostic, which
    MUST read as unanalyzable, never as a clean scan (caught live on a real
    validation repo)."""
    r = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True)
    roots = []
    for f in r.stdout.splitlines():
        if (f == "go.mod" or f.endswith("/go.mod")) and not complexity.EXCLUDE_RE.search(f):
            roots.append(Path(f).parent.as_posix())
    return sorted(roots)


def _staticcheck_pass(repo: str) -> tuple[list[dict] | None, str | None, list[str]]:
    """(findings, skip_reason, warnings). Runs staticcheck per tracked go.mod
    module root. staticcheck absent / no module / every module failing to
    compile yields (None, reason, []) — the Go tier degrades to unscanned,
    never raises and never serves a compile failure as a clean result. A
    PARTIAL failure (some modules analyzed, some not) returns the findings it
    has plus a warning per failed module."""
    binary = shutil.which("staticcheck")
    if not binary:
        return None, "staticcheck not on PATH (go install honnef.co/go/tools/cmd/staticcheck@latest)", []
    roots = _go_module_roots(repo)
    if not roots:
        return None, "no tracked go.mod — staticcheck needs a Go module", []

    findings: list[dict] = []
    warnings: list[str] = []
    failed: list[str] = []
    for root in roots:
        try:
            proc = subprocess.run(
                [binary, "-checks", "U1000,U1001", "-f", "json", "./..."],
                cwd=str(Path(repo) / root), capture_output=True, text=True,
                timeout=STATICCHECK_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed.append(root)
            warnings.append(f"module {root}: staticcheck failed to run: {exc}")
            continue
        module_findings: list[dict] = []
        compile_errors: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = str(d.get("code", ""))
            msg = str(d.get("message", ""))
            if code == "compile":
                compile_errors.append(msg)
                continue
            if not code.startswith("U"):
                continue
            loc = d.get("location") or {}
            loc_file = str(loc.get("file", ""))
            if not Path(loc_file).is_absolute():
                loc_file = str(Path(repo) / root / loc_file)
            rel = Path(os.path.relpath(loc_file, repo)).as_posix()
            m = re.search(r"(?:func|type|var|const|field|method|struct)\s+([A-Za-z_][\w.]*)", msg)
            module_findings.append({
                "file": rel,
                "line": int(loc.get("line", 0) or 0),
                "symbol": m.group(1) if m else msg[:60],
                "kind": "unused",
                "message": msg[:160],
                "tier": "staticcheck",
            })
        if compile_errors and not module_findings:
            # Can't tell "clean" from "unanalyzable" — never serve the latter
            # as the former.
            failed.append(root)
            warnings.append(f"module {root}: staticcheck could not analyze: {compile_errors[0][:160]}")
            continue
        if compile_errors:
            warnings.append(
                f"module {root}: {len(compile_errors)} package(s) failed to compile — "
                "findings are partial for this module"
            )
        findings.extend(module_findings)
    if len(failed) == len(roots):
        return None, f"staticcheck analyzed no module: {warnings[0] if warnings else 'unknown'}", []
    return findings, None, warnings


# --- TS/JS tier ---------------------------------------------------------------


def _knip_pass(repo: str) -> tuple[list[dict] | None, str | None]:
    """(findings, skip_reason). knip absent / no package.json / failing run
    yields (None, reason)."""
    binary = shutil.which("knip")
    if not binary:
        return None, "knip not on PATH (npm i -g knip)"
    if not (Path(repo) / "package.json").is_file():
        return None, "no package.json at the repo root — knip needs a JS/TS project"
    try:
        proc = subprocess.run(
            [binary, "--reporter", "json", "--no-exit-code"],
            cwd=repo, capture_output=True, text=True, timeout=KNIP_TIMEOUT,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return None, f"knip failed to run/parse: {exc}"
    findings: list[dict] = []
    for f in data.get("files", []) or []:
        rel = Path(os.path.relpath(str(Path(repo) / str(f)), repo)).as_posix()
        findings.append({
            "file": rel, "line": 1, "symbol": Path(rel).name,
            "kind": "unused_file", "tier": "knip",
        })
    for issue in data.get("issues", []) or []:
        rel = str(issue.get("file", ""))
        for bucket in ("exports", "types"):
            for exp in issue.get(bucket, []) or []:
                findings.append({
                    "file": rel,
                    "line": int(exp.get("line", 0) or 0),
                    "symbol": str(exp.get("name", "")),
                    "kind": f"unused_{bucket[:-1]}",
                    "tier": "knip",
                })
    return findings, None


# --- composition --------------------------------------------------------------


def analyze(repo: str, path_filter=None) -> dict:
    """Per-language dead-symbol findings over the #213-scoped population.

    Scope doctrine (same as check_single_writer): **detection repo-wide,
    authority in-scope**. The USE corpus is always the FULL repo population
    (pre-scope) — a symbol used from an out-of-scope file is NOT dead —
    while findings are emitted only for in-scope files. The staticcheck and
    knip tiers already see the whole module/project; only their FINDINGS are
    re-filtered to the population rules, never their evidence.

    Every language present in the population is accounted for: scanned (a
    tier ran), or counted in ``unscanned`` with the skip reason — never
    silently empty.
    """
    files = population.tracked_source(repo, path_filter=path_filter)
    # Detection corpus: the full-repo population, pre-scope. Identical to
    # `files` when no scope filter applies.
    corpus = population.tracked_source(repo) if path_filter is not None else files
    by_lang: dict[str, list[str]] = {}
    for f in files:
        by_lang.setdefault(population.lang_of(f), []).append(f)

    languages: dict[str, dict] = {}
    unscanned: dict[str, int] = {}
    findings: list[dict] = []

    # Python
    py_all = by_lang.get("python", [])
    if py_all:
        py_prod = [f for f in py_all if not population.is_test_file(f)]
        # Vulture scavenges the PRE-SCOPE production population so a use from
        # an excluded file counts as a use; findings then narrow to in-scope.
        py_prod_corpus = [
            f for f in corpus
            if population.lang_of(f) == "python" and not population.is_test_file(f)
        ]
        vul = _vulture_pass(repo, py_prod_corpus)
        if vul is not None:
            in_scope_prod = set(py_prod)
            py_findings = [x for x in vul if x["file"] in in_scope_prod]
            languages["python"] = {"tier": "vulture", "files": len(py_all),
                                   "findings": len(py_findings)}
        else:
            py_findings, unparsable = _builtin_python_pass(repo, py_prod, corpus)
            languages["python"] = {
                "tier": "builtin-ast", "files": len(py_all),
                "findings": len(py_findings), "unparsable": unparsable,
                "note": (
                    "conservative built-in pass (vulture not importable): flags only "
                    "un-decorated module-level defs/classes whose identifier never "
                    "appears anywhere else in the full repo population (pre-scope) — "
                    "under-reports by design; install vulture for the precise tier"
                ),
            }
        findings.extend(py_findings)

    # Go
    go_all = by_lang.get("go", [])
    if go_all:
        sc, reason, sc_warnings = _staticcheck_pass(repo)
        if sc is None:
            languages["go"] = {"skipped": reason, "files": len(go_all)}
            unscanned["go"] = len(go_all)
        else:
            # staticcheck walks the whole module — re-apply the population
            # rules (prod-only, in-scope, non-generated) to its output.
            go_findings = [
                x for x in sc
                if population.lang_of(x["file"]) == "go"
                and not population.is_test_file(x["file"])
                and not population.GENERATED_RE.search(x["file"])
                and (path_filter is None or path_filter(x["file"]))
            ]
            languages["go"] = {"tier": "staticcheck", "files": len(go_all),
                               "findings": len(go_findings)}
            if sc_warnings:
                languages["go"]["warnings"] = sc_warnings
            findings.extend(go_findings)

    # TS / JS (knip covers both in one project pass)
    ts_all = by_lang.get("typescript", []) + by_lang.get("javascript", [])
    if ts_all:
        kn, reason = _knip_pass(repo)
        if kn is None:
            for lang in ("typescript", "javascript"):
                if by_lang.get(lang):
                    languages[lang] = {"skipped": reason, "files": len(by_lang[lang])}
                    unscanned[lang] = len(by_lang[lang])
        else:
            ts_findings = [
                x for x in kn
                if (path_filter is None or path_filter(x["file"]))
                and not population.is_test_file(x["file"])
            ]
            for lang in ("typescript", "javascript"):
                if by_lang.get(lang):
                    languages[lang] = {"tier": "knip", "files": len(by_lang[lang])}
            if languages.get("typescript"):
                languages["typescript"]["findings"] = len(ts_findings)
            elif languages.get("javascript"):
                languages["javascript"]["findings"] = len(ts_findings)
            findings.extend(ts_findings)

    # Languages that ARE in the population but have no dead-code tier here
    # (C# today). Without this they would be counted in files_in_population,
    # absent from `languages`, AND absent from `unscanned` — a zero-finding
    # inventory over a non-zero population, which reads as health and defeats
    # /status's NOT MEASURED marker. That is #259's own failure mode one layer
    # down, so it is stated rather than inferred (codex review, #259).
    for lang, lang_files in sorted(by_lang.items()):
        if lang is None or lang in languages:
            continue
        languages[lang] = {
            "skipped": f"no dead-code tier for {lang}",
            "files": len(lang_files),
        }
        unscanned[lang] = len(lang_files)

    # Files whose extension maps to no known language never entered the
    # population at all — surface them so an unsupported language is a
    # visible gap, not a silent omission (codex review, #214).
    for ext, n in population.unknown_language_files(repo, path_filter).items():
        unscanned[f"unknown-language ({ext})"] = n

    return {
        "engine": "dead_code",
        "files_in_population": len(files),
        "languages": languages,
        "unscanned": unscanned,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="unused exports/symbols, per-language tier")
    parser.add_argument("repo", help="path to the git repository")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

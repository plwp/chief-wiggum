#!/usr/bin/env python3
"""test_health.py — orphaned / assertion-free / quarantined tests (#214).

Generalizes the gate↔test discipline ``check_cw_standards.py`` applies to the
CW repo ("gates are load-bearing; an untested gate is a gate you can't trust")
to TARGET repos, from the other direction: tests that no longer earn their
keep.

Three mechanical checks, all name-convention/AST/regex tiers (no imports, no
test execution):

  - **orphaned_test** — a test file whose apparent SUBJECT no longer exists,
    under a conservative per-language mapping (reported verbatim in the
    result's ``mapping`` field so a consumer can judge the inference):
      * Python: ``test_<x>.py`` / ``<x>_test.py`` -> some tracked source file
        or directory named ``<x>`` anywhere in the population. Generic stems
        (integration, e2e, smoke, cli, utils, …) are never flagged.
      * Go: ``<x>_test.go`` -> flagged only when ``<x>.go`` is absent from the
        same directory AND the directory has no non-test ``.go`` at all (Go
        tests legitimately test the package, not one file).
      * TS/JS: ``<x>.test.ts`` / ``<x>.spec.tsx`` etc. -> a tracked source
        file with stem ``<x>``, same directory first, else anywhere.
  - **assertion_free_test** — a test function containing no assertion:
      * Python (ast): no ``assert`` statement and no call whose name contains
        an assertion-ish token (assert/raises/warns/fail/check/verify/expect/
        validate/approx) anywhere in the function body. Helper-based suites
        using other naming will under-report — conservative by design.
      * Go (regex tier): a ``func TestXxx`` body with no ``t.Error/t.Fatal/
        t.Fail`` and no assertion-ish call (assert./require./check/verify/
        expect).
      * JS/TS: not scanned in v1 (reported in ``unscanned``).
  - **skipped_test** — quarantined suites: ``@pytest.mark.skip``/``skipif``,
    ``pytest.skip(``, ``@unittest.skip``, Go ``t.Skip*``, JS ``describe/it/
    test .skip(`` and ``xit(``/``xdescribe(``.

Pure Python + git; nothing external to degrade on. Unparsable Python test
files are counted, never silently dropped.

As a module:
    from quality.test_health import analyze
    result = analyze("/path/to/repo")

As a CLI:
    python3 -m quality.test_health <repo>
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from . import population

# Stems that never map to a single subject module — integration/e2e/meta
# suites named for a BEHAVIOR, not a module.
GENERIC_STEMS = {
    "integration", "e2e", "smoke", "acceptance", "regression", "sanity",
    "cli", "main", "app", "api", "utils", "util", "helpers", "helper",
    "common", "base", "all", "misc", "conftest", "fixtures", "setup",
}

MAPPING = {
    "python": "tests/test_<x>.py or <x>_test.py -> tracked file/dir named <x> anywhere",
    "go": "<x>_test.go -> <x>.go in the same dir, or ANY non-test .go in that dir (package tests)",
    "typescript": (
        "<x>.test|spec.ts(x) -> tracked source with stem <x> anywhere; specs under a "
        "standalone tests/ or e2e|playwright|cypress/ dir are FLOW tests (named for a "
        "behavior, not a module) and are never mapped — only colocated/__tests__ specs are"
    ),
    "javascript": (
        "<x>.test|spec.js(x) -> tracked source with stem <x> anywhere; standalone "
        "tests//e2e-dir specs are flow tests, never mapped"
    ),
}

# TS/JS specs under these dirs are end-to-end/flow suites named for the flow
# they exercise (checkout-flow.spec.ts) — mapping them to a module is a false
# positive by construction (observed live on a real validation repo's
# playwright suite). `__tests__/` is the COLOCATED unit convention and still
# maps.
_JS_FLOW_DIR_RE = re.compile(r"(^|/)(tests?|e2e|playwright|cypress)/")

ASSERTIONISH_RE = re.compile(
    r"(assert|raises|warns|fail|check|verify|expect|validate|approx)", re.IGNORECASE
)
GO_ASSERT_RE = re.compile(
    r"\bt\.(Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|"
    r"\b(assert|require)\.|"
    r"(?i:(check|verify|expect|assert)\w*\()",
)
GO_FUNC_RE = re.compile(r"^func\s+(Test\w+)\s*\(", re.MULTILINE)
# Helper delegation: a call passing `t` (or `s.t`/`&s.t`) as an argument —
# `runScenario(t, tc)`, `h.check(s.t, got)`. Such a test DELEGATES its
# assertions to a local helper (a real mined FP class: helper calls require.* on
# the caller's t); it is never flagged assertion-free, and never verified
# either — counted under the `helper_delegated` bucket, stated as unverified.
# `func(` is excluded so a t.Run closure signature alone doesn't count as
# delegation.
GO_HELPER_DELEGATE_RE = re.compile(r"\b(?!func\b)\w+\s*\(\s*(&?\w+\.)?t\s*[,)]")

SKIP_PATTERNS = {
    "python": re.compile(r"@pytest\.mark\.skip(if)?\b|pytest\.skip\(|@unittest\.skip"),
    "go": re.compile(r"\bt\.Skip(f|Now)?\("),
    "typescript": re.compile(r"\b(describe|it|test)\.skip\(|\bxit\(|\bxdescribe\("),
    "javascript": re.compile(r"\b(describe|it|test)\.skip\(|\bxit\(|\bxdescribe\("),
}


# --- orphaned tests -----------------------------------------------------------


def _test_stem(rel: str) -> str | None:
    """The apparent subject stem of a test file, or None when the name doesn't
    follow a subject-mapping convention."""
    name = Path(rel).name
    m = re.match(r"^test_(.+)\.py$", name)
    if m:
        return m.group(1)
    m = re.match(r"^(.+)_test\.(py|go)$", name)
    if m:
        return m.group(1)
    m = re.match(r"^(.+)\.(test|spec)\.[tj]sx?$", name)
    if m:
        return m.group(1)
    return None


def _find_orphans(corpus: list[str], scope: set[str] | None = None) -> list[dict]:
    """Orphan findings under the doctrine **detection repo-wide, authority
    in-scope** (same as check_single_writer): the EXISTENCE corpus — which
    subjects/stems/package dirs exist — is the FULL pre-scope population
    (``corpus``), so a test whose subject lives in a scope-excluded path is
    NOT orphaned. Findings are emitted only for test files in ``scope``
    (``None`` = everything)."""
    non_test = [f for f in corpus if not population.is_test_file(f)]
    non_test_set = set(non_test)
    stems = {Path(f).stem for f in non_test}
    dir_parts = {part for f in corpus for part in Path(f).parts[:-1]}
    by_dir_nontest_go = {}
    for f in non_test:
        if f.endswith(".go"):
            by_dir_nontest_go.setdefault(str(Path(f).parent), True)

    orphans: list[dict] = []
    for rel in corpus:
        if not population.is_test_file(rel):
            continue
        if scope is not None and rel not in scope:
            continue  # authority in-scope: never flag an out-of-scope test
        stem = _test_stem(rel)
        if stem is None or stem.lower() in GENERIC_STEMS:
            continue
        lang = population.lang_of(rel)
        if lang in ("typescript", "javascript") and _JS_FLOW_DIR_RE.search(rel):
            continue  # standalone-suite flow specs have no module mapping
        if lang == "go":
            same_dir = str(Path(rel).parent)
            subject = (Path(same_dir) / f"{stem}.go").as_posix()
            if subject in non_test_set:
                continue
            if by_dir_nontest_go.get(same_dir):
                continue  # package still has production code — tests the package
        else:
            # Subject exists as a source file stem or as a package directory
            # anywhere in the population.
            if stem in stems or stem in dir_parts:
                continue
            # tolerate simple plural/singular drift (orders_test -> order.py)
            if stem.rstrip("s") in stems or (stem + "s") in stems:
                continue
        orphans.append({
            "file": rel,
            "line": 1,
            "kind": "orphaned_test",
            "symbol": Path(rel).name,
            "subject_stem": stem,
            "mapping": MAPPING[lang],
        })
    return orphans


# --- assertion-free tests -----------------------------------------------------


def _call_names(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                yield fn.attr
            elif isinstance(fn, ast.Name):
                yield fn.id


def _python_assertion_free(rel: str, text: str) -> tuple[list[dict], bool]:
    """(findings, parsed_ok) for one Python test file."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], False

    def check_fn(fn: ast.AST, qual: str) -> dict | None:
        for sub in ast.walk(fn):
            if isinstance(sub, (ast.Assert, ast.Raise)):
                return None
        for name in _call_names(fn):
            if ASSERTIONISH_RE.search(name):
                return None
        return {
            "file": rel, "line": fn.lineno, "kind": "assertion_free_test",
            "symbol": qual,
        }

    findings: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            f = check_fn(node, node.name)
            if f:
                findings.append(f)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    f = check_fn(sub, f"{node.name}.{sub.name}")
                    if f:
                        findings.append(f)
    return findings, True


def _go_assertion_free(rel: str, text: str) -> tuple[list[dict], int]:
    """Regex tier: ``(findings, helper_delegated)`` — each top-level ``func
    TestXxx`` body without an assertion-ish call is a finding UNLESS it passes
    ``t`` to a local function (helper delegation, the known FP class caught on
    a real Go corpus): those are counted, not flagged — the delegation is
    stated, the helper's assertions are unverified. Bodies are delimited by
    the next top-level ``func``."""
    findings: list[dict] = []
    helper_delegated = 0
    matches = list(GO_FUNC_RE.finditer(text))
    all_funcs = list(re.finditer(r"^func\s", text, re.MULTILINE))
    for m in matches:
        start = m.end()
        end = len(text)
        for f in all_funcs:
            if f.start() > m.start():
                end = f.start()
                break
        body = text[start:end]
        if not GO_ASSERT_RE.search(body):
            if GO_HELPER_DELEGATE_RE.search(body):
                helper_delegated += 1
                continue
            line = text.count("\n", 0, m.start()) + 1
            findings.append({
                "file": rel, "line": line, "kind": "assertion_free_test",
                "symbol": m.group(1),
            })
    return findings, helper_delegated


# --- skipped / quarantined ----------------------------------------------------


def _skipped(rel: str, text: str, lang: str) -> list[dict]:
    pat = SKIP_PATTERNS.get(lang)
    if pat is None:
        return []
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pat.search(line):
            findings.append({
                "file": rel, "line": lineno, "kind": "skipped_test",
                "symbol": line.strip()[:80],
            })
    return findings


def assertion_scan_gap(engines: dict) -> str | None:
    """One shared line for this engine's ``unscanned.assertion_scan`` note,
    taking a debt envelope's ``engines`` sub-dict — every debt-rendering
    surface (debt_inventory's report, quality_slop_gate's debt block,
    quality/report.py's debt section) prints the gap rather than silently
    implying JS/TS assertion health."""
    th = (engines or {}).get("test_health") or {}
    gap = (th.get("unscanned") or {}).get("assertion_scan") or {}
    if not gap:
        return None
    return (
        "assertion scan not run (test_health): "
        + ", ".join(f"{k}: {v} file(s)" for k, v in sorted(gap.items()))
        + " — absence of a finding there is not evidence of health"
    )


# --- composition --------------------------------------------------------------


def analyze(repo: str, path_filter=None) -> dict:
    files = population.tracked_source(repo, path_filter=path_filter)
    test_files = [f for f in files if population.is_test_file(f)]
    # Detection repo-wide, authority in-scope: subject EXISTENCE is judged
    # against the full pre-scope population, findings only for in-scope tests.
    corpus = population.tracked_source(repo) if path_filter is not None else files

    findings: list[dict] = list(
        _find_orphans(corpus, scope=set(files) if path_filter is not None else None)
    )
    unparsable: list[str] = []
    unscanned_assertion_langs: dict[str, int] = {}
    helper_delegated_go = 0

    for rel in test_files:
        lang = population.lang_of(rel)
        try:
            text = (Path(repo) / rel).read_text(errors="replace")
        except OSError:
            unparsable.append(rel)
            continue
        if lang == "python":
            fnd, ok = _python_assertion_free(rel, text)
            if not ok:
                unparsable.append(rel)
            findings.extend(fnd)
        elif lang == "go":
            fnd, delegated = _go_assertion_free(rel, text)
            helper_delegated_go += delegated
            findings.extend(fnd)
        else:
            unscanned_assertion_langs[lang] = unscanned_assertion_langs.get(lang, 0) + 1
        findings.extend(_skipped(rel, text, lang))

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {
        "engine": "test_health",
        "test_files": len(test_files),
        "mapping": MAPPING,
        "counts_by_kind": counts,
        "unparsable": unparsable,
        "helper_delegated": {
            "go": helper_delegated_go,
            "note": (
                "Go test functions with no direct assertion that pass t to a "
                "local helper — delegation is stated, the helper's assertions "
                "are UNVERIFIED (never flagged assertion-free, never proven "
                "healthy; see docs/debt-inventory.md authority boundary)"
            ),
        },
        "unscanned": {
            "assertion_scan": unscanned_assertion_langs,
            "note": "JS/TS assertion-freeness is not scanned in v1 — absence of a finding there is not evidence of health",
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="orphaned / assertion-free / skipped tests")
    parser.add_argument("repo", help="path to the git repository")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

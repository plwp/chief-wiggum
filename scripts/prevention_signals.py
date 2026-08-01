#!/usr/bin/env python3
"""prevention_signals.py — diff-scoped slop signals for the review context (#216).

The prevention leg of the remediation loop: while `/plan-epic --from-debt`
drains EXISTING debt, this stops NEW slop at write time — as **reviewer
information only**. Given a diff range (``--base``), it emits three
diff-scoped signals:

  (a) **new_duplication** — the diff clones existing code: the clones engine
      (jscpd via the shared #214 runner) runs live at HEAD, and any clone
      class with at least one member span inside the diff's ADDED lines *and*
      at least one member outside them means an added hunk duplicates a span
      that already existed. Skipped (stated) when jscpd/node is absent.
  (b) **dead_code_introduced** — added exports unused anywhere: the
      dead-code engine's conservative ``builtin-ast`` tier runs on the
      changed Python files with the identifier corpus **repo-wide**
      (detection repo-wide, as everywhere in #214), keeping only symbols
      whose ``def`` line is an added line. Non-Python changed files are
      counted unscanned, never silently clean.
  (c) **assertion_free_tests_added** — test functions added by this diff
      that assert nothing (test_health's Python-AST / Go-regex tiers on the
      changed test files, filtered to added lines). JS/TS is unscanned in v1
      and reported as such.

**NEVER blocking**: this script always exits 0, has no ``--gate`` flag, and
its authority line says so. Promotion to a blocking gate would require the
full ``docs/gate-validation.md`` protocol first (chief-wiggum#216 explicitly
ships it report-only per ``docs/gate-rollout.md``).

Usage:
    python3 scripts/prevention_signals.py [owner/repo] [--repo PATH]
        --base REF [--workdir DIR] [--format text|json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402
from debt_inventory import resolve_target  # noqa: E402
from quality import clones, dead_code, population, test_health  # noqa: E402

SCHEMA = "prevention-signals/1"

AUTHORITY = (
    "diff-scoped reviewer signals over {base}...HEAD; report-only, NEVER "
    "blocking (always exit 0) — promotion to a gate requires the "
    "docs/gate-validation.md protocol first. Absence of a signal in an "
    "unscanned language is NOT evidence of health."
)

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# --- diff parsing -------------------------------------------------------------


_C_ESCAPES = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11,
              '"': 34, "\\": 92}


def unquote_git_path(name: str) -> str:
    """Undo git's C-style path quoting (#216 F7): with ``core.quotepath``
    default-on, a path with spaces/unicode arrives as ``"b/p\\303\\244 th.py"``
    — octal byte escapes inside double quotes. An unquoted name passes
    through unchanged."""
    if not (name.startswith('"') and name.endswith('"') and len(name) >= 2):
        return name
    body = name[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            break
        e = body[i]
        if e in "01234567":
            out.append(int(body[i:i + 3], 8))
            i += 3
        else:
            out.append(_C_ESCAPES.get(e, ord(e)))
            i += 1
    return out.decode("utf-8", errors="replace")


def parse_added_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """file -> [(start, end)] 1-indexed inclusive ADDED-line ranges in the new
    file, from ``git diff --unified=0`` output. Deleted-only hunks (length 0)
    add no range. C-style quoted paths (spaces/unicode under git's default
    ``core.quotepath``) are unquoted first (#216 F7)."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            name = unquote_git_path(line[4:].strip())
            current = None if name == "/dev/null" else name.removeprefix("b/")
            continue
        m = HUNK_RE.match(line)
        if m and current is not None:
            start = int(m.group(1))
            length = int(m.group(2)) if m.group(2) is not None else 1
            if length > 0:
                out.setdefault(current, []).append((start, start + length - 1))
    return out


def _in_added(added: dict[str, list[tuple[int, int]]], file: str, line: int) -> bool:
    return any(s <= line <= e for s, e in added.get(file, []))


def _overlaps_added(added: dict[str, list[tuple[int, int]]], file: str,
                    start: int, end: int) -> bool:
    return any(s <= end and start <= e for s, e in added.get(file, []))


def _git_diff(repo: str, base: str) -> str:
    proc = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")
    return proc.stdout


# --- (a) new duplication ------------------------------------------------------


def duplication_findings(clone_classes: list[dict],
                         added: dict[str, list[tuple[int, int]]]) -> list[dict]:
    """Clone classes where >=1 member span sits inside the added lines and
    >=1 member sits outside them — the diff duplicated existing code. Pure
    function of (classes, added) so it is testable without jscpd."""
    findings = []
    for cls in clone_classes:
        new_spans, existing_spans = [], []
        for m in cls.get("members") or []:
            span = f"{m['file']}:{m['start_line']}-{m['end_line']}"
            if _overlaps_added(added, m["file"], m["start_line"], m["end_line"]):
                new_spans.append(span)
            else:
                existing_spans.append(span)
        if new_spans and existing_spans:
            findings.append({
                "content_hash": cls.get("content_hash"),
                "lines": cls.get("lines"),
                "added_spans": sorted(new_spans),
                "existing_spans": sorted(existing_spans),
            })
    findings.sort(key=lambda f: (f["content_hash"] or ""))
    return findings


def _new_duplication(repo: str, workdir: str,
                     added: dict[str, list[tuple[int, int]]]) -> dict:
    result = clones.analyze(repo, os.path.join(workdir, "jscpd"))
    if result.get("skipped"):
        return {"skipped": result["skipped"],
                "note": "duplication signal not computed — absence is stated, not health"}
    findings = duplication_findings(result.get("clone_classes") or [], added)
    return {"findings": findings, "clone_classes_scanned": len(result.get("clone_classes") or [])}


# --- (b) dead code introduced -------------------------------------------------


def _dead_code_introduced(repo: str, added: dict[str, list[tuple[int, int]]]) -> dict:
    changed_files = sorted(added)
    py_prod = [f for f in changed_files
               if population.lang_of(f) == "python" and not population.is_test_file(f)
               and (Path(repo) / f).is_file()]
    unscanned: dict[str, int] = {}
    for f in changed_files:
        lang = population.lang_of(f)
        if lang and lang != "python" and not population.is_test_file(f):
            unscanned[lang] = unscanned.get(lang, 0) + 1
    if not py_prod:
        return {"tier": "builtin-ast", "findings": [], "unscanned": unscanned}
    corpus = population.tracked_source(repo)  # detection repo-wide, always
    # F7: git ls-files C-quotes unusual paths too, so a changed file with a
    # unicode/space name may be absent from the corpus even though the diff
    # side has been unquoted — the changed files themselves must always be in
    # the scan corpus (their own def tokens need counting).
    corpus = sorted(set(corpus) | set(py_prod))
    all_findings, _unparsable = dead_code._builtin_python_pass(repo, py_prod, corpus)
    findings = [f for f in all_findings if _in_added(added, f["file"], f["line"])]
    return {
        "tier": "builtin-ast",
        "note": ("conservative built-in tier on changed files, identifier corpus "
                 "repo-wide — under-reports by design"),
        "findings": findings,
        "unscanned": unscanned,
    }


# --- (c) assertion-free tests added -------------------------------------------


def _assertion_free_added(repo: str, added: dict[str, list[tuple[int, int]]]) -> dict:
    findings: list[dict] = []
    unscanned: dict[str, int] = {}
    for f in sorted(added):
        if not population.is_test_file(f) or not (Path(repo) / f).is_file():
            continue
        lang = population.lang_of(f)
        try:
            text = (Path(repo) / f).read_text(errors="replace")
        except OSError:
            continue
        if lang == "python":
            fnd, _ok = test_health._python_assertion_free(f, text)
        elif lang == "go":
            fnd, _delegated = test_health._go_assertion_free(f, text)
        else:
            if lang:
                unscanned[lang] = unscanned.get(lang, 0) + 1
            continue
        findings.extend(x for x in fnd if _in_added(added, x["file"], x["line"]))
    return {"findings": findings, "unscanned": unscanned}


# --- composition --------------------------------------------------------------


def build_signals(repo: str, base: str, workdir: str) -> dict:
    added = parse_added_ranges(_git_diff(repo, base))
    # F7: a changed path that does not resolve in the working tree (a quoting
    # form we failed to decode, or a path outside the checkout) is counted as
    # UNSCANNED with a reason — never silently treated as clean.
    unresolved = sorted(f for f in added if not (Path(repo) / f).is_file())
    signals = {
        "new_duplication": _new_duplication(repo, workdir, added),
        "dead_code_introduced": _dead_code_introduced(repo, added),
        "assertion_free_tests_added": _assertion_free_added(repo, added),
    }
    counts = {
        name: (len(sig.get("findings") or []) if not sig.get("skipped") else None)
        for name, sig in signals.items()
    }
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "target_sha": artifacts.head_sha(repo),
        "authority": AUTHORITY.format(base=base),
        "changed_files": len(added),
        "unscanned_files": {
            "count": len(unresolved),
            "files": unresolved,
            "reason": ("changed path did not resolve in the working tree "
                       "(quoted/renamed/deleted form?) — not scanned, not clean"),
        },
        "counts": counts,
        "signals": signals,
    }


def format_report(env: dict) -> str:
    lines = ["## Prevention signals (report-only — reviewer information, never blocking)"]
    lines.append(env["authority"])
    lines.append("")
    dup = env["signals"]["new_duplication"]
    if dup.get("skipped"):
        lines.append(f"- new duplication: skipped — {dup['skipped']}")
    elif dup["findings"]:
        lines.append(f"- new duplication: {len(dup['findings'])} clone class(es) "
                     "where this diff copies EXISTING code:")
        for f in dup["findings"]:
            lines.append(f"    {f['content_hash']} (~{f['lines']} lines): added "
                         f"{', '.join(f['added_spans'])} duplicates existing "
                         f"{', '.join(f['existing_spans'])}")
    else:
        lines.append("- new duplication: none detected")
    dc = env["signals"]["dead_code_introduced"]
    if dc["findings"]:
        lines.append(f"- dead code introduced ({dc['tier']}): {len(dc['findings'])} "
                     "added symbol(s) unused anywhere:")
        for f in dc["findings"]:
            lines.append(f"    {f['file']}:{f['line']} {f['symbol']}")
    else:
        lines.append(f"- dead code introduced ({dc['tier']}): none detected")
    if dc.get("unscanned"):
        uns = ", ".join(f"{k}: {v} file(s)" for k, v in sorted(dc["unscanned"].items()))
        lines.append(f"    (unscanned changed files: {uns})")
    af = env["signals"]["assertion_free_tests_added"]
    if af["findings"]:
        lines.append(f"- assertion-free tests added: {len(af['findings'])}:")
        for f in af["findings"]:
            lines.append(f"    {f['file']}:{f['line']} {f['symbol']}")
    else:
        lines.append("- assertion-free tests added: none detected")
    if af.get("unscanned"):
        uns = ", ".join(f"{k}: {v} file(s)" for k, v in sorted(af["unscanned"].items()))
        lines.append(f"    (assertion scan not run for: {uns})")
    unres = env.get("unscanned_files") or {}
    if unres.get("count"):
        lines.append(f"- unscanned changed path(s): {unres['count']} — "
                     f"{unres['reason']}:")
        for f in unres["files"]:
            lines.append(f"    {f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="diff-scoped slop signals for the review context "
                    "(report-only, never blocking)")
    parser.add_argument("owner_repo", nargs="?", default=None)
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--base", required=True, help="diff base ref (base...HEAD)")
    parser.add_argument("--workdir", default=None, help="scratch dir for jscpd output")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    target = resolve_target(args.owner_repo, args.repo)
    if args.workdir:
        workdir = args.workdir
    else:
        import env  # session temp dir under ~/.chief-wiggum/tmp — never the target repo
        workdir = os.path.join(str(env.create_tmp()), "prevention", Path(target).name)

    try:
        envelope = build_signals(target, args.base, workdir)
    except Exception as exc:  # noqa: BLE001 — C4: NO failure may vanish
        # Even a broken diff range — or any unexpected engine crash — must
        # not block a workflow that calls this unconditionally, AND must not
        # leave the review context empty-handed: emit an honest error block
        # on stdout (the review context) and exit 0 (report-only, always).
        note = (f"prevention signals unavailable: {exc} — treat as not-run, "
                "not clean")
        if args.format == "json":
            print(json.dumps({"schema": SCHEMA, "base": args.base,
                              "error": str(exc), "note": note}, indent=2))
        else:
            print("## Prevention signals — ERROR (report-only)")
            print(note)
        print(f"prevention_signals: {exc}", file=sys.stderr)
        return 0

    if args.format == "json":
        print(json.dumps(envelope, indent=2))
    else:
        print(format_report(envelope))
    return 0  # NEVER blocking


if __name__ == "__main__":
    sys.exit(main())

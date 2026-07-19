#!/usr/bin/env python3
"""DST-readiness scanner (#167): flags nondeterminism-shaped calls before they calcify.

Deterministic-simulation testing (FoundationDB/TigerBeetle/Antithesis-style) is only
cheap if determinism is designed in from day one — a wall-clock read or an unseeded
random call sprinkled through business logic makes a repo un-simulatable, and by the
time anyone notices, it is everywhere. Chief Wiggum builds repos from scratch, so this
is the one window where the fix is "put it behind a seam" instead of "rewrite half the
codebase". This checker is advisory: it stamps the opportunity into new products via
`/architect` (see the "DST readiness" step there) and reports violations here — it does
not, by itself, block anything.

Rules (regex tier, language-aware, mirrors ``check_single_writer.py``'s style of
line-scanning with comment stripping):

- **wall-clock** — a direct wall-clock read outside a designated clock seam:
  Go ``time.Now``; Python ``datetime.now``, ``datetime.utcnow``, ``time.time``;
  TS/JS ``Date.now`` and no-arg ``new Date`` construction.
- **unseeded-random** — an unseeded, default-source random call outside a designated
  random seam: Go ``math/rand`` package-level calls (``rand.Intn(``, ``rand.Float64(``,
  etc — deliberately not trying to detect "is there a seeded *rand.Rand nearby", that's
  too clever for a regex tier); Python module-level ``random.<fn>(`` calls (excluding
  ``random.seed(``, which is the seam pattern, not a violation of it); JS/TS
  ``Math.random(``.
- **IO in non-designated modules is OUT OF SCOPE for v1.** Grepping for "any file
  read/network call/db call outside package X" is far noisier than the two rules above
  (imports, wrapper libraries, and legitimate framework glue all look like violations)
  and needs project-specific knowledge of which packages are the designated IO seam.
  Revisit once the clock/random tier has proven itself on real repos.

Allowlist (a file is exempt from every rule, not scanned at all):

- its path matches a **seam glob** — default ``**/clock*``, ``**/rand*``,
  ``**/telemetry/**`` (a repo can add more via ``--config``'s ``seams`` list; additions
  are unioned with, not a replacement for, the defaults);
- it carries a ``# cw:dst-exempt`` / ``// cw:dst-exempt`` line comment in its first 20
  lines;
- it is a test file, by the same heuristic ``check_traceability.py`` uses ("test" or
  "spec" in the path, or an ``e2e`` path segment).

**Authority boundary**: this flags nondeterminism-shaped calls in scanned first-party
code; it does not prove dependencies or unscanned files are deterministic.

**Advisory FOREVER by default** (per #167's design decision: this is design-readiness
signal, not enforcement — a repo opts into gating via its own ratchet config, not by
this script defaulting to `--gate`). `--gate` exists for that opt-in case: exit 1 if
any finding survives the allowlist.

Exit codes: 0 = ok (or report-only with findings), 1 = `--gate` violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

AUTHORITY = (
    "flags nondeterminism-shaped calls in scanned first-party code; does not prove "
    "dependencies or unscanned files are deterministic"
)

GO_EXTS = {".go"}
PY_EXTS = {".py"}
JS_EXTS = {".ts", ".tsx", ".js", ".jsx"}
ALL_EXTS = GO_EXTS | PY_EXTS | JS_EXTS

SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}

DEFAULT_SEAMS = ["**/clock*", "**/rand*", "**/telemetry/**"]

EXEMPT_MARKER = "cw:dst-exempt"
TOP_MARKER_LINES = 20

# Go's default (package-level, unseeded) math/rand surface. Deliberately not trying to
# detect "is there a seeded *rand.Rand in scope" — that needs real type/data-flow
# analysis, not a grep tier. If a repo seeds its own generator, it belongs behind a
# `**/rand*` seam (or gets a `cw:dst-exempt` marker) so it doesn't show up here.
_GO_RAND_FUNCS = (
    "Intn", "Int31n", "Int63n", "Int31", "Int63", "Int",
    "Float32", "Float64", "Perm", "Shuffle", "NormFloat64", "ExpFloat64",
)

RULES: list[dict] = [
    {
        "id": "wall-clock",
        "label": "wall-clock read",
        "checks": [
            (GO_EXTS, re.compile(r"\btime\.Now\s*\(")),
            (PY_EXTS, re.compile(r"\bdatetime\.now\s*\(")),
            (PY_EXTS, re.compile(r"\bdatetime\.utcnow\s*\(")),
            (PY_EXTS, re.compile(r"\btime\.time\s*\(")),
            (JS_EXTS, re.compile(r"\bDate\.now\s*\(")),
            (JS_EXTS, re.compile(r"\bnew\s+Date\s*\(\s*\)")),
        ],
    },
    {
        "id": "unseeded-random",
        "label": "unseeded randomness",
        "checks": [
            (GO_EXTS, re.compile(r"\brand\.(?:" + "|".join(_GO_RAND_FUNCS) + r")\s*\(")),
            # Module-level `random.<fn>(` — excluding `random.seed(`, which IS the seam
            # pattern (a repo that seeds explicitly is doing the right thing).
            (PY_EXTS, re.compile(r"\brandom\.(?!seed\b)\w+\s*\(")),
            (JS_EXTS, re.compile(r"\bMath\.random\s*\(")),
        ],
    },
]

# Line-comment markers per language, used to strip trailing comments before matching so
# a call mentioned in a comment/docstring isn't misread as live code. Mirrors
# check_single_writer.py's _strip_line_comment / _COMMENT_MARKERS.
_COMMENT_MARKERS = {
    ".go": ("//",), ".ts": ("//",), ".tsx": ("//",), ".js": ("//",), ".jsx": ("//",),
    ".py": ("#",),
}


def _strip_line_comment(line: str, suffix: str) -> str:
    """Drop a trailing line comment, respecting string/char literals so an in-string
    marker (a URL's `//`, a `#` inside a Python string) is preserved. Per-line only —
    a rare multi-line string literal isn't tracked, matching check_single_writer.py."""
    markers = _COMMENT_MARKERS.get(suffix)
    if not markers:
        return line
    quote: str | None = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        for m in markers:
            if line.startswith(m, i):
                return line[:i]
        i += 1
    return line


def _is_test_path(rel: str) -> bool:
    """Same heuristic as check_traceability.py: test infrastructure isn't a production
    nondeterminism concern the way live business logic is."""
    low = rel.lower()
    return "test" in low or "spec" in low or any(p == "e2e" for p in Path(low).parts)


def _glob_to_regex(glob: str) -> re.Pattern:
    """Translate a small gitignore-ish glob to an anchored regex.

    Supports ``**`` (any number of path segments, including none — so ``**/clock*``
    matches both a root-level ``clock.go`` and ``internal/clock/clock.go``), ``*``
    (anything within one path segment), and ``?`` (one char within a segment).
    Good enough for the handful of small, human-authored seam globs this tool takes;
    not a general-purpose glob library.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        if glob[i:i + 3] == "**/":
            out.append(r"(?:.*/)?")
            i += 3
        elif glob[i:i + 2] == "**":
            out.append(r".*")
            i += 2
        elif glob[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _is_seam(posix_rel: str, seam_regexes: list[re.Pattern]) -> bool:
    """True if ``posix_rel`` (or one of its ancestor directories) matches a seam glob.

    Testing ancestor directories, not just the full file path, means a seam glob that
    matches a *directory* name (e.g. ``**/clock*`` matching a ``internal/clock/``
    package) exempts every file under it, not just files whose own name starts with
    "clock".
    """
    segments = posix_rel.split("/")
    candidates = ["/".join(segments[:k]) for k in range(1, len(segments) + 1)]
    for regex in seam_regexes:
        for cand in candidates:
            if regex.match(cand):
                return True
    return False


def _has_exempt_marker(raw_lines: list[str]) -> bool:
    for line in raw_lines[:TOP_MARKER_LINES]:
        if EXEMPT_MARKER in line:
            return True
    return False


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    text: str
    match: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DSTReport:
    findings: list[dict] = field(default_factory=list)
    scanned_files: int = 0
    exempted_files: list[dict] = field(default_factory=list)
    seams: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    authority: str = AUTHORITY

    @property
    def counts(self) -> dict:
        wall_clock = sum(1 for f in self.findings if f["rule"] == "wall-clock")
        unseeded_random = sum(1 for f in self.findings if f["rule"] == "unseeded-random")
        return {
            "total": len(self.findings),
            "wall_clock": wall_clock,
            "unseeded_random": unseeded_random,
            "files_scanned": self.scanned_files,
            "files_exempted": len(self.exempted_files),
        }

    @property
    def ok(self) -> bool:
        """True if there are no findings — the single gate condition ``--gate`` checks."""
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "ok": self.ok,
            "authority": self.authority,
            "seams": self.seams,
            "findings": self.findings,
            "exempted_files": self.exempted_files,
            "warnings": self.warnings,
        }


def _load_seams(config_path: str | Path | None, warnings: list[str]) -> list[str]:
    seams = list(DEFAULT_SEAMS)
    if not config_path:
        return seams
    try:
        cfg = json.loads(Path(config_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"could not read --config {config_path}: {exc}")
        return seams
    extra = cfg.get("seams") if isinstance(cfg, dict) else None
    if extra is None:
        return seams
    if not isinstance(extra, list):
        warnings.append(f"--config {config_path}: 'seams' must be a list of glob strings; ignoring")
        return seams
    seams += [str(s) for s in extra if s]
    return seams


def check(source_root: str | Path, config_path: str | Path | None = None) -> DSTReport:
    root = Path(source_root)
    warnings: list[str] = []
    seams = _load_seams(config_path, warnings)
    seam_regexes = [_glob_to_regex(g) for g in seams]

    findings: list[Finding] = []
    exempted: list[dict] = []
    scanned = 0

    if not root.exists():
        warnings.append(f"source root not found: {source_root}")
        return DSTReport(seams=seams, warnings=warnings)

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in ALL_EXTS:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        rel = str(path.relative_to(root))
        posix_rel = rel.replace("\\", "/")

        if _is_test_path(rel):
            exempted.append({"file": rel, "reason": "test-path"})
            continue
        if _is_seam(posix_rel, seam_regexes):
            exempted.append({"file": rel, "reason": "seam-glob"})
            continue
        try:
            raw_lines = path.read_text().splitlines()
        except OSError:
            continue
        if _has_exempt_marker(raw_lines):
            exempted.append({"file": rel, "reason": f"{EXEMPT_MARKER} marker"})
            continue

        scanned += 1
        code_lines = [_strip_line_comment(rl, path.suffix) for rl in raw_lines]
        for i, line in enumerate(code_lines):
            for rule in RULES:
                for exts, pattern in rule["checks"]:
                    if path.suffix not in exts:
                        continue
                    for m in pattern.finditer(line):
                        findings.append(Finding(
                            rule=rule["id"],
                            file=rel,
                            line=i + 1,
                            text=raw_lines[i].strip()[:200],
                            match=m.group(0).strip(),
                        ))

    return DSTReport(
        findings=[f.to_dict() for f in findings],
        scanned_files=scanned,
        exempted_files=exempted,
        seams=seams,
        warnings=warnings,
    )


def render_text(report: DSTReport) -> str:
    c = report.counts
    lines = [
        "# DST-Readiness Scan",
        "",
        f"Files scanned: {c['files_scanned']}  |  Exempted: {c['files_exempted']}",
        f"Findings: {c['total']}  (wall-clock: {c['wall_clock']}, unseeded-random: {c['unseeded_random']})",
        "",
        f"Authority: {report.authority}",
        "",
        f"Seams: {', '.join(report.seams)}",
    ]
    if report.findings:
        lines += ["", "## Findings", ""]
        for f in report.findings:
            lines.append(f"- [{f['rule']}] {f['file']}:{f['line']} — `{f['match']}`")
            lines.append(f"    {f['text']}")
    else:
        lines += ["", "No findings."]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DST-readiness scanner: flags wall-clock reads and unseeded "
        "randomness outside designated seams (report-only by default)"
    )
    parser.add_argument("source_root", help="Repo root to scan")
    parser.add_argument(
        "--config",
        help="JSON file with a 'seams' array of glob patterns, unioned with the "
        "built-in defaults (**/clock*, **/rand*, **/telemetry/**)",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Fail (exit 1) if any finding survives the allowlist. This scanner is "
        "advisory FOREVER by default — a repo opts into gating via its own ratchet "
        "config, not by this flag being on by default anywhere in the pipeline.",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if not Path(args.source_root).exists():
        print(f"Error: source root not found: {args.source_root}", file=sys.stderr)
        return 2

    report = check(args.source_root, args.config)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        caught = len(report.findings)
        emit_gate(
            "check_dst_readiness",
            "fail" if caught else "pass",
            caught=caught,
            repo=os.path.basename(os.path.abspath(args.source_root)),
        )
    except Exception:
        pass

    if args.gate and not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

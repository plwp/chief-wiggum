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

Rules (regex tier, language-aware line scanning over comment- and string-stripped
source; see "What is stripped" below):

- **wall-clock** — a direct wall-clock read outside a designated clock seam:
  Go ``time.Now``; Python ``datetime.now`` / ``datetime.utcnow`` (both the
  ``datetime.now(...)`` and ``datetime.datetime.now(...)`` spellings), ``time.time``,
  plus per-file import-alias forms (``import datetime as dt`` → ``dt.now`` /
  ``dt.datetime.now``; ``import time as tm`` → ``tm.time``; ``from time import time``
  → a bare ``time(...)`` call, ``as`` aliases included); TS/JS ``Date.now`` and
  no-arg ``new Date`` construction.
- **unseeded-random** — an unseeded, DEFAULT-SOURCE random call outside a designated
  random seam:
  - Go: package-level calls on the ``math/rand`` (or ``math/rand/v2``) import —
    resolved per file from the import declarations, so an aliased import
    (``mrand "math/rand"``) is tracked, and a file that imports ``crypto/rand`` as
    ``rand`` is NOT misflagged. Seeded/explicit-source construction
    (``rand.New(rand.NewSource(...))``, ``rand.Seed(...)``) is not flagged — only
    the default-source draw functions (``Intn``, ``Float64``, v2's ``N``/``IntN``,
    etc). A file with no visible rand-related import falls back to matching the
    conventional ``rand.`` package name (snippets/fixtures).
  - Python: module-level ``random.<fn>(`` calls (canonical name, an
    ``import random as X`` alias, or bare names from ``from random import randint,
    choice``), excluding ``random.seed(`` (seeding explicitly is the seam pattern,
    not a violation of it) and capitalized constructors (``random.Random(42)``,
    ``random.SystemRandom()`` — constructing an explicit source is a deliberate
    decision, and instance-method calls on the resulting variable are untrackable
    at a regex tier anyway).
  - JS/TS: ``Math.random(``.
- **IO in non-designated modules is OUT OF SCOPE for v1.** Grepping for "any file
  read/network call/db call outside package X" is far noisier than the two rules above
  (imports, wrapper libraries, and legitimate framework glue all look like violations)
  and needs project-specific knowledge of which packages are the designated IO seam.
  Revisit once the clock/random tier has proven itself on real repos.

**Precision over recall.** This gate is advisory; per the gate-rollout doctrine a noisy
advisory is worse than none. Wherever a regex tier cannot decide (an aliased local
variable holding a rand source, code inside a JS template-literal interpolation, a
dynamically imported module), the scanner deliberately UNDER-reports rather than risk
false positives. Known under-reporting: instance-method randomness (``rng.Intn(`` /
``my_rng.random(``), calls inside JS/TS template-literal ``${...}`` interpolations
(the whole literal is stripped), multi-line ``from random import (...)`` continuation
lines, star/dot imports, and rare multi-line ``'``/``"`` string literals.

What is stripped before matching (so prose can't be misread as live code):

- line comments (``//``, ``#``), string-literal aware;
- Go/TS/JS block comments (``/* ... */``), including multi-line;
- Python triple-quoted strings/docstrings, including multi-line;
- quoted string-literal CONTENTS on a line (``'...'``, ``"..."``, and backtick
  raw/template literals in Go/JS — backtick literals are tracked across lines).

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

# Matched against path components RELATIVE to the scanned root — a checkout that
# happens to live under /tmp/build/ or /vendor/ must still be scanned in full.
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}

DEFAULT_SEAMS = ["**/clock*", "**/rand*", "**/telemetry/**"]

EXEMPT_MARKER = "cw:dst-exempt"
TOP_MARKER_LINES = 20

WALL_CLOCK = "wall-clock"
UNSEEDED_RANDOM = "unseeded-random"

# Go's default (package-level, unseeded) math/rand draw surface, v1 and v2 names.
# Deliberately excludes the seam-construction surface (`New`, `NewSource`, `Seed`,
# `NewPCG`, `NewChaCha8`) — building a seeded/explicit source is the FIX, not a
# violation — and instance methods on a *rand.Rand variable are untrackable at a
# regex tier (documented under-reporting).
_GO_RAND_FUNCS = (
    "Intn", "Int31n", "Int63n", "Int31", "Int63", "Int",
    "Float32", "Float64", "Perm", "Shuffle", "NormFloat64", "ExpFloat64",
    # math/rand/v2 spellings
    "N", "IntN", "Int32N", "Int64N", "Int32", "Int64",
    "Uint", "UintN", "Uint32", "Uint32N", "Uint64", "Uint64N",
)
_GO_RAND_TAIL = r"\.(?:" + "|".join(_GO_RAND_FUNCS) + r")\s*\("

# Python module-level random calls: lowercase names only, so capitalized constructors
# (`random.Random(42)`, `random.SystemRandom()`) are never flagged, and `seed` is
# excluded (seeding explicitly IS the seam pattern).
_PY_RANDOM_TAIL = r"\.(?!seed\b)[a-z]\w*\s*\("

# Static per-extension checks that need no import context.
_STATIC_CHECKS: dict[str, list[tuple[str, re.Pattern]]] = {}
for _ext in GO_EXTS:
    _STATIC_CHECKS[_ext] = [
        (WALL_CLOCK, re.compile(r"\btime\.Now\s*\(")),
    ]
for _ext in PY_EXTS:
    _STATIC_CHECKS[_ext] = [
        # `\bdatetime\.now(` also matches inside `datetime.datetime.now(` — one
        # pattern covers both spellings (findings dedupe per rule+line).
        (WALL_CLOCK, re.compile(r"\bdatetime\.(?:now|utcnow)\s*\(")),
        (WALL_CLOCK, re.compile(r"\btime\.time\s*\(")),
        (UNSEEDED_RANDOM, re.compile(r"\brandom" + _PY_RANDOM_TAIL)),
    ]
for _ext in JS_EXTS:
    _STATIC_CHECKS[_ext] = [
        (WALL_CLOCK, re.compile(r"\bDate\.now\s*\(")),
        (WALL_CLOCK, re.compile(r"\bnew\s+Date\s*\(\s*\)")),
        (UNSEEDED_RANDOM, re.compile(r"\bMath\.random\s*\(")),
    ]


# --- import-alias tracking (per file) ----------------------------------------

# Go import of math/rand or crypto/rand, plain or aliased, single-line or inside an
# import block. Matched against RAW lines: the sanitizer blanks string literals, and
# an import path IS a string literal.
_GO_IMPORT_RE = re.compile(
    r'^\s*(?:import\s+)?(?:(?P<alias>[A-Za-z_.]\w*)\s+)?"(?P<path>math/rand(?:/v2)?|crypto/rand)"'
)

_PY_IMPORT_AS_RE = re.compile(r"^\s*import\s+(?P<mod>random|datetime|time)\s+as\s+(?P<alias>\w+)\s*$")
_PY_FROM_IMPORT_RE = re.compile(r"^\s*from\s+(?P<mod>random|datetime|time)\s+import\s+(?P<names>.+)$")


def _py_from_names(names: str) -> list[tuple[str, str]]:
    """Parse `a, b as c` (parens tolerated) into [(imported_name, local_name)].
    Single-line form only — a multi-line parenthesized import's continuation lines
    are documented under-reporting."""
    out: list[tuple[str, str]] = []
    for part in names.split(","):
        part = part.strip().lstrip("(").rstrip(")").strip()
        if not part or part == "*":
            continue
        m = re.match(r"^(\w+)(?:\s+as\s+(\w+))?$", part)
        if m:
            out.append((m.group(1), m.group(2) or m.group(1)))
    return out


def _dynamic_checks(
    suffix: str, raw_lines: list[str], code_lines: list[str]
) -> list[tuple[str, re.Pattern]]:
    """Per-file checks derived from the file's own import declarations."""
    checks: list[tuple[str, re.Pattern]] = []

    if suffix in GO_EXTS:
        math_aliases: list[str] = []
        crypto_seen = False
        for line in raw_lines:
            m = _GO_IMPORT_RE.match(line)
            if not m:
                continue
            alias = m.group("alias")
            if m.group("path").startswith("math/rand"):
                if alias in ("_", "."):
                    continue  # blank/dot import: bare-name calls are untrackable
                math_aliases.append(alias or "rand")
            else:
                crypto_seen = True
        if math_aliases:
            for a in dict.fromkeys(math_aliases):
                checks.append((UNSEEDED_RANDOM, re.compile(r"\b" + re.escape(a) + _GO_RAND_TAIL)))
        elif not crypto_seen:
            # No visible rand-related import (snippet/fixture): fall back to the
            # conventional package name. A file importing ONLY crypto/rand gets no
            # rand check — its `rand.` is the crypto package, not math/rand.
            checks.append((UNSEEDED_RANDOM, re.compile(r"\brand" + _GO_RAND_TAIL)))

    elif suffix in PY_EXTS:
        for line in code_lines:
            m = _PY_IMPORT_AS_RE.match(line)
            if m:
                alias = re.escape(m.group("alias"))
                mod = m.group("mod")
                if mod == "random":
                    checks.append((UNSEEDED_RANDOM, re.compile(r"\b" + alias + _PY_RANDOM_TAIL)))
                elif mod == "datetime":
                    checks.append((WALL_CLOCK, re.compile(
                        r"\b" + alias + r"\.(?:datetime\.)?(?:now|utcnow)\s*\(")))
                elif mod == "time":
                    checks.append((WALL_CLOCK, re.compile(r"\b" + alias + r"\.time\s*\(")))
                continue
            m = _PY_FROM_IMPORT_RE.match(line)
            if not m:
                continue
            mod = m.group("mod")
            for name, local in _py_from_names(m.group("names")):
                esc = re.escape(local)
                if mod == "time" and name == "time":
                    checks.append((WALL_CLOCK, re.compile(r"(?<![\w.])" + esc + r"\s*\(")))
                elif mod == "datetime" and name == "datetime" and local != "datetime":
                    # unaliased `from datetime import datetime` is covered statically
                    checks.append((WALL_CLOCK, re.compile(r"\b" + esc + r"\.(?:now|utcnow)\s*\(")))
                elif mod == "random" and name[:1].islower() and name != "seed":
                    checks.append((UNSEEDED_RANDOM, re.compile(r"(?<![\w.])" + esc + r"\s*\(")))

    return checks


# --- comment/string stripping ------------------------------------------------


def _skip_string(line: str, i: int) -> int:
    """Index just past the string literal opening at ``line[i]`` (backslash-escape
    aware). An unclosed literal blanks the rest of the line (documented: rare
    multi-line ``'``/``"`` literals are not tracked across lines)."""
    quote = line[i]
    i += 1
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def sanitize_lines(raw_lines: list[str], suffix: str) -> list[str]:
    """Strip comments and string-literal contents so prose can't match as live code.

    Handles line comments, Go/TS/JS block comments and backtick literals (both
    tracked across lines), Python triple-quoted strings/docstrings (tracked across
    lines), and single/double-quoted literal contents on a line. Code inside a JS
    template-literal interpolation is stripped with the literal — deliberate
    under-reporting (see module docstring).
    """
    is_py = suffix in PY_EXTS
    out: list[str] = []
    mode = "code"  # code | block_comment | triple | backtick
    triple_delim = ""
    for line in raw_lines:
        buf: list[str] = []
        i, n = 0, len(line)
        while i < n:
            if mode == "block_comment":
                j = line.find("*/", i)
                if j == -1:
                    i = n
                else:
                    i, mode = j + 2, "code"
                continue
            if mode == "triple":
                j = line.find(triple_delim, i)
                if j == -1:
                    i = n
                else:
                    i, mode = j + 3, "code"
                continue
            if mode == "backtick":
                j = line.find("`", i)
                if j == -1:
                    i = n
                else:
                    i, mode = j + 1, "code"
                continue
            ch = line[i]
            if is_py:
                if line.startswith('"""', i) or line.startswith("'''", i):
                    triple_delim = line[i:i + 3]
                    j = line.find(triple_delim, i + 3)
                    if j == -1:
                        mode, i = "triple", n
                    else:
                        i = j + 3
                    continue
                if ch == "#":
                    break
                if ch in ("'", '"'):
                    i = _skip_string(line, i)
                    continue
            else:
                if line.startswith("//", i):
                    break
                if line.startswith("/*", i):
                    j = line.find("*/", i + 2)
                    if j == -1:
                        mode, i = "block_comment", n
                    else:
                        i = j + 2
                    continue
                if ch == "`":
                    j = line.find("`", i + 1)
                    if j == -1:
                        mode, i = "backtick", n
                    else:
                        i = j + 1
                    continue
                if ch in ("'", '"'):
                    i = _skip_string(line, i)
                    continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


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
    return any(EXEMPT_MARKER in line for line in raw_lines[:TOP_MARKER_LINES])


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
        wall_clock = sum(1 for f in self.findings if f["rule"] == WALL_CLOCK)
        unseeded_random = sum(1 for f in self.findings if f["rule"] == UNSEEDED_RANDOM)
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


def scan_file(suffix: str, raw_lines: list[str], rel: str) -> list[Finding]:
    """Scan one file's lines: sanitize, resolve per-file import aliases, match rules.

    At most one finding per (rule, line) — a line spelling several banned calls of
    the same rule is one item to fix, not several findings.
    """
    code_lines = sanitize_lines(raw_lines, suffix)
    checks = list(_STATIC_CHECKS.get(suffix, ())) + _dynamic_checks(suffix, raw_lines, code_lines)
    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(code_lines):
        for rule, pattern in checks:
            if (rule, i) in seen:
                continue
            m = pattern.search(line)
            if not m:
                continue
            seen.add((rule, i))
            findings.append(Finding(
                rule=rule,
                file=rel,
                line=i + 1,
                text=raw_lines[i].strip()[:200],
                match=m.group(0).strip(),
            ))
    return findings


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
        rel_path = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel_path.parts):
            continue
        rel = str(rel_path)
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
        findings += scan_file(path.suffix, raw_lines, rel)

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

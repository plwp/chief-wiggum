"""Field-agnostic write-site emission (#162): the per-file regex family that
finds candidate write-shaped tokens in one file's text, with NO knowledge of
any invariant or controlled field.

This is the exact logic that used to live inline in ``check_single_writer.py``
(the emission half of the emission/claim split from #160) — moved here so it
can sit BEHIND the ``scripts/emitters/`` per-language interface (one seam,
reused by the Go/Python/TypeScript/generic emitter modules) instead of being
private to a single checker. ``check_single_writer.py`` re-exports every name
below unchanged, so existing imports (``check_single_writer.emit_write_sites``,
``check_single_writer.KIND_ASSIGN``, ...) keep working — this is a pure move,
not a behavior change (golden parity; see ``tests/test_single_writer_golden.py``
and ``docs/languages.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# A file is test infrastructure (not a sanctioned/unsanctioned production writer)
# — same heuristic as check_traceability.py. Test writes of a controlled field
# are fixtures, not a competing production write path, so they don't violate.
def _is_test_path(rel: str) -> bool:
    low = rel.lower()
    return "test" in low or "spec" in low or any(p == "e2e" for p in Path(low).parts)


# Generic (field-agnostic) write-site detectors. Each captures a candidate
# identifier token; whether that token belongs to any particular invariant's
# controlled field is a QUERY-TIME decision (match_writers, in
# check_single_writer.py), not baked into the regex. Kind indices (0-3) mirror
# the four write shapes documented in check_single_writer's module docstring
# and drive `persistence_only` filtering in match_writers.
KIND_ASSIGN, KIND_STRUCT, KIND_QUOTED, KIND_SQL = range(4)

# The token classes are wider than `\w+` on purpose: the pre-split scanner
# built its regexes from `re.escape(leaf_token)`, so a controlled field whose
# leaf contains a hyphen (e.g. a Mongo key `plan-tier`) DID match. Emission
# must cover at least that same token surface or a full scan could silently
# miss an unsanctioned writer of such a field; over-wide captures are harmless
# (a token that matches no invariant's field is dropped at claim time).
# 0. Assignment: `something.Plan =` / `.stripe_plan =` (not ==; `:=` — Go's
#    declare+assign — IS a write, so it's allowed).
ASSIGN_RE = re.compile(r"\.([\w-]+)\s*:?=[^=]")
# 1. Struct-literal / map set: `Plan: value` or `"plan": value` or `Key: "plan"`.
#    `:(?!=)` so Go's short-var-decl `plan := expr` is NOT read as a field set
#    (the captured token would be the local var name, and `:` the `:` of `:=`).
STRUCT_RE = re.compile(r"""(^|[\s,{(])['"]?([\w.-]+)['"]?\s*:(?!=)\s*""")
# 2. bson/Mongo update key referenced literally in a set expression. `.` is in
#    the class so a dotted key (`"provider.plan"`) is captured whole — leaf
#    tokens never contain dots, so it can't claim-match a leaf field, exactly
#    like the old quote-delimited exact-token match behaved.
QUOTED_RE = re.compile(r"""['"]([\w.-]+)['"]""")
# 3. SQL UPDATE ... SET <field> = ...  (multiple fields may be set on one line).
SQL_SET_KEYWORD_RE = re.compile(r"\bSET\b", re.IGNORECASE)
SQL_FIELD_RE = re.compile(r"\b([\w-]+)\s*=")

# A bson $set / Mongo update / SQL UPDATE context marker — a bare `"plan":` in a
# non-mutating context (e.g. a JSON response DTO field) shouldn't count. We only
# treat KIND_QUOTED (quoted-literal) as a write when the surrounding lines look
# like a mutation. KIND_ASSIGN and KIND_STRUCT are writes on their own.
MUTATION_CONTEXT_RE = re.compile(
    r"\$set|UpdateOne|UpdateMany|UpdateByID|FindOneAndUpdate|bson\.[ME]|SET\b|UPDATE\b",
    re.IGNORECASE,
)

# A bson/Mongo QUERY operator on the same line means the `"field":` there is a FILTER
# clause (which document to match), not a `$set` value (what to write). e.g.
# `bson.M{"plan": bson.M{"$exists": false}}` selects rows, it doesn't write plan. Skip it.
FILTER_OPERATOR_RE = re.compile(
    r"\$(?:exists|in|nin|ne|eq|gt|gte|lt|lte|regex|or|and|not|nor|type|all|elemMatch|size)\b"
)

# Line-comment markers per language. Used to strip trailing comments before matching,
# so a field name mentioned in a comment (e.g. `// Free plan: …`) is not read as a write.
_COMMENT_MARKERS = {
    ".go": ("//",), ".ts": ("//",), ".tsx": ("//",), ".js": ("//",), ".jsx": ("//",),
    ".java": ("//",), ".rs": ("//",), ".py": ("#",), ".rb": ("#",),
}


def _strip_line_comment(line: str, suffix: str) -> str:
    """Drop a trailing line comment, respecting string/char literals so an in-string
    marker (a URL's `//`, a TS `#private`, a `#` inside a Python string) is preserved.
    Multi-line strings aren't tracked (per-line scan) — acceptable: at worst a comment
    marker inside a rare multi-line literal truncates a line we only search for writes."""
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


GO_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")
PY_FUNC_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)")
TS_FUNC_RE = re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\()")


def _enclosing_symbol(lines: list[str], idx: int) -> str | None:
    """Nearest function/method name declared at or above line index ``idx``."""
    for j in range(idx, -1, -1):
        line = lines[j]
        for pat in (GO_FUNC_RE, PY_FUNC_RE):
            m = pat.match(line)
            if m:
                return m.group(1)
        m = TS_FUNC_RE.search(line)
        if m:
            return m.group(1) or m.group(2)
    return None


@dataclass
class WriteSite:
    """A FIELD-AGNOSTIC candidate write site: emission time knows nothing about
    any invariant's controlled field — just "this line assigns/sets/matches an
    identifier token, in this file, in this enclosing symbol". Whether ``token``
    belongs to a controlled field is a query-time decision (``match_writers``,
    in ``check_single_writer.py``).
    """

    file: str
    line: int  # 1-indexed
    text: str  # raw (comment-un-stripped) line, stripped + truncated for display
    symbol: str | None
    is_test: bool
    kind: int  # KIND_ASSIGN | KIND_STRUCT | KIND_QUOTED | KIND_SQL
    token: str  # the identifier exactly as it appears in source (case preserved)


def emit_write_sites(path: str, text: str) -> list[WriteSite]:
    """Emission: every candidate write site in a single file's ``text``, with NO
    knowledge of any specific invariant or controlled field (see module +
    class docstrings). ``path`` is a repo-relative label used only for
    ``.suffix`` (comment-marker lookup) and the ``file`` attribute — the file
    is never re-read from disk, so this is safe to call on manifest-sourced
    content. This is the function every write-site emitter (language-specific
    or generic) under ``scripts/emitters/`` delegates to."""
    suffix = Path(path).suffix
    raw_lines = text.splitlines()
    code_lines = [_strip_line_comment(rl, suffix) for rl in raw_lines]
    is_test = _is_test_path(path)
    sites: list[WriteSite] = []
    for i, line in enumerate(code_lines):
        candidates: list[tuple[int, str]] = []  # (kind, token)
        for m in ASSIGN_RE.finditer(line):
            candidates.append((KIND_ASSIGN, m.group(1)))
        for m in STRUCT_RE.finditer(line):
            candidates.append((KIND_STRUCT, m.group(2)))
        for m in QUOTED_RE.finditer(line):
            # A bare quoted literal only counts as a write in a mutation context
            # (this line or either of the two lines above) and NOT when the same
            # line carries a query operator (a filter clause, not a $set value).
            # Both checks are invariant-independent, so resolved once here.
            if not (
                MUTATION_CONTEXT_RE.search(line)
                or (i > 0 and MUTATION_CONTEXT_RE.search(code_lines[i - 1]))
                or (i > 1 and MUTATION_CONTEXT_RE.search(code_lines[i - 2]))
            ):
                continue
            if FILTER_OPERATOR_RE.search(line):
                continue
            candidates.append((KIND_QUOTED, m.group(1)))
        for set_m in SQL_SET_KEYWORD_RE.finditer(line):
            tail = line[set_m.end():]
            semi = tail.find(";")
            if semi != -1:
                tail = tail[:semi]
            for fm in SQL_FIELD_RE.finditer(tail):
                candidates.append((KIND_SQL, fm.group(1)))
        if not candidates:
            continue
        symbol = _enclosing_symbol(code_lines, i)
        snippet = raw_lines[i].strip()[:200]
        for kind, token in candidates:
            sites.append(WriteSite(
                file=path, line=i + 1, text=snippet, symbol=symbol,
                is_test=is_test, kind=kind, token=token,
            ))
    return sites

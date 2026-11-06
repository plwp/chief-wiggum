#!/usr/bin/env python3
"""The performance gate in /saas-gate (chief-wiggum#524).

A delivered staff list took 20 s on its first production read: a write-side
sweep ran inline on a GET, one store write per item, under a 20 s deadline.
Staging measured 1.4 s because it had no backlog, a static review missed the
route, and no screen had a latency budget, so no test could fail. This gate
covers the four places that would have caught it:

``static``     Three rules over a Go service's source, with file:line output:
               * ``write-on-read``  a persistence write reachable from a GET
                 handler, on the request path (a goroutine the handler never
                 joins is off it);
               * ``unbounded-list`` a list query with no window: no Limit, no
                 range filter, only a tenant filter (``site``/``tenant``);
               * ``read-in-loop``   a store read, direct or through a call,
                 inside a loop in GET-reachable code (N+1).
``walk``       The cold, production-shaped walk: time every declared GET
               operation twice against a running instance (first load, then
               warm), and read the browser walk's HAR files for each declared
               screen's API waterfall and time-to-usable. Fails anything over
               its budget. Never fires a mutating request without
               ``--probe-mutating``.
``telemetry``  Per-endpoint p50/p95 from Cloud Run request logs (read-only),
               as one line for the morning briefing, flagging endpoints over
               budget.
``self-test``  Plants the #434 defect in a fixture and requires every rule
               that should fire to fire, and the fixed shape to stay clean.

Budgets are one field, ``latency_budget_ms``, on an operation in
``contracts.json`` and on a page in ``ui-spec.json``. With none declared, a
GET operation and a signed-in (staff) screen get 2000 ms.

What it does not see (also in docs/perf-gate.md):
* Go only. Other stacks report ``inapplicable``, never ``pass``.
* The call graph is by name: an interface call reaches every method of that
  name whose first parameter is a context. It over-reaches rather than miss.
* Calls through function values (callbacks passed by name) are not followed.
* ``unbounded-list`` checks that a bound exists, not that it is the asked
  window; a query built in a helper from a parameter is not judged.
* A line marked ``// cw-perf: ok <reason>`` is a reviewed exception: a call
  so marked is not followed, a finding so marked is dropped and counted.
* Never walks node_modules, .next, vendor, dist, build or other build output.

Gate status: REPORT-ONLY by default per docs/gate-rollout.md; ``--gate``
exits 1 on findings. Validation record: docs/quality/validation/perf-gate.json.

Exit codes: 0 ok / report-only / inapplicable, 1 findings under --gate,
2 usage error, 3 an input could not be read.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_boot_and_hit import PARAM_RE, SAFE_METHODS, substitute  # noqa: E402

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_ERROR = 0, 1, 2, 3

DEFAULT_BUDGET_MS = 2000

# Build output, dependencies and tooling state. Walking these is how a
# traceability run on a Next.js repo took six minutes and how a TypeScript
# type file in .next was read as a writer (fable-formal review, 6 Nov 2026).
EXCLUDED_DIRS = frozenset({
    "node_modules", ".next", ".nuxt", ".svelte-kit", ".turbo", ".vercel",
    ".git", ".hg", "vendor", "dist", "build", "out", "coverage", "target",
    "testdata", "fixtures", ".cache", "__pycache__", ".venv", "venv", ".terraform",
    "bower_components", ".parcel-cache", ".output",
})

# A filter on one of these alone scopes a query to a tenant: every record the
# tenant ever had, which grows without bound. It is not a window.
DEFAULT_SCOPE_FIELDS = ("site", "tenant", "tenant_id", "site_id", "org",
                        "org_id", "account", "account_id", "workspace",
                        "workspace_id", "owner", "owner_id")

OK_MARKER = re.compile(r"//.*\bcw-perf:\s*ok\b")

CTX = r"(?:\w*ctx\w*|\w*Ctx\w*|[\w.]+\.Context\(\)|context\.\w+\(\))"
CTX_ARG = re.compile(r"ctx|Ctx|Context\(\)|context\.")

# Store calls by shape. A generic verb (Set/Get/...) counts only when the
# name is NOT defined in the repo: a repo-defined name is a call edge and is
# judged by its body instead.
WRITE_LIB = [
    re.compile(r"\.(RunTransaction)\s*\("),
    re.compile(r"\b(tx\w*|txn\w*)\.(Set|Create|Update|Delete)\s*\("),
    re.compile(r"\.(Set|Create|Update|Delete|Add|Commit)\s*\(\s*" + CTX),
    re.compile(r"\.(ExecContext)\s*\("),
    re.compile(r"\.(InsertOne|InsertMany|UpdateOne|UpdateMany|UpdateByID|ReplaceOne|"
               r"DeleteOne|DeleteMany|FindOneAndUpdate|FindOneAndReplace|"
               r"FindOneAndDelete|BulkWrite)\s*\("),
]
SQL_EXEC = re.compile(r"\.(Exec)\s*\(")
SQL_WRITE_WORD = re.compile(r"(?i)\b(insert|update|delete|replace|merge|upsert)\b")
READ_LIB = [
    re.compile(r"\.(Get|Documents|GetAll|DocumentRefs)\s*\(\s*" + CTX),
    re.compile(r"\b(tx\w*|txn\w*)\.(Get|GetAll|Documents)\s*\("),
    re.compile(r"\.(QueryContext|QueryRowContext)\s*\("),
    re.compile(r"\.(Find|FindOne|Aggregate|CountDocuments)\s*\(\s*" + CTX),
]
LIST_CALL = re.compile(r"\.(Documents|DocumentRefs)\s*\(\s*" + CTX)

FUNC_RE = re.compile(
    r"^func\s*(?:\(\s*(?:(\w+)\s+)?\*?\s*(\w+)(?:\[[^\]]*\])?\s*\))?\s*(\w+)\s*(?:\[[^\]]*\])?\s*\(",
    re.M)
CALL_RE = re.compile(r"(\.)?\s*\b([A-Za-z_]\w*)\s*\(")
GO_KEYWORDS = frozenset(
    "if for switch select func return go defer case range map chan struct "
    "interface make new len cap append copy delete panic recover print println "
    "close complex real imag min max clear string int bool byte rune error any".split())
METHOD_TOKEN = re.compile(r'http\.Method(\w+)|"([A-Z]+)"')
GET_LIKE = {"GET", "HEAD"}
REG_RE = re.compile(
    r'\.(?:HandleFunc|Handle)\(\s*"(?:(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?[^"]*"\s*,\s*([\w.()*&]+)')
ROUTER_RE = re.compile(
    r'\.(Get|Post|Put|Patch|Delete|Head|GET|POST|PUT|PATCH|DELETE|HEAD)\(\s*"[^"]*"\s*,\s*([\w.]+)')
WHERE_RE = re.compile(r'\.Where\(\s*("[^"]*"|[\w.]+)\s*,\s*"([^"]*)"\s*,\s*([^)]*)\)')
DOC_RE = re.compile(r"\.Doc\(([^)]*)\)\s*\.\s*Collection\(")
BOUND_CALLS = re.compile(r"\.(Limit|LimitToLast|StartAt|StartAfter|EndAt|EndBefore)\s*\(")
RANGE_OPS = {"<", "<=", ">", ">=", "in", "array-contains-any", "not-in"}


# --- Go source model -------------------------------------------------------------


def _blank(src: str) -> tuple[str, str]:
    """Return (no_comments, code). Both keep every offset and newline of src;
    ``no_comments`` keeps string literals, ``code`` blanks their contents too,
    so a pattern never matches inside a string or a comment."""
    nc = list(src)
    code = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                nc[k] = code[k] = " "
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    nc[k] = code[k] = " "
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                if c != "`" and src[j] == "\\":
                    j += 1
                elif c != "`" and src[j] == "\n":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if src[k] != "\n":
                    code[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(nc), "".join(code)


def _match(code: str, open_at: int) -> int:
    """Offset of the bracket closing the one at open_at (or len(code))."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    o = code[open_at]
    c = pairs[o]
    depth = 0
    for k in range(open_at, len(code)):
        ch = code[k]
        if ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return k
    return len(code)


@dataclass
class GoFile:
    rel: str
    raw: str
    nc: str
    code: str
    nl: list[int]
    ok_lines: set[int]

    def line(self, off: int) -> int:
        return bisect.bisect_right(self.nl, off) + 1

    def text_line(self, line: int) -> str:
        start = self.nl[line - 2] + 1 if line >= 2 else 0
        end = self.nl[line - 1] if line - 1 < len(self.nl) else len(self.raw)
        return self.raw[start:end]


@dataclass
class GoFunc:
    key: int
    name: str
    recv: str | None
    file: GoFile
    params: str
    body_start: int  # offset of '{'
    body_end: int    # offset of '}'
    decl_line: int
    recv_var: str | None = None
    is_handler: bool = False
    req_var: str | None = None
    first_ctx: bool = False
    detached: list[tuple[int, int]] = field(default_factory=list)
    get_excluded: list[tuple[int, int]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.recv}.{self.name}" if self.recv else self.name


def _find_body(code: str, pos: int) -> int | None:
    """From just after a func's parameter list, the offset of its body '{'."""
    depth = 0
    k = pos
    n = len(code)
    while k < n:
        ch = code[k]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "{":
            before = code[max(0, k - 12):k].rstrip()
            if before.endswith(("interface", "struct")):
                k = _match(code, k) + 1
                continue
            if depth == 0:
                return k
        elif ch == "\n" and depth == 0:
            # `func f() T` with no body on this line: a forward declaration.
            rest = code[k + 1:k + 200].lstrip()
            if not rest.startswith(("{", ".", ",", ")")) and code[k - 1:k] not in (",", "("):
                return None
        k += 1
    return None


def _load_go(root: Path) -> tuple[list[GoFile], list[dict]]:
    files: list[GoFile] = []
    unreadable: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if not fn.endswith(".go") or fn.endswith("_test.go"):
                continue
            p = Path(dirpath) / fn
            try:
                raw = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                unreadable.append({"file": str(p.relative_to(root)), "reason": str(exc)})
                continue
            if "// Code generated" in raw[:400] and "DO NOT EDIT" in raw[:400]:
                continue
            nc, code = _blank(raw)
            nl = [m.start() for m in re.finditer("\n", raw)]
            ok = {i + 1 for i, ln in enumerate(raw.split("\n")) if OK_MARKER.search(ln)}
            files.append(GoFile(str(p.relative_to(root)), raw, nc, code, nl, ok))
    return files, unreadable


def _parse_funcs(files: list[GoFile]) -> list[GoFunc]:
    funcs: list[GoFunc] = []
    for f in files:
        for m in FUNC_RE.finditer(f.code):
            open_paren = m.end() - 1
            close_paren = _match(f.code, open_paren)
            body = _find_body(f.code, close_paren + 1)
            if body is None:
                continue
            end = _match(f.code, body)
            params = f.nc[open_paren + 1:close_paren]
            fn = GoFunc(len(funcs), m.group(3), m.group(2), f, params, body, end,
                        f.line(m.start()), recv_var=m.group(1))
            fn.first_ctx = bool(re.match(r"\s*\w+\s+context\.Context\b", params))
            if "http.ResponseWriter" in params and "*http.Request" in params:
                fn.is_handler = True
                rv = re.search(r"(\w+)\s+\*http\.Request", params)
                fn.req_var = rv.group(1) if rv else "r"
            funcs.append(fn)
    return funcs


def _methods_in(text: str) -> set[str]:
    return {(a or b).upper() for a, b in METHOD_TOKEN.findall(text)}


def _get_excluded(fn: GoFunc) -> list[tuple[int, int]]:
    """Ranges of a handler body that serve only non-GET methods."""
    nc = fn.file.nc
    code = fn.file.code
    rv = re.escape(fn.req_var or "r")
    out: list[tuple[int, int]] = []
    lo, hi = fn.body_start, fn.body_end
    for m in re.finditer(r"\bif\s+([^{;]*\b" + rv + r"\.Method\b[^{]*)\{", nc[lo:hi]):
        cond = m.group(1)
        b_open = lo + m.end() - 1
        b_close = _match(code, b_open)
        block_returns = re.search(r"\breturn\b", code[b_open:b_close]) is not None
        neq = re.findall(rv + r"\.Method\s*!=\s*(http\.Method\w+|\"[A-Z]+\")", cond)
        eq = re.findall(rv + r"\.Method\s*==\s*(http\.Method\w+|\"[A-Z]+\")", cond)
        neq_m = _methods_in(" ".join(neq))
        eq_m = _methods_in(" ".join(eq))
        if neq_m and not eq_m:
            if neq_m & GET_LIKE:
                out.append((b_open, b_close))          # the block is for non-GET
            elif block_returns:
                out.append((b_close, hi))              # the rest is for neq_m only
        elif eq_m and not neq_m:
            if not eq_m & GET_LIKE:
                out.append((b_open, b_close))
            elif block_returns:
                out.append((b_close, hi))
    for m in re.finditer(r"\bswitch\s*(" + rv + r"\.Method)?\s*\{", nc[lo:hi]):
        on_method = m.group(1) is not None
        s_open = lo + m.end() - 1
        s_close = _match(code, s_open)
        clauses = []
        depth = 0
        k = s_open + 1
        while k < s_close:
            ch = code[k]
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
            elif depth == 0 and re.match(r"\b(case|default)\b", code[k:k + 8]) and (
                    k == 0 or not (code[k - 1].isalnum() or code[k - 1] == "_")):
                colon = code.find(":", k)
                clauses.append((k, colon))
            k += 1
        get_case = any(
            re.search(rv + r"\.Method\s*==", nc[a:c]) and _methods_in(nc[a:c]) & GET_LIKE
            for a, c in clauses)
        for idx, (start, colon) in enumerate(clauses):
            end = clauses[idx + 1][0] if idx + 1 < len(clauses) else s_close
            label = nc[start:colon]
            if label.startswith("default"):
                # In a tagless switch whose cases test the method, a GET case
                # claims the reads; default is what is left over.
                if not on_method and get_case:
                    out.append((colon, end))
                continue
            if on_method:
                methods = _methods_in(label)
            elif re.search(rv + r"\.Method\s*==", label):
                methods = _methods_in(" ".join(re.findall(
                    rv + r"\.Method\s*==\s*(http\.Method\w+|\"[A-Z]+\")", label)))
            else:
                continue
            if methods and not methods & GET_LIKE:
                out.append((colon, end))
    return out


def _detached(fn: GoFunc) -> list[tuple[int, int]]:
    """``go`` statements the function never joins: off the request path. A
    goroutine followed by a ``.Wait(`` or a channel receive in the same
    function is joined, and its work is on the path."""
    code = fn.file.code
    out = []
    for m in re.finditer(r"(?<![\w.])go\s+", code[fn.body_start:fn.body_end]):
        s = fn.body_start + m.end()
        if code.startswith("func", s):
            p_open = code.find("(", s)
            p_close = _match(code, p_open)
            b_open = code.find("{", p_close)
            e = _match(code, b_open)
            call_end = code.find(")", e)
            span = (s, e)
            after = call_end if call_end > 0 else e
        else:
            eol = code.find("\n", s)
            span = (s, eol)
            after = eol
        tail = code[after:fn.body_end]
        if re.search(r"\.Wait\s*\(|<-", tail):
            continue
        out.append(span)
    return out


def _in(off: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= off < b for a, b in ranges)


def _arg0(code: str, open_paren: int) -> str:
    depth = 0
    for k in range(open_paren, min(len(code), open_paren + 400)):
        ch = code[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1:k]
        elif ch == "," and depth == 1:
            return code[open_paren + 1:k]
    return ""


class GoModel:
    def __init__(self, files: list[GoFile]):
        self.files = files
        self.funcs = _parse_funcs(files)
        self.by_name: dict[str, list[GoFunc]] = {}
        for fn in self.funcs:
            self.by_name.setdefault(fn.name, []).append(fn)
            fn.detached = _detached(fn)
            if fn.is_handler:
                fn.get_excluded = _get_excluded(fn)
        self._edges: dict[int, list[tuple[GoFunc, int, bool]]] = {}
        self.suppressed = 0

    def ranges_skip(self, fn: GoFunc, as_root_handler: bool) -> list[tuple[int, int]]:
        skip = list(fn.detached)
        if fn.is_handler and as_root_handler:
            skip += fn.get_excluded
        return skip

    def calls(self, fn: GoFunc, skip: list[tuple[int, int]]):
        """(callee, offset, suppressed) for every call in fn's body outside skip."""
        code = fn.file.code
        for m in CALL_RE.finditer(code, fn.body_start + 1, fn.body_end):
            name = m.group(2)
            if name in GO_KEYWORDS or name not in self.by_name:
                continue
            off = m.start(2)
            if _in(off, skip):
                continue
            dotted = m.group(1) is not None
            arg0 = _arg0(code, m.end() - 1)
            arg0_nc = _arg0(fn.file.nc, m.end() - 1)
            # A call on the method's own receiver (`k.refresh()` inside a
            # method of k's type) reaches that type's methods, if it has one.
            on_self = bool(dotted and fn.recv_var and re.search(
                r"(?<![\w.])" + re.escape(fn.recv_var) + r"\s*\.\s*$", code[max(0, m.start() - 60):m.start() + 1]))
            same_type = [c for c in self.by_name[name] if c.recv == fn.recv] if on_self else []
            for callee in same_type or self.by_name[name]:
                if callee is fn:
                    continue
                if not dotted and callee.recv:
                    continue
                if callee.first_ctx and not CTX_ARG.search(arg0_nc or arg0):
                    continue
                yield callee, off

    def handler_roots(self) -> list[GoFunc]:
        handlers = [f for f in self.funcs if f.is_handler]
        non_get: set[str] = set()
        get_ok: set[str] = set()
        routed: list[GoFunc] = []
        for f in self.files:
            for m in REG_RE.finditer(f.nc):
                name = re.split(r"[.)]", m.group(2).rstrip(")"))[-1]
                (get_ok if not m.group(1) or m.group(1) in GET_LIKE else non_get).add(name)
            for m in ROUTER_RE.finditer(f.nc):
                name = m.group(2).split(".")[-1]
                if m.group(1).upper() in GET_LIKE:
                    get_ok.add(name)
                    routed.extend(fn for fn in self.by_name.get(name, []) if not fn.is_handler)
                else:
                    non_get.add(name)
        # A route table row names its method beside the handler:
        # {method: http.MethodPost, handler: (*server).handleSubmit}.
        names = {h.name for h in handlers}
        for f in self.files:
            for ln in f.nc.split("\n"):
                methods = _methods_in(ln)
                if not methods or re.search(r"\.Method\s*[!=]=", ln):
                    continue  # no method named, or dispatch logic rather than a route row
                for name in re.findall(r"\b(\w+)\b(?!\s*\()", ln):
                    if name in names and re.search(r"[.)]\s*" + name + r"\b", ln) and \
                            not re.search(r"\bfunc\b", ln):
                        (get_ok if methods & GET_LIKE else non_get).add(name)
        referenced: list[GoFunc] = []
        for h in handlers:
            if h.name in non_get and h.name not in get_ok:
                continue
            pat = re.compile(r"\b" + re.escape(h.name) + r"\b(?!\s*\()")
            for f in self.files:
                if any(not (f is h.file and abs(f.line(m.start()) - h.decl_line) == 0)
                       for m in pat.finditer(f.code)):
                    referenced.append(h)
                    break
        roots = referenced or [h for h in handlers if not (h.name in non_get and h.name not in get_ok)]
        return roots + [r for r in routed if r not in roots]

    def reach(self, roots: list[GoFunc]) -> dict[int, tuple[GoFunc, list[GoFunc]]]:
        """fn.key -> (root, chain root..fn), shortest chain over all roots."""
        best: dict[int, tuple[GoFunc, list[GoFunc]]] = {}
        for root in roots:
            seen = {root.key: [root]}
            q = deque([root])
            while q:
                fn = q.popleft()
                skip = self.ranges_skip(fn, as_root_handler=True)
                for callee, off in self.calls(fn, skip):
                    if fn.file.line(off) in fn.file.ok_lines:
                        self.suppressed += 1
                        continue
                    if callee.key in seen:
                        continue
                    seen[callee.key] = seen[fn.key] + [callee]
                    q.append(callee)
            for k, chain in seen.items():
                if k not in best or len(chain) < len(best[k][1]):
                    best[k] = (root, chain)
        return best


# --- the three rules ---------------------------------------------------------------


def _lib_hits(model: GoModel, fn: GoFunc, pats, skip, lo=None, hi=None):
    """Library store calls in fn's body (outside skip). A generic verb whose
    name the repo itself defines is a call edge, judged by its body instead;
    a transaction's verbs (``tx.Set``) are always the library's."""
    code = fn.file.code
    lo = fn.body_start if lo is None else lo
    hi = fn.body_end if hi is None else hi
    hits = []
    for pat in pats:
        for m in pat.finditer(code, lo, hi):
            on_tx = pat.groups >= 2
            verb = m.group(2) if on_tx else m.group(1)
            if not on_tx and verb in model.by_name:
                continue
            if _in(m.start(), skip):
                continue
            hits.append((m.start(), m.group(0).strip()))
    return hits


def _writes(model: GoModel, fn: GoFunc, skip) -> list[tuple[int, str]]:
    hits = _lib_hits(model, fn, WRITE_LIB, skip)
    code, nc = fn.file.code, fn.file.nc
    for m in SQL_EXEC.finditer(code, fn.body_start, fn.body_end):
        if _in(m.start(), skip) or "Exec" in model.by_name:
            continue
        if SQL_WRITE_WORD.search(nc[m.start():m.start() + 300].split("\n\n")[0]):
            hits.append((m.start(), m.group(0)))
    return sorted(hits)


def _direct_reads(model: GoModel, fn: GoFunc, skip, lo, hi) -> list[tuple[int, str]]:
    return sorted(_lib_hits(model, fn, READ_LIB, skip, lo, hi))


def _chain_text(fn: GoFunc, off: int) -> str:
    """The statement holding the list call, back to its start (continuation
    lines end in '.', '(' or ',')."""
    nc = fn.file.nc
    line_start = nc.rfind("\n", 0, off) + 1
    start = line_start
    while start > fn.body_start:
        prev_end = start - 1
        prev_start = nc.rfind("\n", 0, prev_end) + 1
        prev = nc[prev_start:prev_end].rstrip()
        if prev.endswith((".", "(", ",")):
            start = prev_start
        else:
            break
    return nc[start:off]


def _bounded(text: str, scope: set[str]) -> bool:
    if BOUND_CALLS.search(text):
        return True
    for fld, op, val in WHERE_RE.findall(text):
        name = fld.strip('"').split(".")[-1].lower()
        if op in RANGE_OPS:
            return True
        if op == "==" and (not fld.startswith('"') or name not in scope):
            return True
        if op == "array-contains" and not re.fullmatch(r'\s*"[^"]*"\s*', val):
            return True
    for args in DOC_RE.findall(text):
        idents = {t for t in re.findall(r"[A-Za-z_]\w*", re.sub(r'"[^"]*"', "", args))}
        if idents - scope:
            return True
    return False


def _unbounded(model: GoModel, fn: GoFunc, skip, scope: set[str]) -> list[tuple[int, str]]:
    out = []
    code = fn.file.code
    for m in LIST_CALL.finditer(code, fn.body_start, fn.body_end):
        if _in(m.start(), skip):
            continue
        text = _chain_text(fn, m.start())
        recv = re.search(r"(?:^|[\s=(,])([A-Za-z_]\w*)\s*(?:\.\s*\w+\([^()]*\)\s*)*$", text)
        if recv and not re.search(r"\.\s*$", text[:recv.start(1)]):
            var = recv.group(1)
            if re.search(r"\b" + re.escape(var) + r"\s+[\w.*\[\]]*(firestore\.)?Query\b", fn.params):
                continue  # the caller builds the query: not judged here
            assigns = re.findall(r"\b" + re.escape(var) + r"\s*:?=\s*([^\n]+)", fn.file.nc[fn.body_start:m.start()])
            text = text + " " + " ".join(assigns)
        # One level into a repo helper the chain calls (e.g. f.coll(site)).
        for cm in re.finditer(r"\.?\b([A-Za-z_]\w*)\s*\(", text):
            for h in model.by_name.get(cm.group(1), []):
                text += " " + h.file.nc[h.body_start:h.body_end]
        if _bounded(text, scope):
            continue
        out.append((m.start(), m.group(0).strip()))
    return out


def _loops(fn: GoFunc, skip) -> list[tuple[int, int, int]]:
    """(for offset, body open, body close) of loops over a result set: a
    ``range`` or an iterator loop. Not a range over a literal list, and not a
    three-clause counter loop."""
    code = fn.file.code
    out = []
    for m in re.finditer(r"(?<![\w.])for\b", code[fn.body_start:fn.body_end]):
        s = fn.body_start + m.start()
        if _in(s, skip):
            continue
        k = s + 3
        depth = 0
        literal = False
        while k < fn.body_end:
            ch = code[k]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "{" and depth == 0:
                head = code[s:k].rstrip()
                if re.search(r"(\][\w.*]+|map\[[^\]]*\][\w.*]+|struct)$", head):
                    literal = True
                    k = _match(code, k) + 1
                    continue
                break
            k += 1
        if k >= fn.body_end or literal:
            continue
        if code[s + 3:k].count(";") == 2:
            continue  # `for i := 0; i < n; i++`: a counter (retries, pages), not a result set
        out.append((s, k, _match(code, k)))
    return out


def scan_static(root: Path | str, *, scope_fields=DEFAULT_SCOPE_FIELDS) -> StaticReport:
    root = Path(root)
    t0 = time.monotonic()
    report = StaticReport(root=str(root))
    files, unreadable = _load_go(root)
    report.files_scanned = len(files)
    report.unreadable = unreadable
    if unreadable:
        report.outcome = "error"
    model = GoModel(files)
    report.functions = len(model.funcs)
    roots = model.handler_roots()
    report.handlers = len(roots)
    if not roots:
        if report.outcome != "error":
            report.outcome = "inapplicable"
            report.reason = ("no Go HTTP handlers found (the static rules cover Go services "
                             "only); nothing was judged, which is not a pass")
        report.seconds = round(time.monotonic() - t0, 2)
        return report
    reached = model.reach(roots)
    report.reached = len(reached)
    scope = {s.lower() for s in scope_fields}
    reads_memo: dict[int, bool] = {}

    def reads_store(fn: GoFunc, stack: set[int]) -> bool:
        if fn.key in reads_memo:
            return reads_memo[fn.key]
        if fn.key in stack:
            return False
        stack.add(fn.key)
        skip = fn.detached
        hit = bool(_direct_reads(model, fn, skip, fn.body_start, fn.body_end))
        if not hit:
            for callee, off in model.calls(fn, skip):
                if fn.file.line(off) in fn.file.ok_lines:
                    continue
                if reads_store(callee, stack):
                    hit = True
                    break
        stack.discard(fn.key)
        reads_memo[fn.key] = hit
        return hit

    findings: dict[tuple, dict] = {}

    def add(rule: str, fn: GoFunc, off: int, root: GoFunc, chain: list[GoFunc], detail: str):
        line = fn.file.line(off)
        if line in fn.file.ok_lines:
            model.suppressed += 1
            return
        key = (rule, fn.file.rel, line)
        if key in findings:
            findings[key]["handlers"] = sorted(set(findings[key]["handlers"]) | {root.name})
            return
        findings[key] = {
            "rule": rule, "file": fn.file.rel, "line": line, "function": fn.label,
            "handler": root.name, "handler_file": root.file.rel, "handler_line": root.decl_line,
            "handlers": [root.name], "chain": [c.name for c in chain], "detail": detail,
        }

    for _key, (hroot, chain) in sorted(reached.items(), key=lambda kv: (kv[1][1][-1].file.rel, kv[1][1][-1].decl_line)):
        fn = chain[-1]
        skip = model.ranges_skip(fn, as_root_handler=True)
        w = _writes(model, fn, skip)
        if w:
            off, text = w[0]
            add("write-on-read", fn, off, hroot, chain,
                f"`{text}` runs on the request path of a GET handler")
        for off, text in _unbounded(model, fn, skip, scope):
            add("unbounded-list", fn, off, hroot, chain,
                f"`{text}` lists with no Limit or range filter (a tenant filter is not a window)")
        for _for, b_open, b_close in _loops(fn, skip):
            for off, text in _direct_reads(model, fn, skip, b_open, b_close):
                add("read-in-loop", fn, off, hroot, chain, f"`{text}` inside a loop: one store read per item")
            for callee, off in model.calls(fn, skip):
                if not (b_open < off < b_close) or fn.file.line(off) in fn.file.ok_lines:
                    continue
                if reads_store(callee, set()):
                    add("read-in-loop", fn, off, hroot, chain,
                        f"`{callee.name}(...)` reads the store, once per loop item")
    report.findings = sorted(findings.values(), key=lambda f: (f["rule"], f["file"], f["line"]))
    report.suppressed = model.suppressed
    if report.outcome != "error":
        report.outcome = "findings" if report.findings else "pass"
    report.seconds = round(time.monotonic() - t0, 2)
    return report


@dataclass
class StaticReport:
    root: str
    outcome: str = "pass"
    reason: str = ""
    files_scanned: int = 0
    functions: int = 0
    handlers: int = 0
    reached: int = 0
    suppressed: int = 0
    seconds: float = 0.0
    unreadable: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f["rule"]] = counts.get(f["rule"], 0) + 1
        return {
            "outcome": self.outcome, "reason": self.reason, "root": self.root,
            "measured": {"files_scanned": self.files_scanned, "functions": self.functions,
                         "get_handlers": self.handlers, "reached_functions": self.reached,
                         "suppressed": self.suppressed, "seconds": self.seconds,
                         "excluded_dirs": sorted(EXCLUDED_DIRS)},
            "counts": counts, "unreadable": self.unreadable, "findings": self.findings,
        }


def render_static_text(report: StaticReport) -> str:
    d = report.to_dict()
    m = d["measured"]
    lines = [f"Perf static rules: {report.outcome}"
             f" ({m['files_scanned']} Go files, {m['get_handlers']} GET handlers, "
             f"{m['reached_functions']} reachable functions, {m['suppressed']} suppressed, "
             f"{m['seconds']} s)"]
    if report.reason:
        lines.append(f"  {report.reason}")
    for u in report.unreadable:
        lines.append(f"  unreadable: {u['file']}: {u['reason']}")
    for f in report.findings:
        via = " -> ".join(f["chain"])
        more = len(f["handlers"]) - 1
        lines.append(f"{f['file']}:{f['line']}: {f['rule']}: {f['detail']}")
        lines.append(f"    from GET handler {f['handler']} ({f['handler_file']}:{f['handler_line']})"
                     f"{f' and {more} more' if more > 0 else ''} via {via}")
    return "\n".join(lines) + "\n"


# --- budgets -------------------------------------------------------------------------


def _model_files(dirs: list[Path], name: str) -> list[Path]:
    out = []
    for d in dirs:
        if d.is_file() and d.name == name:
            out.append(d)
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDED_DIRS]
            if name in filenames:
                out.append(Path(dirpath) / name)
    return sorted(out)


def load_budgets(*dirs: Path | str, default_ms: int = DEFAULT_BUDGET_MS) -> list[dict]:
    """Every operation (not external) and every staff or budgeted screen, with
    its latency budget: the declared ``latency_budget_ms`` or the default."""
    paths = [Path(d) for d in dirs]
    targets: dict[tuple, dict] = {}

    def put(t: dict) -> None:
        k = (t["kind"], t["method"], t["path"])
        if k not in targets or t["budget_ms"] < targets[k]["budget_ms"]:
            targets[k] = t

    for p in _model_files(paths, "contracts.json"):
        data = json.loads(p.read_text())
        for ent in data.get("entities") or []:
            for op in (ent.get("operations") if isinstance(ent, dict) else None) or []:
                if not isinstance(op, dict) or op.get("external") is True or not op.get("path"):
                    continue
                b = op.get("latency_budget_ms")
                put({"kind": "operation", "method": str(op.get("method") or "GET").upper(),
                     "path": op["path"], "budget_ms": int(b) if b else default_ms,
                     "budget_source": "declared" if b else "default", "model": str(p)})
    for p in _model_files(paths, "ui-spec.json"):
        data = json.loads(p.read_text())
        for key, page in (data.get("pages") or {}).items():
            if not isinstance(page, dict):
                continue
            route = page.get("route", key)
            if not route:
                continue
            b = page.get("latency_budget_ms")
            if not b and page.get("auth", "required") == "public":
                continue
            put({"kind": "screen", "method": "GET", "path": route,
                 "budget_ms": int(b) if b else default_ms,
                 "budget_source": "declared" if b else "default", "model": str(p)})
    return sorted(targets.values(), key=lambda t: (t["kind"], t["path"], t["method"]))


# --- the walk --------------------------------------------------------------------------


def default_fetch(url: str, method: str, headers: dict, *, timeout: float = 60.0) -> int:
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": "chief-wiggum-perf-walk", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator URL
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def _verdict(budget: int, *times: int | None) -> str:
    return "over_budget" if any(t is not None and t > budget for t in times) else "within_budget"


def walk_operations(targets: list[dict], base_url: str, *, params: dict[str, str] | None = None,
                    fetch=None, clock=None, probe_mutating: bool = False,
                    headers: dict | None = None) -> dict:
    fetch = fetch or default_fetch
    clock = clock or time.monotonic
    params = params or {}
    headers = headers or {}
    root = base_url.rstrip("/")
    out = []
    for t in (t for t in targets if t["kind"] == "operation"):
        e = {"method": t["method"], "path": t["path"], "budget_ms": t["budget_ms"],
             "budget_source": t["budget_source"], "cold_ms": None, "warm_ms": None, "status": None}
        if t["method"] not in SAFE_METHODS and not probe_mutating:
            out.append({**e, "verdict": "not_probed",
                        "detail": "mutating; pass --probe-mutating for a disposable target"})
            continue
        filled, missing = substitute(t["path"], params)
        if missing:
            out.append({**e, "verdict": "unprobeable",
                        "detail": f"pass --path-param {missing[0]}=<value>"})
            continue
        url = root + (filled if filled.startswith("/") else "/" + filled)
        e["url"] = url
        try:
            times = []
            for _ in ("cold", "warm"):
                s = clock()
                status = fetch(url, t["method"], headers)
                times.append(int(round((clock() - s) * 1000)))
        except Exception as exc:  # noqa: BLE001
            out.append({**e, "verdict": "unreachable", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        e.update(cold_ms=times[0], warm_ms=times[1], status=status)
        if status in (401, 403):
            e["verdict"] = "gated"
            e["detail"] = "the auth layer answered: give a QA principal (--auth-header-env)"
        elif status >= 400:
            e["verdict"] = "error_status"
        else:
            e["verdict"] = _verdict(t["budget_ms"], *times)
        out.append(e)
    return {"operations": out, "outcome": _walk_outcome(out)}


def _walk_outcome(entries: list[dict]) -> str:
    if any(e["verdict"] in ("over_budget", "error_status", "unreachable") for e in entries):
        return "findings"
    if not any(e["verdict"] == "within_budget" for e in entries):
        return "inapplicable"
    return "pass"


def _iso_ms(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def _route_re(route: str) -> re.Pattern:
    parts = []
    last = 0
    for m in PARAM_RE.finditer(route):
        parts.append(re.escape(route[last:m.start()]))
        parts.append(r"[^/]+")
        last = m.end()
    parts.append(re.escape(route[last:]))
    return re.compile("^" + "".join(parts).rstrip("/") + "/?$")


def _har_loads(har_path: Path) -> list[dict]:
    """One record per page load in a HAR: its path, total time to the last
    response, and its request waterfall."""
    log = json.loads(Path(har_path).read_text())["log"]
    by_page: dict[str, list[dict]] = {}
    for e in log.get("entries") or []:
        by_page.setdefault(e.get("pageref") or "", []).append(e)
    loads = []
    for page in log.get("pages") or []:
        entries = sorted(by_page.get(page["id"], []), key=lambda e: e["startedDateTime"])
        if not entries:
            continue
        p0 = _iso_ms(page["startedDateTime"])
        doc = next((e for e in entries if e.get("_resourceType") == "document"), entries[0])
        path = urllib.parse.urlparse(doc["request"]["url"]).path
        fall = []
        for e in entries:
            start = _iso_ms(e["startedDateTime"]) - p0
            fall.append({"url": e["request"]["url"], "method": e["request"].get("method", "GET"),
                         "start_ms": int(round(start)), "ms": int(round(e.get("time") or 0)),
                         "status": (e.get("response") or {}).get("status"),
                         "type": e.get("_resourceType")})
        total = max(f["start_ms"] + f["ms"] for f in fall)
        loads.append({"path": path, "ms": total, "waterfall": fall})
    return loads


def walk_screens(targets: list[dict], *, cold_har: Path | None, warm_har: Path | None) -> dict:
    """Time-to-usable per declared screen from the browser walk's HAR files:
    the first load (cold) and a reload (warm). Time-to-usable is the end of
    the screen's last request."""
    cold = _har_loads(cold_har) if cold_har else []
    warm = _har_loads(warm_har) if warm_har else []
    out = []
    for t in (t for t in targets if t["kind"] == "screen"):
        rx = _route_re(t["path"])
        c = next((x for x in cold if rx.match(x["path"])), None)
        w = next((x for x in warm if rx.match(x["path"])), None)
        e = {"path": t["path"], "budget_ms": t["budget_ms"], "budget_source": t["budget_source"],
             "cold_ms": c["ms"] if c else None, "warm_ms": w["ms"] if w else None,
             "waterfall": {"cold": c["waterfall"] if c else [], "warm": w["waterfall"] if w else []}}
        if not c and not w:
            e["verdict"] = "not_walked"
        else:
            e["verdict"] = _verdict(t["budget_ms"], e["cold_ms"], e["warm_ms"])
        out.append(e)
    outcome = _walk_outcome(out)
    return {"screens": out, "outcome": outcome,
            "not_walked": [e["path"] for e in out if e["verdict"] == "not_walked"]}


# --- production telemetry -----------------------------------------------------------------


def _latency_ms(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return int(round(float(v.get("seconds", 0)) * 1000 + float(v.get("nanos", 0)) / 1e6))
    s = str(v).strip()
    if s.endswith("s"):
        s = s[:-1]
    try:
        return int(round(float(s) * 1000))
    except ValueError:
        return None


ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{32,36}|(?=.*\d)[A-Za-z0-9_-]{6,})$")


def _normalise(path: str) -> str:
    return "/".join("{id}" if seg and ID_SEG.match(seg) else seg for seg in path.split("/"))


def _pct(values: list[int], p: float) -> int:
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def endpoint_stats(entries: list[dict], budgets: list[dict], *,
                   default_ms: int = DEFAULT_BUDGET_MS, routes: list[str] | None = None) -> list[dict]:
    """``routes``: extra route templates (e.g. ``/api/staff/{site}/bookings``)
    for endpoints the contracts do not declare, so a tenant slug in a path is
    grouped, not reported."""
    ops = [(t["method"], _route_re(t["path"]), t) for t in budgets if t["kind"] == "operation"]
    ops += [(m, _route_re(p), {"path": p, "budget_ms": default_ms, "budget_source": "default"})
            for m in sorted(SAFE_METHODS) for p in routes or []]
    groups: dict[tuple[str, str], dict] = {}
    for e in entries:
        hr = e.get("httpRequest") or {}
        ms = _latency_ms(hr.get("latency"))
        if ms is None or not hr.get("requestUrl"):
            continue
        method = (hr.get("requestMethod") or "GET").upper()
        path = urllib.parse.urlparse(hr["requestUrl"]).path
        match = next((t for m, rx, t in ops if m == method and rx.match(path)), None)
        route = match["path"] if match else _normalise(path)
        g = groups.setdefault((method, route), {"ms": [], "budget": match, "errors": 0})
        g["ms"].append(ms)
        if int(hr.get("status") or 0) >= 500:
            g["errors"] += 1
    stats = []
    for (method, route), g in groups.items():
        budget = g["budget"]["budget_ms"] if g["budget"] else (default_ms if method in SAFE_METHODS else None)
        p95 = _pct(g["ms"], 0.95)
        stats.append({"method": method, "route": route, "count": len(g["ms"]),
                      "p50_ms": _pct(g["ms"], 0.5), "p95_ms": p95, "errors_5xx": g["errors"],
                      "budget_ms": budget,
                      "budget_source": "declared" if g["budget"] and g["budget"]["budget_source"] == "declared"
                      else ("default" if budget else None),
                      "over_budget": bool(budget and p95 > budget)})
    return sorted(stats, key=lambda s: (-s["over_budget"], -s["p95_ms"]))


def _sec(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def briefing_line(stats: list[dict], *, window: str = "24h", top: int = 3) -> str:
    if not stats:
        return f"Perf ({window}): no request logs read."
    over = [s for s in stats if s["over_budget"]]
    parts = [f"Perf ({window}): {len(stats)} endpoints, {sum(s['count'] for s in stats)} requests, "
             f"{len(over)} over budget."]
    for s in (over or stats)[:top]:
        flag = "OVER " if s["over_budget"] else ""
        b = f", budget {_sec(s['budget_ms'])}" if s["budget_ms"] else ""
        parts.append(f"{flag}{s['method']} {s['route']} p95 {_sec(s['p95_ms'])} "
                     f"(p50 {_sec(s['p50_ms'])}{b}, n={s['count']})")
    return " ".join(parts[:1]) + " " + "; ".join(parts[1:])


def read_cloud_run_logs(project: str, service: str, region: str | None, freshness: str,
                        limit: int) -> list[dict]:
    """Read-only: ``gcloud logging read`` of Cloud Run request logs."""
    flt = (f'logName="projects/{project}/logs/run.googleapis.com%2Frequests" '
           f'AND resource.labels.service_name="{service}"')
    if region:
        flt += f' AND resource.labels.location="{region}"'
    proc = subprocess.run(
        ["gcloud", "logging", "read", flt, f"--project={project}", f"--freshness={freshness}",
         f"--limit={limit}", "--format=json"],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:])
    return json.loads(proc.stdout or "[]")


# --- /saas-gate ---------------------------------------------------------------------------


def saas_findings(repo: Path | str, *, models: list | None = None, base_url: str | None = None,
                  params: dict | None = None, probe_mutating: bool = False,
                  headers: dict | None = None, cold_har: Path | None = None,
                  warm_har: Path | None = None, static_report: StaticReport | None = None,
                  fetch=None, clock=None) -> list[tuple[str, str, str]]:
    """The gate's results as saas_gate findings: (name, status, detail), with
    saas_gate's statuses (pass/fail/warn/skipped/not_applicable/error)."""
    out: list[tuple[str, str, str]] = []
    r = static_report or scan_static(repo)
    if r.outcome == "error":
        out.append(("static-rules", "error", f"unreadable sources: {r.unreadable[:3]}"))
    elif r.outcome == "inapplicable":
        out.append(("static-rules", "not_applicable", r.reason))
    else:
        for rule in ("write-on-read", "unbounded-list", "read-in-loop"):
            hits = [f for f in r.findings if f["rule"] == rule]
            if hits:
                sites = ", ".join(f"{f['file']}:{f['line']} ({f['handler']})" for f in hits[:5])
                more = f" and {len(hits) - 5} more" if len(hits) > 5 else ""
                out.append((rule, "fail", f"{len(hits)}: {sites}{more}"))
            else:
                out.append((rule, "pass", f"none across {r.handlers} GET handlers"))
    if not models:
        out.append(("budget-walk", "skipped", "no --perf-models: no budgets to walk against"))
        return out
    targets = load_budgets(*models)
    if not base_url:
        out.append(("budget-walk", "skipped", "no --base-url: nothing was timed"))
    else:
        w = walk_operations(targets, base_url, params=params, probe_mutating=probe_mutating,
                            headers=headers, fetch=fetch, clock=clock)
        bad = [e for e in w["operations"] if e["verdict"] in ("over_budget", "error_status", "unreachable")]
        timed = [e for e in w["operations"] if e["verdict"] == "within_budget"]
        if bad:
            out.append(("budget-walk", "fail", "; ".join(
                f"{e['method']} {e['path']} {e['verdict']} (cold {e['cold_ms']} ms, warm {e['warm_ms']} ms, "
                f"budget {e['budget_ms']} ms)" for e in bad[:5])))
        elif timed:
            out.append(("budget-walk", "pass", f"{len(timed)} operations within budget, cold and warm"))
        else:
            out.append(("budget-walk", "warn", "nothing was timed: every operation was "
                        "unprobeable, gated or mutating"))
    if cold_har or warm_har:
        sw = walk_screens(targets, cold_har=cold_har, warm_har=warm_har)
        over = [e for e in sw["screens"] if e["verdict"] == "over_budget"]
        if over:
            out.append(("screen-walk", "fail", "; ".join(
                f"{e['path']} cold {e['cold_ms']} ms, warm {e['warm_ms']} ms, budget {e['budget_ms']} ms"
                for e in over[:5])))
        elif sw["outcome"] == "pass":
            nw = f"; not walked: {', '.join(sw['not_walked'])}" if sw["not_walked"] else ""
            out.append(("screen-walk", "warn" if nw else "pass", f"walked screens within budget{nw}"))
        else:
            out.append(("screen-walk", "warn", "the HAR held none of the declared screens"))
    else:
        out.append(("screen-walk", "skipped", "no HAR from the browser walk"))
    return out


# --- self-test -----------------------------------------------------------------------------


FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "perf_gate"
VENDORED = ("package pkg\n\nimport \"net/http\"\n\n"
            "func handleVendored(w http.ResponseWriter, r *http.Request) {\n"
            "\tclient.RunTransaction(r.Context(), nil)\n}\n")


def self_test() -> list[dict]:
    """The planted #434 defect must fire every rule it carries; the fixed
    shape must not fire at all; build output must never be read."""
    trials = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("inline_sweep", "background_sweep"):
            dst = Path(tmp) / name
            shutil.copytree(FIXTURES / name, dst)
            for rel in ("node_modules/pkg/vendored.go", ".next/server/chunk.go"):
                p = dst / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(VENDORED)
            r = scan_static(dst)
            rules = {f["rule"] for f in r.findings}
            walked_build = any(f["file"].startswith(("node_modules", ".next")) for f in r.findings)
            if name == "inline_sweep":
                for rule in ("write-on-read", "unbounded-list", "read-in-loop"):
                    hit = [f for f in r.findings if f["rule"] == rule]
                    trials.append({"seed_id": f"perf-434-{rule}", "seed_class": "direct",
                                   "repo": f"tests/fixtures/perf_gate/{name}", "expected": "fire",
                                   "result": "fired" if hit else "not-fired",
                                   "evidence": [f"{f['file']}:{f['line']}" for f in hit],
                                   "passed": bool(hit)})
                trials.append({"seed_id": "perf-build-output-not-walked", "seed_class": "evasion-omission",
                               "repo": f"tests/fixtures/perf_gate/{name}", "expected": "no-fire",
                               "result": "fired" if walked_build else "not-fired",
                               "passed": not walked_build})
            else:
                trials.append({"seed_id": "perf-434-fixed-clean", "seed_class": "clean",
                               "repo": f"tests/fixtures/perf_gate/{name}", "expected": "no-fire",
                               "result": "fired" if r.findings else "not-fired",
                               "evidence": sorted(rules), "passed": not r.findings})
    return trials


# --- CLI ---------------------------------------------------------------------------------------


def _params(raw: list[str] | None) -> dict[str, str]:
    out = {}
    for item in raw or []:
        k, sep, v = item.partition("=")
        if not sep:
            raise SystemExit(f"--path-param expects name=value, got {item!r}")
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("static", help="the three static rules over a Go service")
    s.add_argument("root")
    s.add_argument("--scope-field", action="append",
                   help="a tenant field whose filter alone is not a window (repeatable; "
                        f"default {', '.join(DEFAULT_SCOPE_FIELDS)})")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--gate", action="store_true")

    w = sub.add_parser("walk", help="cold and warm timings against budgets")
    w.add_argument("--models", action="append", required=True,
                   help="dir (or file) holding contracts.json / ui-spec.json; repeatable")
    w.add_argument("--base-url")
    w.add_argument("--path-param", action="append")
    w.add_argument("--probe-mutating", action="store_true")
    w.add_argument("--auth-header-env",
                   help="env var holding a QA principal's Authorization header value; never printed")
    w.add_argument("--cold-har", help="HAR of the browser walk's first load of each screen")
    w.add_argument("--warm-har", help="HAR of the same walk, warm")
    w.add_argument("--out", help="write the full record (waterfalls included) here")
    w.add_argument("--gate", action="store_true")

    t = sub.add_parser("telemetry", help="per-endpoint p50/p95 from Cloud Run request logs")
    t.add_argument("--models", action="append", default=[])
    t.add_argument("--project")
    t.add_argument("--service")
    t.add_argument("--region")
    t.add_argument("--freshness", default="24h")
    t.add_argument("--limit", type=int, default=20000)
    t.add_argument("--from-file", help="read log entries (gcloud --format=json) from a file")
    t.add_argument("--route", action="append", default=[],
                   help="route template to group paths the contracts do not declare, e.g. "
                        "/api/staff/{site}/bookings; repeatable")
    t.add_argument("--format", choices=["line", "json"], default="line")

    st = sub.add_parser("self-test", help="plant the #434 defect and require the gate to fire")
    st.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_parser("scanner-version", help="hash of this scanner's source")

    a = ap.parse_args(argv)

    if a.cmd == "scanner-version":
        from chief_wiggum.hashing import scanner_version
        here = Path(__file__).resolve()
        print(scanner_version(here, here.parent / "check_boot_and_hit.py",
                              here.parent / "chief_wiggum" / "hashing.py"))
        return EXIT_OK

    if a.cmd == "static":
        root = Path(a.root)
        if not root.is_dir():
            print(f"Error: {a.root}: not a directory", file=sys.stderr)
            return EXIT_USAGE
        r = scan_static(root, scope_fields=tuple(a.scope_field) if a.scope_field else DEFAULT_SCOPE_FIELDS)
        print(json.dumps(r.to_dict(), indent=2) if a.format == "json" else render_static_text(r), end="")
        if r.outcome == "error":
            return EXIT_ERROR
        return EXIT_FINDINGS if a.gate and r.findings else EXIT_OK

    if a.cmd == "walk":
        try:
            targets = load_budgets(*a.models)
        except (OSError, ValueError) as exc:
            print(f"Error: reading models: {exc}", file=sys.stderr)
            return EXIT_ERROR
        headers = {}
        if a.auth_header_env:
            val = os.environ.get(a.auth_header_env)
            if not val:
                print(f"Error: {a.auth_header_env} is unset or empty", file=sys.stderr)
                return EXIT_USAGE
            headers["Authorization"] = val
        record: dict = {"targets": len(targets)}
        if a.base_url:
            record["api"] = walk_operations(targets, a.base_url, params=_params(a.path_param),
                                            probe_mutating=a.probe_mutating, headers=headers)
        else:
            record["api"] = {"outcome": "inapplicable", "operations": [],
                             "reason": "no --base-url: nothing was timed"}
        if a.cold_har or a.warm_har:
            record["screens"] = walk_screens(targets, cold_har=a.cold_har and Path(a.cold_har),
                                             warm_har=a.warm_har and Path(a.warm_har))
        else:
            record["screens"] = {"outcome": "inapplicable", "screens": [],
                                 "reason": "no HAR from the browser walk"}
        if a.out:
            Path(a.out).write_text(json.dumps(record, indent=2) + "\n")
        for e in record["api"]["operations"]:
            print(f"{e['verdict']:14} {e['method']:6} {e['path']}  cold {e['cold_ms']} ms, "
                  f"warm {e['warm_ms']} ms, budget {e['budget_ms']} ms ({e['budget_source']})")
        for e in record["screens"]["screens"]:
            print(f"{e['verdict']:14} screen {e['path']}  cold {e['cold_ms']} ms, "
                  f"warm {e['warm_ms']} ms, budget {e['budget_ms']} ms ({e['budget_source']})")
        print(f"api: {record['api']['outcome']}; screens: {record['screens']['outcome']}")
        bad = "findings" in (record["api"]["outcome"], record["screens"]["outcome"])
        return EXIT_FINDINGS if a.gate and bad else EXIT_OK

    if a.cmd == "telemetry":
        try:
            budgets = load_budgets(*a.models) if a.models else []
            if a.from_file:
                entries = json.loads(Path(a.from_file).read_text())
            elif a.project and a.service:
                entries = read_cloud_run_logs(a.project, a.service, a.region, a.freshness, a.limit)
            else:
                print("Error: give --from-file, or --project and --service", file=sys.stderr)
                return EXIT_USAGE
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        stats = endpoint_stats(entries, budgets, routes=a.route)
        if a.format == "json":
            print(json.dumps({"window": a.freshness, "endpoints": stats,
                              "line": briefing_line(stats, window=a.freshness)}, indent=2))
        else:
            print(briefing_line(stats, window=a.freshness))
        return EXIT_OK

    trials = self_test()
    if a.format == "json":
        print(json.dumps(trials, indent=2))
    else:
        for tr in trials:
            print(f"{'ok  ' if tr['passed'] else 'FAIL'} {tr['seed_id']}: expected {tr['expected']}, "
                  f"{tr['result']} {' '.join(tr.get('evidence') or [])}")
    return EXIT_OK if all(tr["passed"] for tr in trials) else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())

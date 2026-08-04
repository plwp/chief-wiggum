"""Verifier-test body hashing (chief-wiggum#206) — the ratchet's third hashed
dimension, closing goalpost channel C1c.

The pass-set is keyed by test ID, so a high-water test's *body* can be
rewritten to bless new behavior while its node ID stays green — no pass-set
shrink, no contract-hash change, every detector quiet. The goalpost-integrity
experiment demonstrated this both scripted (probe C1c) and behaviorally (a
pilot worker independently kept a stale test name alive "so the ratchet
pass-set node ID does not shrink" while inverting the assertion). See
docs/paper/experiment/results/.

The targeted fix: a test annotated ``@cw-trace verifies <ID>`` is not a
worker-owned unit test — it is the executable expression of a contract, i.e.
a goalpost. This module content-hashes each annotated test function's body so
``ratchet.py`` can ratchet those hashes exactly like contract-definition
hashes (first-entry high-water, journaled amend/retire, weakened/removed
findings).

Authority boundary (v1, stated — not silently claimed away):

- Only the annotated test function's own source span is hashed. Assertions
  relocated into a shared helper/fixture the test *calls* are outside the
  span — a documented no-fire boundary probed by the gate-validation record's
  ``evasion-config-indirection`` seed, not a covered case.
- Only Python is extracted in v1. A ``verifies`` annotation in any other
  language, an unparseable Python file, or an annotation with no enclosing/
  adjacent function is COUNTED AND SURFACED (``unscanned``), never silently
  dropped — same doctrine as check_single_writer's
  ``unsupported_extension_counts``.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from chief_wiggum import languages as cw_languages
from chief_wiggum.hashing import stable_hash
from chief_wiggum.trace_ids import TRACE_RE, canonical_id

# Directory names never scanned. EXTENDS the single-writer scanner's
# SKIP_PARTS ({.git, node_modules, vendor, dist, build, __pycache__, .venv})
# with test-scan-relevant dirs (venv/.tox/.mypy_cache/.pytest_cache) — not a
# mirror, a superset. (Several scanners hand-maintain their own SKIP_PARTS;
# consolidating them into one shared constant is a standing cleanup, #206
# review.)
SKIP_PARTS = {
    ".git", "node_modules", "vendor", "dist", "build", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
}

# Documentation formats are OUT OF SCOPE BY DESIGN, not "unscanned": markdown/
# rst/txt cannot host an executable test, and every annotation they carry is a
# syntax example (docs/traceability.md, docs/ratchet.md, ...). Surfacing those
# would be permanent warning noise — the erosion failure mode gate-rollout.md
# exists to prevent. Stated in the gate-validation record's authority boundary,
# same as vendor/ exclusion in check_single_writer.
DOC_EXTS = {".md", ".rst", ".txt"}

# (#326) scan_file's non-.py branch used to `path.read_text(errors="replace")`
# EVERY non-.py, non-doc file in the repo in full — no extension allow-list at
# all — just to substring-check for "@cw-trace". Images/lockfiles/fixtures
# dominated ratchet.py `score`'s I/O as a result. SCANNABLE_EXTS is a
# DELIBERATELY GENEROUS allow-list: the union of every extension
# config/languages.json recognizes at ALL — both BUILT-emitter languages
# (all_known_extensions: tier-1 + generic tier) and "recognized but no
# emitter" languages (unsupported_extensions: .c/.cpp/.php/.kt/.swift/...,
# still curated and maintained, see config/languages.json's own docstring) —
# plus DOC_EXTS (redundant with the early `path.suffix in DOC_EXTS` return
# above, listed for completeness/clarity: markdown can never reach this
# allow-list check anyway) and check_traceability's VERIFICATION_EXTS
# (.rego/.yaml/.yml). Any extension OUTSIDE this set was already invisible to
# check_traceability.py's own @cw-trace annotation scan (its SOURCE_EXTS
# predicate is this same known+verification set) — so restricting THIS
# scanner to the same universe is a consistency fix, not a new coverage gap:
# an extension nobody in the system recognizes as source was never going to
# host a meaningful verifies annotation in the first place. If a real repo
# ever needs a language extending past this list, extend
# config/languages.json's unsupported_extensions (curated, not exhaustive, by
# its own docstring) rather than narrowing this set further.
SCANNABLE_EXTS = (
    frozenset(cw_languages.all_known_extensions())
    | cw_languages.unsupported_extensions()
    | {".rego", ".yaml", ".yml"}
    | DOC_EXTS
)

# A file this large is not source code we expect a `@cw-trace verifies`
# annotation to live in — generous enough (2 MB) that no real test/source
# file is ever truncated in a way that would matter, while capping the I/O
# cost of a pathological huge file that happens to carry a SCANNABLE_EXTS
# extension (e.g. a generated fixture). Only the HEAD of an oversized file is
# read; an annotation past this offset is not detected — an accepted,
# documented boundary (mirrors chief_wiggum/review.py's diff truncation), not
# a silent extension-based drop.
MAX_NONPY_SCAN_BYTES = 2_000_000

# An annotation comment placed immediately ABOVE a def (rather than inside the
# body/docstring) still belongs to that function if the def starts within this
# many lines below it.
_ABOVE_DEF_WINDOW = 3


@dataclass
class VerifierScan:
    """Result of scanning a repo for ``@cw-trace verifies`` test bodies.

    ``hashes`` maps a stable ref (``<posix-relpath>::<function-name>``) to the
    hash of that function's source span. ``targets`` maps the same ref to the
    canonical contract IDs it verifies — ``ratchet.py record --amend CTR-x``
    uses it to re-baseline the tests that express an amended contract.
    ``unscanned`` counts annotated files/annotations the extractor could not
    hash, keyed by reason — surfaced by ``score``, never silently dropped.
    """

    hashes: dict[str, str] = field(default_factory=dict)
    targets: dict[str, list[str]] = field(default_factory=dict)
    unscanned: dict[str, int] = field(default_factory=dict)


def _verifies_ids(text: str) -> list[str]:
    """Canonical IDs of every ``verifies`` annotation in ``text``."""
    ids: list[str] = []
    for m in TRACE_RE.finditer(text):
        if m.group("verb").lower() == "verifies":
            ids.extend(canonical_id(t) for t in m.group("ids").replace(",", " ").split())
    return ids


def _comment_annotations(text: str) -> dict[int, list[str]]:
    """Map 0-based line number -> canonical IDs for ``verifies`` annotations in
    Python COMMENT tokens only.

    Tokenized, not line-grepped, so an annotation inside a string literal —
    e.g. a test that WRITES an annotated fixture file — can never register as
    an annotation of the writing test itself (a false pin that would turn
    every fixture-generator into a "verifier test")."""
    out: dict[int, list[str]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT and "@cw-trace" in tok.string:
                ids = _verifies_ids(tok.string)
                if ids:
                    out.setdefault(tok.start[0] - 1, []).extend(ids)
    except (tokenize.TokenError, IndentationError):
        pass  # caller already ast-parsed successfully; unreachable in practice
    return out


def _qualified_functions(tree: ast.AST):
    """Yield ``(node, qualified_name, (start_line0, end_line0))`` for every
    (async) function, so both annotation carriers share one qualified-name and
    span definition.

    The name is QUALIFIED by its enclosing class/function path
    (``TestA.test_it``) so two same-named methods in different classes — or a
    method and a module function of the same name — get distinct refs. A bare
    name would collide, silently overwriting one test's hash in the scorecard
    map and blinding the gate to a rewrite of the overwritten one. Start
    includes decorators, so hashing covers e.g. a swapped-in
    ``@pytest.mark.skip``."""
    out = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list]) - 1
                qual = f"{prefix}{child.name}"
                out.append((child, qual, (start, (child.end_lineno or child.lineno) - 1)))
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _hash_span(lines: list[str], start: int, end: int) -> str:
    """Hash a source span, right-stripped per line (mirrors the
    whitespace-normalization of contract-block hashing: reformatting is not
    weakening, any token change is)."""
    return stable_hash("\n".join(ln.rstrip() for ln in lines[start:end + 1]).strip())


def _bump(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _record_ref(scan: VerifierScan, relpath: str, name: str,
                lines: list[str], span: tuple[int, int], ids: list[str]) -> None:
    ref = f"{relpath}::{name}"
    scan.hashes[ref] = _hash_span(lines, span[0], span[1])
    scan.targets[ref] = sorted(set(scan.targets.get(ref, [])) | set(ids))


def scan_file(path: Path, relpath: str, scan: VerifierScan) -> None:
    """Extract verifier hashes from one file into ``scan``.

    Annotations are recognized in exactly two carriers, both immune to
    string-literal false pins: Python COMMENT tokens (attached to the
    containing function, or to a ``def`` starting within ``_ABOVE_DEF_WINDOW``
    lines below) and function DOCSTRINGS (attached to their own function).
    """
    if path.suffix in DOC_EXTS:
        return  # documentation — out of scope by design, see DOC_EXTS
    if path.suffix != ".py":
        if path.suffix not in SCANNABLE_EXTS:
            # (#326) not a plausible @cw-trace carrier by ANY known-language
            # extension — never read at all. See SCANNABLE_EXTS's comment for
            # why this allow-list is deliberately as generous as the rest of
            # the system's own source-extension universe, not a new gap.
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > MAX_NONPY_SCAN_BYTES:
            with path.open("r", errors="replace") as fh:
                text = fh.read(MAX_NONPY_SCAN_BYTES)
        else:
            text = path.read_text(errors="replace")
        if "@cw-trace" in text and _verifies_ids(text):
            _bump(scan.unscanned, f"unsupported extension {path.suffix or '(none)'}")
        return
    text = path.read_text(errors="replace")
    if "@cw-trace" not in text:
        return
    try:
        tree = ast.parse(text)
    except SyntaxError:
        if _verifies_ids(text):
            _bump(scan.unscanned, "python syntax error")
        return
    lines = text.splitlines()
    quals = _qualified_functions(tree)
    # A qualified name that appears more than once (conditional top-level
    # redefinition, a shadowed/redeclared class) is AMBIGUOUS — its ref cannot
    # reliably identify one body, so recording it would silently overwrite one
    # hash and union-merge two tests' contract targets into a false "covered".
    # Surface it as unscanned and refuse to record it, rather than pretend
    # coverage (#206 soundness review, finding 2).
    seen: dict[str, int] = {}
    for _n, q, _sp in quals:
        seen[q] = seen.get(q, 0) + 1
    ambiguous = {q for q, n in seen.items() if n > 1}
    _AMBIG = "ambiguous ref (duplicate qualified name — not recorded)"
    # Containment spans keep AMBIGUOUS functions (so a comment inside one is
    # reported as ambiguous, not "not attached") but recording refuses them.
    spans = sorted(((s, e, q) for _n, q, (s, e) in quals),
                   key=lambda s: s[1] - s[0], reverse=True)

    def _emit(qual: str, span: tuple[int, int], ids: list[str]) -> None:
        if qual in ambiguous:
            _bump(scan.unscanned, _AMBIG)
            return
        _record_ref(scan, relpath, qual, lines, span, ids)

    # Carrier 1: docstrings — attached to their own function, no line math.
    for node, qual, (start, end) in quals:
        ids = _verifies_ids(ast.get_docstring(node) or "")
        if ids:
            _emit(qual, (start, end), ids)

    # Carrier 2: comment tokens — containing function, else above-def window.
    for line_no, ids in sorted(_comment_annotations(text).items()):
        fn = None
        for s, e, name in spans:  # innermost containing function wins (last)
            if s <= line_no <= e:
                fn = (s, e, name)
        if fn is None:  # annotation-above-def form (skip ambiguous targets)
            below = [sp for sp in spans
                     if line_no < sp[0] <= line_no + _ABOVE_DEF_WINDOW
                     and sp[2] not in ambiguous]
            if below:
                fn = min(below, key=lambda sp: sp[0])
        if fn is None:
            _bump(scan.unscanned, "annotation not attached to a function")
            continue
        s, e, name = fn
        _emit(name, (s, e), ids)  # routes ambiguous names to unscanned


def scan_verifier_hashes(repo: str | Path) -> VerifierScan:
    """Scan a repo tree for ``@cw-trace verifies`` annotated test bodies.

    Nested git checkouts/submodules are pruned (their contents belong to their
    own repo's gates — same rule as the single-writer scanner)."""
    repo = Path(repo)
    scan = VerifierScan()
    stack = [repo]
    while stack:
        d = stack.pop()
        for entry in sorted(d.iterdir()):
            if entry.name in SKIP_PARTS:
                continue
            if entry.is_dir():
                if entry != repo and (entry / ".git").exists():
                    continue
                stack.append(entry)
            elif entry.is_file():
                scan_file(entry, entry.relative_to(repo).as_posix(), scan)
    return scan

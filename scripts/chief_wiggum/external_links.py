"""Symbol-anchored external ``@cw-trace`` link store (chief-wiggum#213, Phase C).

In sidecar footprint mode, in-source ``@cw-trace`` annotations are replaced by
entries in ``<meta_root>/quality/external-links.json`` — a link of
``file :: symbol -> <verb> <ID>...`` carrying a content hash of the anchored
symbol's source span. Hash drift means SUSPECT: "this symbol claimed to guard
CTR-x, but the symbol changed since that claim was recorded" — the exact
discipline of ``check_traceability.py``'s suspect links (definition-hash drift,
``chief_wiggum.trace_links``), applied from the code side instead of the
contract side. Suspect links are surfaced, never silently dropped, and do NOT
satisfy coverage.

The store reuses the SHAPE of ``docs/quality/trace-links.json``
(``{"links": [...]}``, deterministically ordered, written via
``trace_links.write_sidecar``); each entry additionally carries the sidecar
version binding (``target_sha``, the target HEAD the hash was computed
against — ``scripts/artifacts.py``'s mandatory staleness field).

Symbol anchoring is TIERED (settled in #213 — reuse, don't reimplement):

- **ast** — Python files resolve through the exact qualified-function span
  machinery of ``chief_wiggum.verifier_hashes`` (#206): qualified names
  (``TestA.test_it``), decorator-inclusive spans, whitespace-normalized
  hashing. Preferred over LSP for ``.py`` — it is byte-identical to the
  verifier-hash span discipline and needs no server spin-up.
- **lsp** — languages with a configured, installed language server
  (``chief_wiggum.lsp.SERVERS``; gopls today) resolve via
  ``textDocument/documentSymbol``.
- **regex** — remaining known extensions (``config/languages.json`` tier-1 +
  generic tier) fall back to the emitters' declaration regexes
  (``chief_wiggum.write_emission`` GO_FUNC_RE/PY_FUNC_RE/TS_FUNC_RE — the same
  matching ``_enclosing_symbol`` applies): the span runs from the symbol's
  declaration line to the next declaration (mirroring
  ``hashing.hash_markdown_defs`` block semantics).
- beyond that — **skip-with-warning**: the entry is recorded/reported as
  ``unresolved`` with a reason, never dropped (same doctrine as
  ``verifier_hashes``' ``unscanned`` counts).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # direct CLI invocation: put scripts/ on the path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifacts  # noqa: E402 — meta resolver (head_sha version binding, #213)

from chief_wiggum import languages as cw_languages  # noqa: E402
from chief_wiggum import lsp  # noqa: E402
from chief_wiggum.trace_ids import canonical_id  # noqa: E402
from chief_wiggum.trace_links import load_sidecar, write_sidecar  # noqa: E402

# REUSE of existing span machinery, not a reimplementation: the Python ast tier
# is verifier_hashes' qualified-function extraction (#206), the span hash is its
# whitespace-normalized hasher, and the regex tier is write_emission's
# declaration regexes (#160). These are private names imported deliberately —
# promoting/renaming them would edit modules that feed OTHER gates'
# scanner-version hashes (ratchet), staling validation records for no behavior
# change.
from chief_wiggum.verifier_hashes import _hash_span, _qualified_functions  # noqa: E402
from chief_wiggum.write_emission import GO_FUNC_RE, PY_FUNC_RE, TS_FUNC_RE  # noqa: E402

# Store filename beneath the meta root's quality/ dir — the sibling of
# trace-links.json in sidecar mode.
STORE_NAME = "external-links.json"

VERBS = ("guards", "ensures", "verifies")


@dataclass(frozen=True)
class SymbolSpan:
    """A resolved symbol anchor: 0-based inclusive line span + its content hash."""

    start: int
    end: int
    tier: str  # "ast" | "lsp" | "regex"
    hash: str

    @property
    def line(self) -> int:
        """1-based start line, for reporting handles."""
        return self.start + 1


# --- tiered symbol resolution -----------------------------------------------------


def _ast_span(text: str, symbol: str) -> tuple[SymbolSpan | None, str | None]:
    """Python tier: verifier_hashes' qualified-function spans. ``symbol`` may be
    the full qualified name (``TestA.test_it``) or a bare name that matches
    exactly one function."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return None, f"python syntax error: {exc}"
    lines = text.splitlines()
    quals = _qualified_functions(tree)
    matches = [(q, span) for _n, q, span in quals if q == symbol]
    if not matches:
        matches = [(q, span) for _n, q, span in quals if q.split(".")[-1] == symbol]
    if not matches:
        return None, f"symbol {symbol!r} not found (python ast tier)"
    if len(matches) > 1:
        return None, (
            f"symbol {symbol!r} is ambiguous ({len(matches)} matches — "
            "use the qualified name, e.g. ClassName.method)"
        )
    _q, (start, end) = matches[0]
    return SymbolSpan(start, end, "ast", _hash_span(lines, start, end)), None


def _decl_name(line: str) -> str | None:
    """The function/symbol a line declares, if any — the SAME per-line matching
    ``write_emission._enclosing_symbol`` applies (Go/Python anchored ``match``,
    TS/JS ``search``)."""
    for pat in (GO_FUNC_RE, PY_FUNC_RE):
        m = pat.match(line)
        if m:
            return m.group(1)
    m = TS_FUNC_RE.search(line)
    if m:
        return m.group(1) or m.group(2)
    return None


def _regex_span(lines: list[str], symbol: str) -> tuple[SymbolSpan | None, str | None]:
    """Regex tier: the span runs from the symbol's declaration line to the line
    before the next declaration (or EOF), trailing blanks trimmed — the same
    block semantics as ``hashing.hash_markdown_defs``."""
    decls = [(i, name) for i, line in enumerate(lines) if (name := _decl_name(line))]
    hits = [idx for idx, (i, name) in enumerate(decls) if name == symbol]
    if not hits:
        return None, f"symbol {symbol!r} not found (regex tier)"
    if len(hits) > 1:
        return None, f"symbol {symbol!r} is ambiguous ({len(hits)} declarations — regex tier)"
    pos = hits[0]
    start = decls[pos][0]
    end = decls[pos + 1][0] - 1 if pos + 1 < len(decls) else len(lines) - 1
    while end > start and not lines[end].strip():
        end -= 1
    return SymbolSpan(start, end, "regex", _hash_span(lines, start, end)), None


def _flatten_symbols(items: list, prefix: str = "") -> list[tuple[str, int, int]]:
    """Flatten an LSP documentSymbol result into ``(qualified_name, start_line,
    end_line)`` triples (0-based). Handles both hierarchical ``DocumentSymbol``
    (``range`` + ``children``) and flat ``SymbolInformation`` (``location``)."""
    out: list[tuple[str, int, int]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        rng = item.get("range") or (item.get("location") or {}).get("range") or {}
        start = (rng.get("start") or {}).get("line")
        end = (rng.get("end") or {}).get("line")
        if not name or start is None or end is None:
            continue
        qual = f"{prefix}{name}"
        out.append((qual, int(start), int(end)))
        out.extend(_flatten_symbols(item.get("children") or [], f"{qual}."))
    return out


def _lsp_span(root: Path, full: Path, symbol: str, lines: list[str]) -> SymbolSpan | None:
    """LSP tier. Returns None (fall through to the regex tier) when no server is
    configured/installed, the server errors, or the symbol isn't uniquely
    matched — the native tiers own the not-found/ambiguous REPORTING."""
    server = lsp.server_for_file(full)
    if server is None or not lsp.server_available(server):
        return None
    try:
        with lsp.LspClient(server, root) as client:
            client.did_open(full)
            symbols = client.document_symbols(full)
    except lsp.LspError:
        return None
    flat = _flatten_symbols(symbols)
    matches = [s for s in flat if s[0] == symbol]
    if not matches:
        matches = [s for s in flat if s[0].split(".")[-1] == symbol]
    if len(matches) != 1:
        return None
    _name, start, end = matches[0]
    start = max(0, min(start, len(lines) - 1))
    end = max(start, min(end, len(lines) - 1))
    return SymbolSpan(start, end, "lsp", _hash_span(lines, start, end))


def resolve_symbol_span(
    target: str | Path, relpath: str, symbol: str, *, use_lsp: bool = True
) -> tuple[SymbolSpan | None, str | None]:
    """Resolve ``relpath :: symbol`` in the target repo to a hashed source span.

    Returns ``(span, None)`` on success, ``(None, reason)`` otherwise — a
    reason is ALWAYS given on failure so callers can surface it (skip-with-
    warning, never a silent drop). Tier order: Python ast (native, exact),
    then LSP where a server is installed, then the emitters' declaration-regex
    tier, then unresolved.
    """
    root = Path(target)
    full = root / relpath
    if not full.is_file():
        return None, f"file not found: {relpath}"
    try:
        text = full.read_text(errors="replace")
    except OSError as exc:
        return None, f"cannot read {relpath}: {exc}"
    lines = text.splitlines()
    suffix = full.suffix
    if suffix == ".py":
        return _ast_span(text, symbol)
    if use_lsp:
        span = _lsp_span(root, full, symbol, lines)
        if span is not None:
            return span, None
    if suffix in cw_languages.all_known_extensions():
        return _regex_span(lines, symbol)
    return None, (
        f"no symbol-resolution tier for {suffix or '(no extension)'} files "
        "(no LSP server, no regex tier — see config/languages.json)"
    )


# --- store ------------------------------------------------------------------------


def _norm_rel(p: str) -> str:
    return PurePosixPath(Path(p)).as_posix()


def _sort_key(entry: dict) -> tuple:
    return (entry.get("file", ""), entry.get("symbol", ""), entry.get("verb", ""))


def load_links(store_path: str | Path) -> dict:
    """Load the store. Missing/malformed degrades to ``{"links": []}`` — the
    exact behavior (and shape) of ``trace_links.load_sidecar``."""
    return load_sidecar(store_path)


def add_link(
    store_path: str | Path,
    target: str | Path,
    file: str,
    symbol: str,
    verb: str,
    ids: list[str],
    *,
    use_lsp: bool = True,
    now: datetime | None = None,
) -> tuple[dict, str | None]:
    """Record (or replace) the link ``file :: symbol -> verb ids`` in the store,
    computing and storing the anchored symbol's content hash plus the target
    HEAD it was computed against.

    An unresolvable anchor is still RECORDED (``symbol_hash: null``) and the
    returned warning explains why — ``verify_links`` will keep surfacing it as
    ``unresolved``. Re-adding the same ``(file, symbol, verb)`` replaces the
    existing entry (the store is a current-claims record, not a history — the
    meta repo's git history is the history).
    """
    verb = verb.lower()
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r} (expected one of {VERBS})")
    canonical = sorted({canonical_id(i) for i in ids if i and i.strip()})
    if not canonical:
        raise ValueError("at least one stable ID is required")
    rel = _norm_rel(file)
    span, reason = resolve_symbol_span(target, rel, symbol, use_lsp=use_lsp)
    entry = {
        "file": rel,
        "symbol": symbol,
        "verb": verb,
        "ids": canonical,
        "symbol_hash": span.hash if span else None,
        "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
        "target_sha": artifacts.head_sha(target),
    }
    store = load_links(store_path)
    links = [
        e for e in store.get("links", [])
        if not (e.get("file") == rel and e.get("symbol") == symbol and e.get("verb") == verb)
    ]
    links.append(entry)
    links.sort(key=_sort_key)
    write_sidecar(store_path, {"links": links})
    warning = None if span else (
        f"link recorded WITHOUT a symbol hash — {rel}::{symbol} could not be "
        f"anchored: {reason}; it will report as unresolved until re-anchored"
    )
    return entry, warning


def verify_links(
    store_path: str | Path, target: str | Path, *, use_lsp: bool = True
) -> dict:
    """Re-anchor every stored link against the target's CURRENT source.

    Returns ``{"ok": [...], "suspect": [...], "unresolved": [...]}``:

    - **ok** — the anchored symbol re-hashes to the recorded hash (entries gain
      ``line``/``tier`` from the fresh resolution).
    - **suspect** — the symbol resolves but its hash differs from the recorded
      one (entries gain ``current_hash``): the claim was validated against a
      symbol that has since changed — re-verify, don't trust. Same discipline
      as ``trace_links.find_suspect_links``.
    - **unresolved** — the file/symbol can no longer be resolved (or the entry
      was recorded without a hash, or is malformed); entries gain ``reason``.
      Surfaced, never dropped.
    """
    result: dict[str, list[dict]] = {"ok": [], "suspect": [], "unresolved": []}
    for entry in load_links(store_path).get("links", []):
        if not isinstance(entry, dict):
            continue
        rel, symbol, verb = entry.get("file"), entry.get("symbol"), entry.get("verb")
        if not rel or not symbol or verb not in VERBS or not entry.get("ids"):
            result["unresolved"].append({
                **(entry if isinstance(entry, dict) else {}),
                "reason": "malformed entry (file/symbol/verb/ids required)",
            })
            continue
        span, reason = resolve_symbol_span(target, rel, symbol, use_lsp=use_lsp)
        if span is None:
            result["unresolved"].append({**entry, "reason": reason})
            continue
        recorded = entry.get("symbol_hash")
        if not recorded:
            result["unresolved"].append({
                **entry,
                "reason": "recorded without a symbol hash (no resolution tier at add time) — re-add to anchor",
            })
            continue
        enriched = {**entry, "line": span.line, "tier": span.tier}
        if span.hash == recorded:
            result["ok"].append(enriched)
        else:
            result["suspect"].append({**enriched, "current_hash": span.hash})
    for key in result:
        result[key].sort(key=_sort_key)
    return result


# --- CLI --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Symbol-anchored external @cw-trace link store (chief-wiggum#213)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="record a link (computes + stores the symbol hash)")
    p.add_argument("store", help="path to external-links.json")
    p.add_argument("--target", required=True, help="target repo root the file lives in")
    p.add_argument("--file", required=True, help="repo-relative source file")
    p.add_argument("--symbol", required=True, help="anchored symbol (qualified names allowed)")
    p.add_argument("--verb", required=True, choices=VERBS)
    p.add_argument("--ids", required=True, nargs="+", help="stable IDs (CTR-/INV-/...)")
    p.add_argument("--no-lsp", action="store_true", help="skip the LSP tier")

    p = sub.add_parser("verify", help="re-anchor every stored link (ok/suspect/unresolved)")
    p.add_argument("store", help="path to external-links.json")
    p.add_argument("--target", required=True, help="target repo root to verify against")
    p.add_argument("--no-lsp", action="store_true", help="skip the LSP tier")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        try:
            entry, warning = add_link(
                args.store, args.target, args.file, args.symbol, args.verb, args.ids,
                use_lsp=not args.no_lsp,
            )
        except ValueError as exc:
            print(f"external-links: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(entry, indent=2))
        if warning:
            print(f"external-links: WARNING: {warning}", file=sys.stderr)
        return 0

    # verify — report-only (docs/gate-rollout.md): findings print, exit stays 0;
    # blocking behavior belongs to the gates that CONSUME the verification
    # (check_traceability's coverage math), not to this store tool.
    result = verify_links(args.store, args.target, use_lsp=not args.no_lsp)
    print(json.dumps({
        "counts": {k: len(v) for k, v in result.items()},
        **result,
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

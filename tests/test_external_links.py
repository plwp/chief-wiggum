"""Tests for the symbol-anchored external @cw-trace link store (#213 Phase C).

Tier behavior is tested WITHOUT any language server (``use_lsp=False`` or a
suffix the LSP registry doesn't cover): the Python ast tier and the emitters'
regex tier must stand on their own; the LSP tier's normalization is unit-tested
purely (``_flatten_symbols``) plus via a monkeypatched span, never by spawning
a real server.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from chief_wiggum import external_links as xl

PY_SRC = """\
def create_order(req):
    if req.start > req.end:
        raise ValueError("bad range")
    return persist(req)


def unrelated():
    return 1


class TestOrders:
    def test_create(self):
        assert create_order is not None
"""

GO_SRC = """\
package orders

func CreateOrder(req Request) error {
\tif req.Start.After(req.End) {
\t\treturn ErrBadRange
\t}
\treturn persist(req)
}

func unrelated() int {
\treturn 1
}
"""

CS_SRC = """\
namespace Orders;

public class OrderService
{
    public void CreateOrder(Request req)
    {
        if (req.Start > req.End)
        {
            throw new ArgumentException("bad range");
        }
        Persist(req);
    }

    public int Unrelated()
    {
        return 1;
    }
}
"""


def _target(tmp_path, name="target"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return root


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# --- tiered symbol resolution -------------------------------------------------


def test_python_ast_tier_resolves_without_lsp(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    span, reason = xl.resolve_symbol_span(root, "orders.py", "create_order", use_lsp=False)
    assert reason is None
    assert span.tier == "ast"
    assert span.start == 0 and span.end == 3
    assert span.hash


def test_python_ast_tier_qualified_name(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    span, reason = xl.resolve_symbol_span(root, "orders.py", "TestOrders.test_create", use_lsp=False)
    assert reason is None
    assert span.tier == "ast"
    # Bare method name resolves too (unique in this file).
    bare, _ = xl.resolve_symbol_span(root, "orders.py", "test_create", use_lsp=False)
    assert bare.hash == span.hash


def test_python_ast_tier_hash_ignores_trailing_whitespace(tmp_path):
    """Same normalization as contract-block/verifier hashing: reformatting
    (trailing whitespace) is not drift; a token change is."""
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    before, _ = xl.resolve_symbol_span(root, "orders.py", "create_order", use_lsp=False)
    _write(root, "orders.py", PY_SRC.replace('raise ValueError("bad range")\n',
                                             'raise ValueError("bad range")   \n'))
    after, _ = xl.resolve_symbol_span(root, "orders.py", "create_order", use_lsp=False)
    assert after.hash == before.hash


def test_python_ambiguous_symbol_is_unresolved_not_guessed(tmp_path):
    root = _target(tmp_path)
    _write(root, "dup.py", "class A:\n    def go(self):\n        pass\n\nclass B:\n    def go(self):\n        pass\n")
    span, reason = xl.resolve_symbol_span(root, "dup.py", "go", use_lsp=False)
    assert span is None
    assert "ambiguous" in reason


def test_python_syntax_error_is_unresolved_with_reason(tmp_path):
    root = _target(tmp_path)
    _write(root, "bad.py", "def broken(:\n")
    span, reason = xl.resolve_symbol_span(root, "bad.py", "broken", use_lsp=False)
    assert span is None
    assert "syntax error" in reason


def test_regex_tier_resolves_go_without_lsp(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.go", GO_SRC)
    span, reason = xl.resolve_symbol_span(root, "orders.go", "CreateOrder", use_lsp=False)
    assert reason is None
    assert span.tier == "regex"
    assert span.start == 2  # the `func CreateOrder` line
    changed = GO_SRC.replace("ErrBadRange", "ErrInvalidRange")
    _write(root, "orders.go", changed)
    span2, _ = xl.resolve_symbol_span(root, "orders.go", "CreateOrder", use_lsp=False)
    assert span2.hash != span.hash


def test_regex_tier_resolves_csharp_without_lsp(tmp_path):
    """chief-wiggum#313: CS_FUNC_RE was never wired into the regex tier here,
    so every C# anchor resolved `unresolved` — coverage was permanently
    unsatisfiable for a C#-on-sidecar target. This is the reference case."""
    root = _target(tmp_path)
    _write(root, "OrderService.cs", CS_SRC)
    span, reason = xl.resolve_symbol_span(root, "OrderService.cs", "CreateOrder", use_lsp=False)
    assert reason is None
    assert span.tier == "regex"
    assert span.start == 4  # the `public void CreateOrder(Request req)` line
    changed = CS_SRC.replace("bad range", "worse range")
    _write(root, "OrderService.cs", changed)
    span2, _ = xl.resolve_symbol_span(root, "OrderService.cs", "CreateOrder", use_lsp=False)
    assert span2.hash != span.hash


def test_csharp_regex_does_not_leak_into_other_suffixes(tmp_path):
    """The C# member pattern is suffix-gated exactly like
    write_emission._enclosing_symbol — a line that only reads as a C# member
    declaration must not resolve for a non-.cs file."""
    root = _target(tmp_path)
    _write(root, "not_csharp.go", "public void CreateOrder(Request req)\n{\n}\n")
    span, reason = xl.resolve_symbol_span(root, "not_csharp.go", "CreateOrder", use_lsp=False)
    assert span is None
    assert "not found" in reason


def test_add_then_verify_ok_for_csharp(tmp_path):
    root = _target(tmp_path)
    _write(root, "OrderService.cs", CS_SRC)
    store = tmp_path / "external-links.json"
    entry, warning = xl.add_link(store, root, "OrderService.cs", "CreateOrder", "guards",
                                 ["CTR-order-001"], use_lsp=False)
    assert entry["symbol_hash"]
    assert warning is None or "target_sha" in warning  # anchored; only the git-binding warning may fire
    result = xl.verify_links(store, root, use_lsp=False)
    assert [e["symbol"] for e in result["ok"]] == ["CreateOrder"]
    assert result["ok"][0]["tier"] == "regex"
    assert result["suspect"] == [] and result["unresolved"] == []


def test_missing_file_and_missing_symbol_are_unresolved(tmp_path):
    root = _target(tmp_path)
    span, reason = xl.resolve_symbol_span(root, "gone.py", "f", use_lsp=False)
    assert span is None and "file not found" in reason
    _write(root, "orders.py", PY_SRC)
    span, reason = xl.resolve_symbol_span(root, "orders.py", "no_such_fn", use_lsp=False)
    assert span is None and "not found" in reason


def test_no_tier_language_is_skip_with_warning(tmp_path):
    """A file with neither LSP nor regex tier never resolves silently — the
    reason names the gap.

    The fixture extension is deliberately fictional (chief-wiggum#413). These
    tests used `.lua`, which encodes the premise of external links — the
    feature exists for languages CW cannot resolve — in a REAL extension's
    tier. So promoting `.lua` broke four tests here as a side effect of a
    write-detection fix in #377, and the tier table is shared state: changing
    an extension's tier is a cross-subsystem decision, not a per-gate one.

    An extension no language will ever claim cannot be promoted, so this
    subsystem stops having an opinion on which real languages are scannable.
    """
    root = _target(tmp_path)
    _write(root, "hook.cwnolang", "function on_save()\n  noop()\nend\n")
    span, reason = xl.resolve_symbol_span(root, "hook.cwnolang", "on_save", use_lsp=True)
    assert span is None
    assert "no symbol-resolution tier" in reason


def test_flatten_symbols_handles_both_lsp_shapes():
    hierarchical = [{
        "name": "TestOrders",
        "range": {"start": {"line": 9}, "end": {"line": 11}},
        "children": [{"name": "test_create", "range": {"start": {"line": 10}, "end": {"line": 11}}}],
    }]
    flat = xl._flatten_symbols(hierarchical)
    assert ("TestOrders", 9, 11) in flat
    assert ("TestOrders.test_create", 10, 11) in flat
    sym_info = [{"name": "CreateOrder", "location": {"range": {"start": {"line": 2}, "end": {"line": 7}}}}]
    assert xl._flatten_symbols(sym_info) == [("CreateOrder", 2, 7)]


def test_lsp_tier_is_used_when_it_yields_a_span(tmp_path, monkeypatch):
    """For a non-Python file, an available LSP span wins over the regex tier
    (monkeypatched — no real server is ever spawned in tests)."""
    root = _target(tmp_path)
    _write(root, "orders.go", GO_SRC)
    fake = xl.SymbolSpan(2, 7, "lsp", "fakehash")
    monkeypatch.setattr(xl, "_lsp_span", lambda *a, **k: fake)
    span, reason = xl.resolve_symbol_span(root, "orders.go", "CreateOrder", use_lsp=True)
    assert reason is None and span is fake
    # And with use_lsp=False the regex tier answers instead.
    span2, _ = xl.resolve_symbol_span(root, "orders.go", "CreateOrder", use_lsp=False)
    assert span2.tier == "regex"


# --- store: add / load / verify ----------------------------------------------


def test_add_then_verify_ok(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "meta" / "quality" / "external-links.json"
    entry, warning = xl.add_link(store, root, "orders.py", "create_order", "guards",
                                 ["CTR-order-001", "inv-order-003"], use_lsp=False)
    # F12: outside a git repo the entry has no target_sha — recorded, but the
    # missing version binding is WARNED about, never silent.
    assert warning is not None and "target_sha" in warning
    assert entry["ids"] == ["CTR-order-001", "INV-order-003"]  # canonicalized + sorted
    assert entry["symbol_hash"]
    assert entry["recorded_at"]
    assert "target_sha" in entry  # None outside a git repo — still recorded
    assert entry["target_sha"] is None
    result = xl.verify_links(store, root, use_lsp=False)
    assert [e["symbol"] for e in result["ok"]] == ["create_order"]
    assert result["ok"][0]["line"] == 1 and result["ok"][0]["tier"] == "ast"
    assert result["suspect"] == [] and result["unresolved"] == []


def test_edited_symbol_goes_suspect(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    xl.add_link(store, root, "orders.py", "create_order", "verifies", ["CTR-order-001"], use_lsp=False)
    _write(root, "orders.py", PY_SRC.replace("req.start > req.end", "True"))
    result = xl.verify_links(store, root, use_lsp=False)
    assert result["ok"] == []
    assert len(result["suspect"]) == 1
    sus = result["suspect"][0]
    assert sus["current_hash"] != sus["symbol_hash"]


def test_deleted_file_and_deleted_symbol_are_unresolved_surfaced(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    xl.add_link(store, root, "orders.py", "create_order", "guards", ["CTR-order-001"], use_lsp=False)
    xl.add_link(store, root, "orders.py", "unrelated", "guards", ["CTR-order-002"], use_lsp=False)
    # Symbol deleted: file rewritten without create_order.
    _write(root, "orders.py", "def unrelated():\n    return 1\n")
    result = xl.verify_links(store, root, use_lsp=False)
    assert [e["symbol"] for e in result["unresolved"]] == ["create_order"]
    assert "not found" in result["unresolved"][0]["reason"]
    assert [e["symbol"] for e in result["ok"]] == ["unrelated"]
    # File deleted: everything unresolved, nothing dropped.
    (root / "orders.py").unlink()
    result = xl.verify_links(store, root, use_lsp=False)
    assert result["ok"] == [] and result["suspect"] == []
    assert len(result["unresolved"]) == 2
    assert all("file not found" in e["reason"] for e in result["unresolved"])


def test_add_without_tier_records_unanchored_entry_with_warning(tmp_path):
    root = _target(tmp_path)
    _write(root, "hook.cwnolang", "function on_save()\nend\n")
    store = tmp_path / "external-links.json"
    entry, warning = xl.add_link(store, root, "hook.cwnolang", "on_save", "guards", ["CTR-x-001"])
    assert entry["symbol_hash"] is None
    assert warning and "WITHOUT a symbol hash" in warning
    # Never dropped: verify keeps surfacing it as unresolved.
    result = xl.verify_links(store, root)
    assert len(result["unresolved"]) == 1
    assert "no symbol-resolution tier" in result["unresolved"][0]["reason"]


def test_readd_same_anchor_replaces_not_duplicates(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    xl.add_link(store, root, "orders.py", "create_order", "guards", ["CTR-a-001"], use_lsp=False)
    xl.add_link(store, root, "orders.py", "create_order", "guards", ["CTR-b-001"], use_lsp=False)
    links = xl.load_links(store)["links"]
    assert len(links) == 1
    assert links[0]["ids"] == ["CTR-b-001"]
    # A different verb on the same anchor is a distinct entry.
    xl.add_link(store, root, "orders.py", "create_order", "ensures", ["CTR-a-001"], use_lsp=False)
    assert len(xl.load_links(store)["links"]) == 2


def test_add_validates_verb_and_ids(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    with pytest.raises(ValueError, match="unknown verb"):
        xl.add_link(store, root, "orders.py", "create_order", "realizes", ["CTR-a-001"])
    with pytest.raises(ValueError, match="stable ID"):
        xl.add_link(store, root, "orders.py", "create_order", "guards", [])


def test_load_links_degrades_on_missing_and_malformed(tmp_path):
    assert xl.load_links(tmp_path / "nope.json") == {"links": []}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert xl.load_links(bad) == {"links": []}


def test_malformed_entry_is_unresolved_not_crash(tmp_path):
    store = tmp_path / "external-links.json"
    store.write_text(json.dumps({"links": [{"file": "x.py"}]}))
    result = xl.verify_links(store, tmp_path)
    assert len(result["unresolved"]) == 1
    assert "malformed" in result["unresolved"][0]["reason"]


def test_target_sha_recorded_in_git_repo(tmp_path):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "x"], cwd=root, check=True)
    store = tmp_path / "external-links.json"
    entry, warning = xl.add_link(store, root, "orders.py", "create_order", "guards",
                                 ["CTR-a-001"], use_lsp=False)
    assert entry["target_sha"] and len(entry["target_sha"]) == 40
    assert warning is None  # anchored AND version-bound: nothing to warn about


# --- store applicability (chief-wiggum#313 item 3) ---------------------------
#
# An empty store ("nothing authored yet") and a fully-populated-but-broken
# store ("every entry present, NONE anchor") used to look identical to a
# consumer that only reads counts — both report `unresolved: []`-shaped
# vacuously or `ok: []` alike. That is the same absence-reads-as-normal shape
# #289 named for check_traceability.py; the same pass|findings|inapplicable|
# error vocabulary applies here, at the store's own granularity.


def test_empty_store_is_inapplicable(tmp_path):
    result = {"ok": [], "suspect": [], "unresolved": []}
    assert xl.store_applicability(result) == "inapplicable"


def test_fully_unresolved_populated_store_is_error(tmp_path):
    root = _target(tmp_path)
    _write(root, "hook.cwnolang", "function on_save()\nend\n")
    store = tmp_path / "external-links.json"
    xl.add_link(store, root, "hook.cwnolang", "on_save", "guards", ["CTR-x-001"])
    xl.add_link(store, root, "hook.cwnolang", "on_save2", "verifies", ["CTR-x-001"])
    result = xl.verify_links(store, root)
    assert result["ok"] == [] and result["suspect"] == []
    assert len(result["unresolved"]) == 2
    assert xl.store_applicability(result) == "error"


def test_partially_resolved_store_is_ok_not_error(tmp_path):
    """Only a store where NOTHING anchors is the defect — one healthy entry
    alongside broken ones stays 'ok' (the existing warning-per-entry behavior
    is unaffected)."""
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    xl.add_link(store, root, "orders.py", "create_order", "guards", ["CTR-a-001"], use_lsp=False)
    xl.add_link(store, root, "orders.py", "no_such_fn", "verifies", ["CTR-b-001"], use_lsp=False)
    result = xl.verify_links(store, root, use_lsp=False)
    assert len(result["ok"]) == 1 and len(result["unresolved"]) == 1
    assert xl.store_applicability(result) == "ok"


def test_cli_verify_prints_measured_denominator(tmp_path, capsys):
    root = _target(tmp_path)
    _write(root, "hook.cwnolang", "function on_save()\nend\n")
    store = tmp_path / "external-links.json"
    xl.main(["add", str(store), "--target", str(root), "--file", "hook.cwnolang",
             "--symbol", "on_save", "--verb", "guards", "--ids", "CTR-x-001"])
    capsys.readouterr()
    rc = xl.main(["verify", str(store), "--target", str(root)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applicability"] == "error"
    assert out["anchored"] == "0 of 1 entries"


# --- CLI ----------------------------------------------------------------------


def test_cli_add_and_verify_round_trip(tmp_path, capsys):
    root = _target(tmp_path)
    _write(root, "orders.py", PY_SRC)
    store = tmp_path / "external-links.json"
    rc = xl.main(["add", str(store), "--target", str(root), "--file", "orders.py",
                  "--symbol", "create_order", "--verb", "guards",
                  "--ids", "CTR-order-001", "INV-order-003", "--no-lsp"])
    assert rc == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["symbol_hash"]
    rc = xl.main(["verify", str(store), "--target", str(root), "--no-lsp"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"] == {"ok": 1, "suspect": 0, "unresolved": 0}


def test_cli_add_rejects_bad_verb_via_argparse(tmp_path):
    with pytest.raises(SystemExit):
        xl.main(["add", str(tmp_path / "s.json"), "--target", str(tmp_path), "--file", "x.py",
                 "--symbol", "f", "--verb", "realizes", "--ids", "CTR-a-001"])

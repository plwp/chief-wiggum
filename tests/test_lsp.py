"""Tests for the LSP semantic-intelligence client (#35).

Framing/normalization are unit-tested with no subprocess. Integration tests run
against real ``gopls`` / ``pyright`` and are skipped when those aren't installed,
so CI without them stays green; they are exercised locally where they exist.
"""

from __future__ import annotations

import json
import shutil

import lsp_query
import pytest
from chief_wiggum import lsp

HAVE_GOPLS = shutil.which("gopls") is not None and shutil.which("go") is not None
HAVE_PYRIGHT = shutil.which("pyright-langserver") is not None


# --- pure framing -----------------------------------------------------------


def test_encode_uses_byte_length_not_char_length():
    msg = {"k": "é"}  # 'é' is 2 bytes in UTF-8
    raw = lsp.encode_message(msg)
    header, _, body = raw.partition(b"\r\n\r\n")
    assert f"Content-Length: {len(body)}".encode() in header
    assert len(body) == len(json.dumps(msg).encode("utf-8"))


def test_buffer_parses_single_message():
    buf = lsp.MessageBuffer()
    assert buf.push(lsp.encode_message({"id": 1, "result": "ok"})) == [{"id": 1, "result": "ok"}]


def test_buffer_handles_partial_reads():
    buf = lsp.MessageBuffer()
    raw = lsp.encode_message({"id": 1, "result": "ok"})
    out = []
    for i in range(0, len(raw)):  # one byte at a time
        out += buf.push(raw[i: i + 1])
    assert out == [{"id": 1, "result": "ok"}]


def test_buffer_handles_multiple_messages_in_one_chunk():
    buf = lsp.MessageBuffer()
    raw = lsp.encode_message({"id": 1}) + lsp.encode_message({"id": 2})
    assert buf.push(raw) == [{"id": 1}, {"id": 2}]


def test_buffer_preserves_incomplete_trailing():
    buf = lsp.MessageBuffer()
    raw = lsp.encode_message({"id": 1}) + lsp.encode_message({"id": 2})
    first = buf.push(raw[: len(raw) - 5])  # cut the 2nd message short
    assert first == [{"id": 1}]
    assert buf.push(raw[len(raw) - 5:]) == [{"id": 2}]


def test_buffer_case_insensitive_header():
    buf = lsp.MessageBuffer()
    body = b'{"id": 9}'
    raw = b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    assert buf.push(raw) == [{"id": 9}]


def test_buffer_rejects_missing_content_length():
    buf = lsp.MessageBuffer()
    with pytest.raises(lsp.LspError):
        buf.push(b"X-Other: 1\r\n\r\n{}")


# --- server config ----------------------------------------------------------


def test_server_for_file_by_extension():
    assert lsp.server_for_file("a.go") is lsp.GOPLS
    assert lsp.server_for_file("a.py") is lsp.PYRIGHT
    assert lsp.server_for_file("a.txt") is None


def test_server_available(monkeypatch):
    monkeypatch.setattr(lsp.shutil, "which", lambda c: "/bin/" + c if c == "gopls" else None)
    assert lsp.server_available(lsp.GOPLS) is True
    assert lsp.server_available(lsp.PYRIGHT) is False


# --- result normalization ---------------------------------------------------


def test_location_to_dict():
    loc = {"uri": "file:///tmp/x.go", "range": {"start": {"line": 4, "character": 6}}}
    d = lsp._location_to_dict(loc)
    assert d == {"file": "/tmp/x.go", "line": 4, "col": 6, "uri": "file:///tmp/x.go"}


def test_diagnostic_to_dict_maps_severity():
    d = lsp._diagnostic_to_dict({"range": {"start": {"line": 2, "character": 8}}, "severity": 1, "message": "boom"})
    assert d["severity"] == "error" and d["line"] == 2 and d["message"] == "boom"


def test_hover_text_from_markup_and_list():
    assert lsp._hover_text({"kind": "markdown", "value": "func F()"}) == "func F()"
    assert lsp._hover_text([{"value": "a"}, {"value": "b"}]) == "a\nb"


# --- CLI graceful degradation -----------------------------------------------


def test_cli_unknown_extension_is_graceful(tmp_path, capsys):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    rc = lsp_query.main(["diagnostics", str(f), "--root", str(tmp_path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["available"] is False


def test_cli_missing_server_is_graceful(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(lsp, "server_available", lambda s: False)
    f = tmp_path / "x.go"
    f.write_text("package main\n")
    rc = lsp_query.main(["diagnostics", str(f), "--root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["available"] is False and "not installed" in out["reason"]


# --- integration: real gopls ------------------------------------------------


def _go_module(tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "go.mod").write_text("module example.com/fix\n\ngo 1.21\n")
    (mod / "main.go").write_text(
        'package main\n\nfunc Greet(name string) string {\n\treturn "hi " + name\n}\n\n'
        'func main() {\n\t_ = Greet("x")\n}\n'
    )
    (mod / "broken.go").write_text("package main\n\nvar _ = DoesNotExist\n")
    return mod


@pytest.mark.skipif(not HAVE_GOPLS, reason="gopls/go not installed")
def test_gopls_definition_hover_diagnostics(tmp_path):
    mod = _go_module(tmp_path)
    with lsp.LspClient(lsp.GOPLS, mod, timeout=40) as c:
        c.did_open(mod / "main.go")
        # 'Greet' usage on the `_ = Greet("x")` line (0-based line 7, col on Greet).
        defs = c.definition(mod / "main.go", 7, 6)
        assert defs and defs[0]["file"].endswith("main.go") and defs[0]["line"] == 2
        hover = c.hover(mod / "main.go", 2, 5)
        assert "Greet" in (hover["signature"] or "")
        c.did_open(mod / "broken.go")
        diags = c.diagnostics(mod / "broken.go", timeout=20)
        assert any("DoesNotExist" in d["message"] for d in diags)


@pytest.mark.skipif(not HAVE_GOPLS, reason="gopls/go not installed")
def test_cli_diagnostics_against_real_gopls(tmp_path, capsys):
    mod = _go_module(tmp_path)
    rc = lsp_query.main(["diagnostics", str(mod / "broken.go"), "--root", str(mod), "--timeout", "30"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["available"] is True
    assert any("DoesNotExist" in d["message"] for d in out["result"])


# --- integration: real pyright ----------------------------------------------


@pytest.mark.skipif(not HAVE_PYRIGHT, reason="pyright-langserver not installed")
def test_pyright_definition_and_diagnostics(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text('def greet(name):\n    return "hi " + name\n\n\nx = greet("a")\ny = undefined_symbol\n')
    with lsp.LspClient(lsp.PYRIGHT, tmp_path, timeout=40) as c:
        c.did_open(f)
        defs = c.definition(f, 4, 4)  # greet() call -> def greet (line 0)
        assert defs and defs[0]["line"] == 0
        diags = c.diagnostics(f, timeout=20)
        assert any("undefined_symbol" in d["message"] for d in diags)

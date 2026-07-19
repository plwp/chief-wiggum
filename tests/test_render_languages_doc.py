"""Tests for scripts/render_languages_doc.py (#162): docs/languages.md is
mechanically rendered from config/languages.json, never hand-edited."""

from __future__ import annotations

import json

import render_languages_doc as rld


def test_render_includes_every_language():
    text = rld.render()
    assert "| go |" in text
    assert "| python |" in text
    assert "| typescript |" in text
    assert "| rust |" in text


def test_render_includes_generic_and_unsupported_sections():
    text = rld.render()
    assert "## Generic regex tier" in text
    assert "`.java`" in text and "`.rb`" in text
    assert "## Recognized-but-unsupported extensions" in text
    assert "`.php`" in text


def test_render_documents_rust_designed_slot():
    text = rld.render()
    assert "## Designed, unbuilt slots" in text
    assert "### Rust" in text
    assert "first real Rust target repo" in text
    assert "rust-analyzer" in text


def test_committed_doc_is_up_to_date():
    """Regression guard: docs/languages.md must always match a fresh render
    of config/languages.json — the doc is generated, never hand-edited."""
    rendered = rld.render()
    committed = rld.DEFAULT_OUTPUT.read_text()
    assert committed == rendered


def test_cli_check_passes_when_up_to_date(capsys):
    rc = rld.main(["--check"])
    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_cli_check_fails_when_stale(tmp_path, capsys):
    stale = tmp_path / "languages.md"
    stale.write_text("stale content\n")
    rc = rld.main(["--check", "--output", str(stale)])
    assert rc == 1
    assert "stale" in capsys.readouterr().err


def test_cli_writes_output_file(tmp_path):
    out = tmp_path / "out.md"
    rc = rld.main(["--output", str(out)])
    assert rc == 0
    assert out.exists()
    assert "# Language Support Matrix" in out.read_text()


def test_render_uses_custom_config(tmp_path):
    custom = tmp_path / "languages.json"
    custom.write_text(json.dumps({
        "languages": {
            "elixir": {
                "tier": "designed", "status": "designed, unbuilt",
                "extensions": [".ex"], "lsp": "elixir-ls", "emitters": [],
                "func_regex": False, "trigger": "first Elixir target repo",
                "requires": ["placeholder"],
            }
        },
        "generic_tier": {"extensions": []},
        "unsupported_extensions": {"extensions": [".ex"]},
    }))
    text = rld.render(custom)
    assert "elixir" in text
    assert "first Elixir target repo" in text

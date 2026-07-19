"""Tests for chief_wiggum.languages (#162): the config/languages.json loader."""

from __future__ import annotations

import json

import pytest
from chief_wiggum import languages as cw_languages


def test_load_reads_real_config():
    data = cw_languages.load()
    assert "go" in data["languages"]
    assert "python" in data["languages"]
    assert "typescript" in data["languages"]
    assert "rust" in data["languages"]


def test_go_python_typescript_are_tier_1_built():
    langs = cw_languages.languages()
    assert langs["go"].built is True
    assert langs["python"].built is True
    assert langs["typescript"].built is True


def test_rust_is_designed_not_built():
    langs = cw_languages.languages()
    rust = langs["rust"]
    assert rust.built is False
    assert rust.tier == "designed"
    assert rust.func_regex is False
    assert rust.trigger == "first real Rust target repo"
    assert rust.requires  # documents what's needed when triggered


def test_extension_to_language_only_includes_built_languages():
    mapping = cw_languages.extension_to_language()
    assert mapping[".go"] == "go"
    assert mapping[".py"] == "python"
    assert mapping[".ts"] == "typescript"
    assert mapping[".tsx"] == "typescript"
    # Rust is designed-but-unbuilt: its extension must NOT resolve to a
    # tier-1 language module.
    assert ".rs" not in mapping


def test_generic_tier_extensions_include_rust_java_ruby():
    generic = cw_languages.generic_tier_extensions()
    assert {".rs", ".java", ".rb"} <= generic


def test_all_known_extensions_matches_pre_162_source_exts():
    """Regression guard: the pre-#162 hardcoded SOURCE_EXTS in
    check_single_writer.py was exactly this 9-extension set."""
    expected = {".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs"}
    assert cw_languages.all_known_extensions() == frozenset(expected)


def test_unsupported_extensions_are_disjoint_from_known():
    known = cw_languages.all_known_extensions()
    unsupported = cw_languages.unsupported_extensions()
    assert known.isdisjoint(unsupported)
    assert ".php" in unsupported
    assert ".cpp" in unsupported


def test_config_is_valid_json_with_expected_top_level_keys(tmp_path):
    # Sanity: the shipped config file itself parses and has the sections the
    # loader expects (belt-and-suspenders on top of test_load_reads_real_config).
    data = json.loads(cw_languages.DEFAULT_PATH.read_text())
    assert set(data) >= {"languages", "generic_tier", "unsupported_extensions"}


def test_load_is_cached_per_path(tmp_path):
    custom = tmp_path / "languages.json"
    custom.write_text(json.dumps({"languages": {"foo": {"tier": 1, "extensions": [".foo"]}}}))
    data1 = cw_languages.load(custom)
    data2 = cw_languages.load(custom)
    assert data1 is data2  # lru_cache returns the identical object


def test_custom_path_round_trips_language_fields(tmp_path):
    custom = tmp_path / "languages2.json"
    custom.write_text(json.dumps({
        "languages": {
            "elixir": {
                "tier": "designed",
                "status": "designed, unbuilt",
                "extensions": [".ex", ".exs"],
                "lsp": "elixir-ls",
                "emitters": [],
                "func_regex": False,
            }
        },
        "generic_tier": {"extensions": []},
        "unsupported_extensions": {"extensions": [".ex", ".exs"]},
    }))
    langs = cw_languages.languages(custom)
    assert langs["elixir"].extensions == (".ex", ".exs")
    assert langs["elixir"].built is False
    assert cw_languages.unsupported_extensions(custom) == frozenset({".ex", ".exs"})


@pytest.mark.parametrize("name", ["go", "python", "typescript", "rust"])
def test_every_matrix_language_has_extensions(name):
    lang = cw_languages.languages()[name]
    assert lang.extensions, f"{name} must declare at least one extension"

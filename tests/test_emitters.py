"""Tests for scripts/emitters/ (#162): the per-language emitter interface and
its fallback chain (language-specific emitter -> generic regex tier ->
skip-with-warning/unsupported).
"""

from __future__ import annotations

import emitters
from chief_wiggum.trace_emission import Annotation
from chief_wiggum.write_emission import KIND_ASSIGN, WriteSite
from emitters import base as emitters_base
from emitters import generic, go, python, typescript

# --- per-language modules: shape + delegation -------------------------------


def test_go_module_shape():
    assert go.language == "go"
    assert go.extensions == (".go",)
    assert set(go.fact_kinds()) == {"write_site", "trace_annotation"}


def test_python_module_shape():
    assert python.language == "python"
    assert python.extensions == (".py",)


def test_typescript_module_covers_four_extensions():
    assert typescript.language == "typescript"
    assert set(typescript.extensions) == {".ts", ".tsx", ".js", ".jsx"}


def test_generic_module_covers_configured_generic_tier():
    from chief_wiggum import languages as cw_languages

    assert set(generic.extensions) == cw_languages.generic_tier_extensions()


def test_go_emit_produces_write_site_fact_matching_direct_call():
    """The emitter module must be a pure DELEGATION — its write_site facts
    are byte-identical to calling chief_wiggum.write_emission directly."""
    content = "func ChangePlan(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    from chief_wiggum.write_emission import emit_write_sites

    direct = emit_write_sites("admin.go", content)
    facts = go.emit("admin.go", content)
    write_sites = [f.payload for f in facts if f.kind == "write_site"]
    assert write_sites == direct
    assert all(isinstance(s, WriteSite) for s in write_sites)


def test_go_emit_produces_trace_annotation_fact_matching_direct_call():
    content = "// @cw-trace guards CTR-order-001\n"
    from chief_wiggum.trace_emission import emit_source_annotations

    direct = emit_source_annotations("order.go", content, ".go")
    facts = go.emit("order.go", content)
    anns = [f.payload for f in facts if f.kind == "trace_annotation"]
    assert anns == direct
    assert all(isinstance(a, Annotation) for a in anns)


def test_python_emit_delegates_identically():
    content = "def f():\n    # plan: the free tier\n    return 1\n"
    from chief_wiggum.write_emission import emit_write_sites

    facts = python.emit("svc.py", content)
    direct = emit_write_sites("svc.py", content)
    assert [f.payload for f in facts if f.kind == "write_site"] == direct


def test_typescript_emit_uses_correct_suffix_for_tsx():
    # A .tsx file's comment-marker/enclosing-symbol handling must match calling
    # emit_write_sites directly with the .tsx path (suffix-driven).
    content = "class P {\n  #plan = 'free';\n}\n"
    from chief_wiggum.write_emission import emit_write_sites

    facts = typescript.emit("m.tsx", content)
    direct = emit_write_sites("m.tsx", content)
    assert [f.payload for f in facts if f.kind == "write_site"] == direct


# --- fact helpers ------------------------------------------------------------


def test_facts_of_kind_unwraps_payloads():
    facts = go.emit("admin.go", "func F(p *Provider) {\n\tp.Plan = \"x\"\n}\n")
    sites = emitters_base.facts_of_kind(facts, "write_site")
    assert sites and all(isinstance(s, WriteSite) for s in sites)
    assert sites[0].kind == KIND_ASSIGN


# --- registry / fallback chain -----------------------------------------------


def test_tier_for_suffix_language():
    assert emitters.tier_for_suffix(".go") == "language"
    assert emitters.tier_for_suffix(".py") == "language"
    assert emitters.tier_for_suffix(".ts") == "language"


def test_tier_for_suffix_generic():
    assert emitters.tier_for_suffix(".java") == "generic"
    assert emitters.tier_for_suffix(".rb") == "generic"
    # Rust is designed-but-unbuilt at tier-1: it falls to the generic tier.
    assert emitters.tier_for_suffix(".rs") == "generic"


def test_tier_for_suffix_unsupported():
    assert emitters.tier_for_suffix(".php") == "unsupported"
    assert emitters.tier_for_suffix(".cpp") == "unsupported"


def test_emit_dispatches_to_language_tier():
    facts, tier = emitters.emit("admin.go", "func F(p *Provider) {\n\tp.Plan = \"x\"\n}\n")
    assert tier == "language"
    assert facts and facts[0].kind == "write_site"


def test_emit_dispatches_to_generic_tier_for_rust():
    facts, tier = emitters.emit(
        "lib.rs", "fn change_plan(p: &mut Provider) {\n\tp.plan = \"x\";\n}\n"
    )
    assert tier == "generic"
    # Struct-field assignment is still caught by the generic regex family.
    assert any(f.kind == "write_site" for f in facts)


def test_emit_returns_empty_for_unsupported():
    facts, tier = emitters.emit("app.php", "<?php $x = 1;\n")
    assert facts == []
    assert tier == "unsupported"


def test_emitter_for_suffix_returns_module_or_none():
    assert emitters.emitter_for_suffix(".go") is go
    assert emitters.emitter_for_suffix(".java") is generic
    assert emitters.emitter_for_suffix(".php") is None


def test_is_recognized_unsupported():
    assert emitters.is_recognized_unsupported(".php") is True
    assert emitters.is_recognized_unsupported(".go") is False
    # A totally arbitrary/unknown extension is not "recognized" — no
    # coverage-warning noise for e.g. lockfiles or data files.
    assert emitters.is_recognized_unsupported(".lock") is False


# --- matrix <-> registry parity (#162 review) ---------------------------------


def test_registry_matches_declared_matrix():
    """Mechanical parity: the declared matrix (config/languages.json) and the
    actual emitter registry may never drift — every built language has a
    registered emitter of the same name covering exactly its declared
    extensions, no strays in either direction, and the generic module covers
    exactly the matrix's generic tier."""
    assert emitters.validate_registry_matches_matrix() == []


def test_registered_language_extensions_equal_matrix_mapping():
    from chief_wiggum import languages as cw_languages

    assert emitters.registered_language_extensions() == cw_languages.extension_to_language()


def test_validator_reports_matrix_extension_with_no_emitter(monkeypatch):
    # Matrix declares a built language extension the registry doesn't cover.
    monkeypatch.setattr(
        emitters.cw_languages,
        "extension_to_language",
        lambda path=None: {**emitters.registered_language_extensions(), ".zig": "zig"},
    )
    problems = emitters.validate_registry_matches_matrix()
    assert any(".zig" in p and "no emitter module is registered" in p for p in problems)


def test_validator_reports_registered_extension_not_in_matrix(monkeypatch):
    # Registry covers an extension the matrix doesn't declare for a built language.
    declared = {
        ext: lang
        for ext, lang in emitters.registered_language_extensions().items()
        if ext != ".go"
    }
    monkeypatch.setattr(
        emitters.cw_languages, "extension_to_language", lambda path=None: declared
    )
    problems = emitters.validate_registry_matches_matrix()
    assert any(".go" in p and "does not" in p and "declare" in p for p in problems)


def test_validator_reports_language_name_mismatch(monkeypatch):
    declared = dict(emitters.registered_language_extensions())
    declared[".go"] = "golang"  # matrix name diverges from the module's name
    monkeypatch.setattr(
        emitters.cw_languages, "extension_to_language", lambda path=None: declared
    )
    problems = emitters.validate_registry_matches_matrix()
    assert any("golang" in p and ".go" in p for p in problems)


def test_validator_reports_generic_tier_drift(monkeypatch):
    monkeypatch.setattr(
        emitters.cw_languages,
        "generic_tier_extensions",
        lambda path=None: frozenset({".java", ".rb", ".rs", ".kt"}),
    )
    problems = emitters.validate_registry_matches_matrix()
    assert any("generic" in p for p in problems)

"""Embedded-script (Redis Lua) writers and blind coverage (chief-wiggum#377).

The reported incident: on a Go+Redis service, `check_single_writer` ran three
times and caught 0 findings in 0ms. Both single-write-path invariants were
written by atomic Lua scripts, every "writer" it did list was a test fixture,
and one field had no writer at all — yet the coverage gate exited 0. A second
Lua writer of either field would not have been caught, which is the exact class
the gate exists to catch.

The negative test at the bottom is the one the epic-level quorum asked for:
introduce a second Redis writer and prove the gate fails. A detector nobody has
seen fail is not evidence of teeth.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_single_writer as sw  # noqa: E402
from chief_wiggum.write_emission import (  # noqa: E402
    KIND_SCRIPT,
    emit_write_sites,
)

MINT_SCRIPT = """package admission

var mintScript = redis.NewScript(`
	redis.call('HSET', KEYS[1], 'claimed_venue', ARGV[1])
	return 1
`)

func Mint(ctx context.Context) error {
	return mintScript.Run(ctx, rdb, keys).Err()
}
"""


def _epic(tmp_path, field="claimed_venue", writers=("Mint", "session.go")):
    """The prose form with a @cw-writes tag, matching the shipped fixtures."""
    epic = tmp_path / "epic"
    epic.mkdir(parents=True, exist_ok=True)
    (epic / "invariants.md").write_text(
        "# Invariants\n\n"
        f"**INV-relay-032**: {field} has a single write path\n"
        f"<!-- @cw-writes INV-relay-032 controls_field={field} "
        f"sanctioned_writers={','.join(writers)} -->\n"
    )
    return epic


class TestEmission:
    def test_a_lua_hset_in_a_go_string_is_a_write_site(self):
        """The reported shape: an inline Lua script inside a Go file."""
        sites = emit_write_sites("internal/admission/session.go", MINT_SCRIPT)
        script_sites = [s for s in sites if s.kind == KIND_SCRIPT]
        assert [s.token for s in script_sites] == ["claimed_venue"]

    def test_the_redis_command_is_not_reported_as_a_field(self):
        sites = emit_write_sites("x.go", MINT_SCRIPT)
        assert "HSET" not in [s.token for s in sites]

    def test_one_write_produces_one_site_not_two(self):
        """KIND_QUOTED and KIND_SCRIPT must not both claim the same write."""
        sites = [s for s in emit_write_sites("x.go", MINT_SCRIPT)
                 if s.token == "claimed_venue"]
        assert len(sites) == 1

    def test_lua_content_is_handled_when_it_reaches_the_emitter(self):
        """Inline Lua is the reported case and is scanned because its HOST file
        is (a .go file). Standalone .lua files remain outside the scanned set —
        see TestScopeBoundary below for why that stayed deliberate."""
        lua = "redis.call('HSET', KEYS[1], 'contact_binding', ARGV[1])\n"
        tokens = [s.token for s in emit_write_sites("scripts/bind.lua", lua)]
        assert "contact_binding" in tokens

    def test_a_lua_comment_is_not_a_write(self):
        lua = "-- HSET KEYS[1] 'contact_binding' ARGV[1]\nreturn 1\n"
        assert emit_write_sites("scripts/bind.lua", lua) == []

    def test_a_redis_read_is_not_a_write(self):
        """Counting reads would turn a single-writer gate into noise."""
        go = "redis.call('HGET', KEYS[1], 'claimed_venue')\n"
        assert [s for s in emit_write_sites("x.go", go) if s.kind == KIND_SCRIPT] == []

    @pytest.mark.parametrize("command", ["HSET", "HMSET", "SET", "HSETNX", "ZADD", "SADD"])
    def test_every_declared_write_command_is_recognised(self, command):
        go = f"redis.call('{command}', KEYS[1], 'claimed_venue', ARGV[1])\n"
        tokens = [s.token for s in emit_write_sites("x.go", go) if s.kind == KIND_SCRIPT]
        assert "claimed_venue" in tokens

    def test_the_enclosing_symbol_is_captured(self):
        """Sanctioning works by symbol name, so a script write needs one too."""
        go = ("func Mint() {\n"
              "\tredis.call('HSET', KEYS[1], 'claimed_venue', ARGV[1])\n}\n")
        site = [s for s in emit_write_sites("x.go", go) if s.kind == KIND_SCRIPT][0]
        assert site.symbol == "Mint"


class TestGateSeesLuaWriters:
    def test_a_sanctioned_lua_writer_is_recognised(self, tmp_path):
        epic = _epic(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / "session.go").write_text(MINT_SCRIPT)
        report = sw.check(epic, src)
        assert report.writers, "the Lua write must be seen at all"
        assert not report.violations
        assert not report.blind, "a field with a visible writer is not blind"

    def test_an_unsanctioned_second_lua_writer_is_a_violation(self, tmp_path):
        """THE negative test the quorum asked for: prove the gate has teeth.

        A second Redis writer of a single-write-path field is the exact class
        this gate exists to catch, and before #377 it was invisible.
        """
        epic = _epic(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / "session.go").write_text(MINT_SCRIPT)
        (src / "rogue.go").write_text(
            "package rogue\n\n"
            "func Overwrite() {\n"
            "\tredis.call('HSET', KEYS[1], 'claimed_venue', 'stolen')\n"
            "}\n"
        )
        report = sw.check(epic, src)
        assert report.violations, "a second Lua writer must be caught"
        assert not report.coverage_ok, "and it must fail the coverage gate"
        assert any(v.get("symbol") == "Overwrite" for v in report.violations)

    def test_the_root_cause_is_pinned(self):
        """WHY a Lua write was invisible, stated directly.

        The old mutation-context marker was `\\bSET\\b`, which does NOT match
        "HSET": the S is preceded by a word character, so there is no word
        boundary. Every Redis hash write therefore looked like ordinary prose
        and produced no write site at all. Simulating the old code by
        monkeypatching regexes proved fragile — I got that wrong once — so this
        pins the actual root cause instead.
        """
        import re

        line = "redis.call('HSET', KEYS[1], 'claimed_venue', ARGV[1])"
        assert re.search(r"\bSET\b", line, re.IGNORECASE) is None, (
            "if this ever matches, the original bug could not have happened"
        )
        # And the replacement does see it.
        from chief_wiggum.write_emission import MUTATION_CONTEXT_RE

        assert MUTATION_CONTEXT_RE.search(line)


class TestBlindCoverage:
    def test_a_field_with_no_parseable_writer_is_blind_not_pass(self, tmp_path):
        epic = _epic(tmp_path, field="contact_binding", writers=("Bind", "bind.go"))
        src = tmp_path / "src"
        src.mkdir()
        (src / "unrelated.go").write_text("package x\nfunc f() { y := 1; _ = y }\n")
        report = sw.check(epic, src)
        assert report.blind
        assert "contact_binding" in report.blind[0]["field"]
        assert report.outcome == "blind"
        assert report.outcome != "pass"

    def test_the_blind_reason_names_the_likely_cause(self, tmp_path):
        """'may be Lua/stored-proc' is what turns a shrug into a next step."""
        epic = _epic(tmp_path, field="contact_binding", writers=("Bind", "bind.go"))
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.go").write_text("package x\n")
        report = sw.check(epic, src)
        assert "embedded script" in report.blind[0]["reason"]
        assert any("coverage BLIND" in w for w in report.warnings)

    def test_blind_is_counted_and_serialised(self, tmp_path):
        epic = _epic(tmp_path, field="contact_binding", writers=("Bind", "bind.go"))
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.go").write_text("package x\n")
        payload = sw.check(epic, src).to_dict()
        assert payload["counts"]["blind"] == 1
        assert payload["blind"][0]["invariant_id"] == "INV-relay-032"

    def test_blind_is_report_only_for_now(self, tmp_path):
        """docs/gate-rollout.md: a NEW blocking behaviour ships report-only and
        is validated before it blocks. It changes what the gate SAYS."""
        epic = _epic(tmp_path, field="contact_binding", writers=("Bind", "bind.go"))
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.go").write_text("package x\n")
        report = sw.check(epic, src)
        assert report.coverage_ok, "blind does not yet block"
        assert report.outcome == "blind", "but it is not a pass either"

    def test_a_seen_field_is_never_blind(self, tmp_path):
        epic = _epic(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / "session.go").write_text(MINT_SCRIPT)
        assert sw.check(epic, src).blind == []


class TestScopeBoundary:
    """Why standalone .lua is still not in the scanned set.

    I moved it to the generic tier first, and test_external_links caught it:
    that subsystem uses `hook.lua` as its canonical UNSCANNABLE language, since
    external links exist precisely for languages CW cannot resolve. Promoting
    .lua silently redefined a language's status across a whole subsystem as a
    side effect of a write-detection fix.

    The reported incident was inline Lua inside a Go file, which is fixed
    without any tier change. Standalone .lua scanning needs its own design pass
    and is filed as #413 rather than smuggled in here.
    """

    def test_lua_remains_a_declared_unsupported_extension(self):
        config = json.loads((ROOT / "config" / "languages.json").read_text())
        assert ".lua" in config["unsupported_extensions"]["extensions"]
        assert ".lua" not in config["generic_tier"]["extensions"]

    def test_an_unsupported_extension_is_warned_about_not_dropped(self):
        """The existing guarantee that keeps this honest: a .lua file is
        surfaced as a coverage gap, never silently skipped."""
        import check_single_writer as gate

        assert ".lua" in gate.emitters.unsupported_extensions()

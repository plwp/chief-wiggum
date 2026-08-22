"""Generated Go guard templates must not break `go build` (chief-wiggum#380).

`guards.go` is intentionally pseudocode: signatures carry contract argument
names and bodies are not valid Go. Without a build constraint, `go build ./...`
on a Go target picks it up and fails with syntax errors, so every re-render
silently reintroduced a build break.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import formal_models as fm  # noqa: E402

CONSTRAINT = "//go:build ignore"


def _contracts():
    return {
        "entities": [
            {
                "name": "Order",
                "operations": [
                    {
                        "name": "create order",
                        "method": "POST",
                        "path": "/api/v1/orders",
                        "preconditions": [{"description": "total > 0", "expression": "total > 0"}],
                        "postconditions": [{"description": "order exists"}],
                    }
                ],
            }
        ]
    }


class TestGeneratedHeader:
    def test_the_constraint_is_the_very_first_line(self):
        """Go only honours a build constraint before anything else."""
        assert fm.generate_guards_go(_contracts()).splitlines()[0] == CONSTRAINT

    def test_a_blank_line_separates_the_constraint_from_what_follows(self):
        """Without the blank line Go treats it as an ordinary comment."""
        assert fm.generate_guards_go(_contracts()).splitlines()[1] == ""

    def test_the_constraint_precedes_the_package_clause(self):
        lines = fm.generate_guards_go(_contracts()).splitlines()
        assert lines.index(CONSTRAINT) < lines.index("package handlers")

    def test_the_header_says_it_is_a_template(self):
        """A reader who ignores the tag should still be told not to compile it."""
        assert "TEMPLATE" in fm.generate_guards_go(_contracts())

    def test_an_empty_contract_set_still_emits_the_constraint(self):
        """A render with no operations must still be build-safe."""
        assert fm.generate_guards_go({"entities": []}).startswith(CONSTRAINT)

    def test_the_generated_body_is_still_produced(self):
        """The constraint must not have displaced the actual output."""
        code = fm.generate_guards_go(_contracts())
        assert "func CreateOrder(" in code
        assert "// REQUIRES: total > 0" in code


class TestCommittedTemplates:
    def _committed(self):
        return [
            path for path in sorted(ROOT.rglob("guards.go"))
            if ".claude" not in path.relative_to(ROOT).parts
        ]

    def test_there_are_committed_templates_to_check(self):
        """Guard against this suite silently policing an empty set."""
        assert self._committed(), "no committed guards.go found; this test would be vacuous"

    @pytest.mark.parametrize("index", range(3))
    def test_every_committed_template_carries_the_constraint(self, index):
        committed = self._committed()
        if index >= len(committed):
            pytest.skip("fewer committed templates than parameters")
        path = committed[index]
        first = path.read_text().splitlines()[0]
        assert first == CONSTRAINT, (
            f"{path.relative_to(ROOT)} would be compiled by `go build ./...`; "
            "re-render it or restamp the header"
        )

    def test_a_fresh_render_round_trips_the_committed_header(self):
        """AC: a fresh render round-trips identically to the committed template."""
        generated_header = fm.generate_guards_go({"entities": []}).splitlines()[:9]
        for path in self._committed():
            assert path.read_text().splitlines()[:9] == generated_header, (
                f"{path.relative_to(ROOT)} header differs from a fresh render"
            )

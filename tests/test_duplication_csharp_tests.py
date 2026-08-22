"""C# test exclusion in the duplication engine (chief-wiggum#382).

The engine reports "production duplication (tests excluded)" but only encoded
Python/JS conventions, so on a large C# corpus *Tests.cs files and
*.UnitTests / *.ScenarioTests project directories counted as production. On an
~830k-line repo that reported 17.6% for the mixed corpus where the split was
roughly 15% production against 45% test.

The parity test is the point: quality.complexity.TEST_RE already classified
these correctly, and the two conventions had silently diverged. This keeps them
in step rather than fixing one instance of the drift.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from quality import complexity, duplication  # noqa: E402

# Paths a C# repo really produces, plus the near-misses that must NOT be
# classified as tests.
CSHARP_TESTS = [
    "src/Roller.API.Tests/OrderTests.cs",
    "src/Foo.UnitTests/BarTests.cs",
    "src/Foo.ScenarioTests/Scenario.cs",
    "test/Thing.Test.cs",
    "src/Widget/WidgetTest.cs",
]
CSHARP_PRODUCTION = [
    "src/Roller.API/OrderService.cs",
    "src/Foo/Bar.cs",
    # The case-sensitivity traps #259 called out: a lowercase "test" inside an
    # ordinary word is not a test.
    "src/Latest.cs",
    "src/latest/Thing.cs",
    "src/Contest/Entry.cs",
]
OTHER_LANGUAGE_TESTS = [
    "app/tests/test_thing.py",
    "app/thing_test.py",
    "pkg/thing_test.go",
    "web/__tests__/thing.spec.ts",
    "web/thing.test.tsx",
    "e2e/journey.spec.ts",
]


class TestCSharpTestExclusion:
    @pytest.mark.parametrize("path", CSHARP_TESTS)
    def test_csharp_test_files_are_excluded(self, path):
        assert duplication.matches_ignore(path), (
            f"{path} would be counted as production duplication"
        )

    @pytest.mark.parametrize("path", CSHARP_PRODUCTION)
    def test_csharp_production_files_are_not_excluded(self, path):
        assert not duplication.matches_ignore(path), (
            f"{path} is production code and must still be measured"
        )

    @pytest.mark.parametrize("path", OTHER_LANGUAGE_TESTS)
    def test_existing_conventions_still_hold(self, path):
        """The C# additions must not have displaced what already worked."""
        assert duplication.matches_ignore(path)


class TestConventionParity:
    """The anti-divergence guard the ticket actually asks for."""

    @pytest.mark.parametrize("path", CSHARP_TESTS + OTHER_LANGUAGE_TESTS)
    def test_duplication_test_globs_match_complexity(self, path):
        """Anything complexity calls a test, duplication must exclude.

        The two lists drifted apart once; this is what catches the next drift
        rather than waiting for another misstated headline.
        """
        assert complexity.TEST_RE.search(path), f"fixture {path} is not a test by TEST_RE"
        assert duplication.matches_ignore(path), (
            f"complexity classifies {path} as a test but duplication counts it "
            "as production"
        )

    @pytest.mark.parametrize("path", CSHARP_PRODUCTION)
    def test_both_engines_agree_production_is_production(self, path):
        assert not complexity.TEST_RE.search(path)
        assert not duplication.matches_ignore(path)

    def test_the_fixtures_are_not_vacuous(self):
        """A parity suite over an empty set would pass while proving nothing."""
        assert len(CSHARP_TESTS) >= 3
        assert len(CSHARP_PRODUCTION) >= 3

    def test_csharp_is_actually_scanned(self):
        """The exclusion only matters because #259 made .cs visible at all."""
        assert "csharp" in duplication.FORMATS


class TestOomNoteNamesTheKnob:
    """#382 secondary: an OOM left a raw GC log with no mention of the knob."""

    def _proc(self, stderr, returncode=-6):
        return subprocess.CompletedProcess(["jscpd"], returncode, "", stderr)

    def test_an_oom_crash_names_the_heap_variable(self):
        note = duplication._crash_note(
            self._proc("<--- Last few GCs --->\nMark-Compact 4000.1 (4100.0) MB"), 4096
        )
        assert "CW_JSCPD_MAX_OLD_SPACE_MB" in note
        assert "4096" in note, "the note should say what the ceiling currently is"

    def test_the_raw_output_is_still_carried(self):
        note = duplication._crash_note(self._proc("Mark-Compact allocation failed"), 4096)
        assert "Mark-Compact" in note, "the operator still needs the underlying trace"

    def test_a_heap_message_without_the_abort_code_is_still_recognised(self):
        note = duplication._crash_note(
            self._proc("FATAL ERROR: JavaScript heap out of memory", returncode=1), 8192
        )
        assert "CW_JSCPD_MAX_OLD_SPACE_MB" in note
        assert "8192" in note

    def test_an_ordinary_crash_is_not_dressed_up_as_an_oom(self):
        note = duplication._crash_note(
            self._proc("SyntaxError: unexpected token", returncode=1), 4096
        )
        assert "CW_JSCPD_MAX_OLD_SPACE_MB" not in note
        assert "SyntaxError" in note

    def test_notes_stay_bounded(self):
        assert len(duplication._crash_note(self._proc("x" * 5000, returncode=1), 4096)) <= 400
        oom = duplication._crash_note(self._proc("Mark-Compact " + "y" * 5000), 4096)
        assert len(oom) <= 400

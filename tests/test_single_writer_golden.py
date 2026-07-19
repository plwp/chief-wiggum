"""Golden-parity test for check_single_writer.py (#160).

The emission/claim split is a REFACTOR: `scan_writers`'s output for the fixture
below was captured to `tests/fixtures/single_writer_golden/expected.*.txt`
BEFORE the split (see PR #160), by running the CLI directly against the
fixture. This test re-runs the CLI against the same fixture and asserts
byte-identical output — any drift in the internal emission/claim boundary
must never change what a user sees.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "single_writer_golden"
SCRIPT = Path(__file__).parent.parent / "scripts" / "check_single_writer.py"


def _run(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE / "epic"), "--source", str(FIXTURE / "src"), *extra_args],
        capture_output=True,
        text=True,
    )


def test_golden_text_output_byte_identical():
    result = _run("--format", "text")
    expected = (FIXTURE / "expected.text.txt").read_text()
    assert result.stdout == expected
    assert result.returncode == 0


def test_golden_json_output_byte_identical():
    result = _run("--format", "json")
    expected = (FIXTURE / "expected.json.txt").read_text()
    assert result.stdout == expected
    assert result.returncode == 0


def test_golden_coverage_gate_byte_identical_and_still_fails():
    result = _run("--gate", "coverage", "--format", "text")
    expected_lines = (FIXTURE / "expected.coverage_gate.txt").read_text().splitlines()
    expected_stdout = "\n".join(expected_lines[:-1]) + "\n"
    expected_exit = int(expected_lines[-1].split("=")[1])
    assert result.stdout == expected_stdout
    assert result.returncode == expected_exit

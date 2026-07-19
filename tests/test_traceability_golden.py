"""Golden-parity test for check_traceability.py (#160).

Same discipline as `test_single_writer_golden.py`: the emission/claim seam is
formalized as a refactor, not a behavior change. Output for this fixture was
captured to `tests/fixtures/traceability_golden/expected.*.txt` BEFORE the
refactor (see PR #160); this test re-runs the CLI and asserts byte-identical
output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "traceability_golden"
SCRIPT = Path(__file__).parent.parent / "scripts" / "check_traceability.py"


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


def test_golden_json_output_byte_identical():
    result = _run("--format", "json")
    expected = (FIXTURE / "expected.json.txt").read_text()
    assert result.stdout == expected

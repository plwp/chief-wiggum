"""The test suite must never write telemetry into the operator's real log.

`factory_log.emit*` is enabled by an ambient `CW_TELEMETRY=1` — the env var an
operator sets to measure a factory run. Running pytest in that shell used to
send every fixture's gate/consult event to `~/.chief-wiggum/factory-log.jsonl`:
in a real 32k-record log, ~95% of gate records were pytest fixtures (`repo`
values like `epic`, `clean`, `dirty`, `test_cli_gate_exits_1_on_terra0`), and
every value/noise verdict was computed over them.

`tests/conftest.py` redirects the log per-test. These tests hold that line.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factory_log  # noqa: E402


def test_log_path_is_redirected_away_from_the_real_log():
    assert factory_log.log_path() != factory_log.DEFAULT_LOG
    assert str(factory_log.DEFAULT_LOG) not in str(factory_log.log_path())


def test_emitting_does_not_touch_the_real_log(tmp_path):
    """The regression itself: emit a gate event the way a gate test does, and
    confirm it lands in the redirected file."""
    before = factory_log.DEFAULT_LOG.stat().st_size if factory_log.DEFAULT_LOG.is_file() else 0

    factory_log.emit("gate", repo="epic", name="check_traceability", result="fail", caught=12)

    after = factory_log.DEFAULT_LOG.stat().st_size if factory_log.DEFAULT_LOG.is_file() else 0
    assert after == before, "test telemetry leaked into the operator's real factory log"
    assert factory_log.log_path().is_file()
    assert "check_traceability" in factory_log.log_path().read_text()


def test_fixture_applies_without_being_requested():
    """It is autouse — a gate test that never mentions telemetry is still
    isolated. That is the whole point: the 12 leaking test modules did not know
    they were emitting."""
    assert os.environ["CW_FACTORY_LOG"] != str(factory_log.DEFAULT_LOG)

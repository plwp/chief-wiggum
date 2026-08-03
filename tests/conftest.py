"""Shared pytest fixtures for the chief-wiggum test suite.

The one thing every test in this suite needs: **telemetry isolation**.

`factory_log.emit*` is a no-op unless telemetry is enabled, and it is enabled by
an AMBIENT env var (`CW_TELEMETRY=1`) that an operator measuring a factory run
sets in their shell. That is the correct production behaviour — but it means a
test run in that same shell writes its fixture gate/consult events into the
operator's REAL `~/.chief-wiggum/factory-log.jsonl`.

That is not hypothetical. Before this fixture existed, ~95% of the gate records
in a real 32k-record log were pytest fixtures: `repo` values like `epic`,
`clean`, `dirty`, and `test_cli_gate_exits_1_on_terra0` (pytest `tmp_path`
directory names) drowned the few hundred real runs, and every value/noise
verdict `aggregate()` produced was computed over them.

Pointing `CW_FACTORY_LOG` at a per-test `tmp_path` keeps emission ON — so tests
that assert telemetry is written still work — while sending it somewhere that
disappears with the test. Tests that specifically exercise the disabled path
(`test_factory_log.py`) `delenv` both vars themselves; monkeypatch composes, so
this fixture does not fight them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_factory_log(tmp_path, monkeypatch):
    """Redirect factory telemetry to a per-test path (see module docstring)."""
    monkeypatch.setenv("CW_FACTORY_LOG", str(tmp_path / "factory-log.jsonl"))

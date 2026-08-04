"""Tests for scripts/install_deps.py --tool telemetry-capture (chief-wiggum#345 AC1).

Provisions `~/.chief-wiggum/otel/` and runs a catch-up transcript ingest so
Claude-layer cost capture works with zero operator configuration -- the
transcript route needs no dotfile edits. Must NEVER write
`~/.claude/settings.json` or a shell rc file: those stay the operator's own
files (the OTEL env + wrapper snippet is printed, never applied). See the
implementation plan's Open Question 1 and `docs/harness-adapters.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factory_log  # noqa: E402
import install_deps  # noqa: E402


def _patch_home_and_ingest(monkeypatch, tmp_path, calls=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # install_deps.py is expected to `import factory_log` for the catch-up
    # ingest -- bind it explicitly so this test doesn't depend on the module
    # having been wired yet (that wiring is exactly what's under test).
    monkeypatch.setattr(install_deps, "factory_log", factory_log, raising=False)

    def fake_ingest(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(factory_log, "ingest_claude_transcripts", fake_ingest)


def test_telemetry_capture_provisions_otel_dir(monkeypatch, tmp_path):
    _patch_home_and_ingest(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["install_deps.py", "--tool", "telemetry-capture"])

    install_deps.main()

    assert (tmp_path / ".chief-wiggum" / "otel").is_dir()


def test_telemetry_capture_runs_exactly_one_catch_up_ingest(monkeypatch, tmp_path):
    calls: list = []
    _patch_home_and_ingest(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr("sys.argv", ["install_deps.py", "--tool", "telemetry-capture"])

    install_deps.main()

    assert len(calls) == 1


def test_telemetry_capture_never_writes_claude_settings_or_shell_rc(monkeypatch, tmp_path):
    """The mitigation this ticket deliberately rejects (plan Open Question 1):
    /setup and install_deps print the OTEL snippet, they never write it."""
    _patch_home_and_ingest(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["install_deps.py", "--tool", "telemetry-capture"])

    install_deps.main()

    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".zshrc").exists()
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".bash_profile").exists()


def test_telemetry_capture_prints_the_otel_snippet(monkeypatch, tmp_path, capsys):
    """Print -- never write -- the OTEL opt-in snippet for operators who do
    want the console-exporter route."""
    _patch_home_and_ingest(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["install_deps.py", "--tool", "telemetry-capture"])

    install_deps.main()

    out = capsys.readouterr().out
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" in out
    assert not (tmp_path / ".claude" / "settings.json").exists()

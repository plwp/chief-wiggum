"""Tests for the cw CLI facade (P3-17) — argument dispatch only.

Business logic is tested in each helper's own test module; here we only verify
the facade lists helpers and forwards args to the right module's main().
"""

from __future__ import annotations

import importlib

import cw
import pytest


def test_help_lists_all_commands(capsys):
    assert cw.main([]) == 0
    out = capsys.readouterr().out
    for name in cw.SUBCOMMANDS:
        assert name in out


def test_help_flag(capsys):
    assert cw.main(["--help"]) == 0
    assert "Commands:" in capsys.readouterr().out


def test_unknown_command_exits_2(capsys):
    assert cw.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


@pytest.mark.parametrize("command", list(cw.SUBCOMMANDS))
def test_every_command_maps_to_an_importable_main(command):
    module_name, _desc = cw.SUBCOMMANDS[command]
    module = importlib.import_module(module_name)
    assert callable(module.main)


def test_dispatch_forwards_args(monkeypatch):
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    # plan-waves -> plan_waves.main
    module = importlib.import_module("plan_waves")
    monkeypatch.setattr(module, "main", fake_main)
    rc = cw.main(["plan-waves", "--edges", '{"1": []}'])
    assert rc == 0
    assert captured["argv"] == ["--edges", '{"1": []}']


def test_dispatch_returns_helper_exit_code(monkeypatch):
    module = importlib.import_module("git_safety")
    monkeypatch.setattr(module, "main", lambda argv: 7)
    assert cw.main(["git-safety", "check-branch", "x"]) == 7

"""Parallel provider preflight and phase timing (chief-wiggum#375).

The load-bearing property is that "we could not tell" never renders as "it is
fine". A preflight that reports a broken environment as healthy is worse than
no preflight, because the phase proceeds and discovers it serially anyway.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chief_wiggum.preflight import (  # noqa: E402
    Health,
    Probes,
    ProbeUnavailable,
    RoleStatus,
    check_all,
    check_provider,
    preflight,
    requirements_for,
    role_report,
)
from factory_log import phase_summary  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "provider_preflight.py"


def probes(*, commands=(), pythons=(), envs=(), secrets=(), secret_raises=False):
    def secret(name):
        if secret_raises:
            raise ProbeUnavailable("keyring is locked")
        return name in secrets

    return Probes(
        command=lambda name: name in commands,
        python=lambda name: name in pythons,
        env=lambda name: name in envs,
        secret=secret,
    )


CONFIG = {
    "providers": {
        "codex": {"type": "tool", "tool": "codex", "enabled": True},
        "deepseek-flash": {"type": "tool", "tool": "openrouter", "enabled": True},
        "claude-interactive": {"type": "delegate", "delegate": "claude-interactive",
                               "enabled": True},
        "gemini": {"type": "tool", "tool": "gemini", "enabled": False},
    },
    "roles": {
        "reviewer": {"required": ["codex", "deepseek-flash"],
                     "optional": ["claude-interactive"]},
    },
}


# ------------------------------------------------------------ per provider


class TestProviderHealth:
    def test_a_satisfied_provider_is_ok(self):
        report = check_provider("codex", CONFIG["providers"]["codex"],
                                probes(commands={"codex"}))
        assert report.health is Health.OK
        assert report.usable
        assert report.missing == ()

    def test_a_missing_command_names_what_is_missing(self):
        report = check_provider("codex", CONFIG["providers"]["codex"], probes())
        assert report.health is Health.UNAVAILABLE
        assert report.missing == ("command:codex",)
        assert "codex" in report.detail

    def test_a_missing_secret_is_reported_without_reading_any_value(self):
        report = check_provider("deepseek-flash", CONFIG["providers"]["deepseek-flash"],
                                probes())
        assert report.health is Health.UNAVAILABLE
        assert report.missing == ("secret:OPENROUTER_API_KEY",)

    def test_a_delegate_needs_every_one_of_its_requirements(self):
        entry = CONFIG["providers"]["claude-interactive"]
        partial = check_provider("claude-interactive", entry, probes(commands={"claude"}))
        assert partial.health is Health.UNAVAILABLE
        assert partial.missing == ("command:tmux",)
        full = check_provider("claude-interactive", entry,
                              probes(commands={"claude", "tmux"}))
        assert full.health is Health.OK

    def test_a_disabled_provider_is_disabled_not_broken(self):
        report = check_provider("gemini", CONFIG["providers"]["gemini"], probes())
        assert report.health is Health.DISABLED
        assert not report.usable, "disabled is not usable, but it is not a fault either"

    def test_an_unrunnable_probe_is_unknown_not_ok(self):
        """AC: 'could not tell' must never render as 'it is fine'."""
        report = check_provider("deepseek-flash", CONFIG["providers"]["deepseek-flash"],
                                probes(secret_raises=True))
        assert report.health is Health.UNKNOWN
        assert not report.usable
        assert "locked" in report.detail

    def test_an_unrecognised_provider_shape_is_unknown_not_ok(self):
        report = check_provider("mystery", {"type": "tool", "tool": "wat", "enabled": True},
                                probes())
        assert report.health is Health.UNKNOWN
        assert "cannot verify" in report.detail

    def test_a_provider_may_declare_its_own_requirements(self):
        entry = {"enabled": True, "preflight": [{"kind": "env", "name": "MY_THING"}]}
        assert requirements_for("custom", entry)[0].name == "MY_THING"
        assert check_provider("custom", entry, probes(envs={"MY_THING"})).health is Health.OK
        assert check_provider("custom", entry, probes()).health is Health.UNAVAILABLE

    def test_check_provider_never_raises(self):
        class Exploding:
            def check(self, requirement):
                raise ProbeUnavailable("boom")

        report = check_provider("codex", CONFIG["providers"]["codex"], Exploding())
        assert report.health is Health.UNKNOWN


# ---------------------------------------------------------------- parallel


class TestParallelism:
    def test_every_provider_is_checked(self):
        reports = check_all(CONFIG["providers"], probes(commands={"codex"}))
        assert set(reports) == set(CONFIG["providers"])

    def test_checks_run_concurrently_rather_than_serially(self):
        """Serial discovery is the actual complaint: each failure costs a round trip."""
        started = threading.Barrier(3, timeout=10)

        def slow_command(name):
            started.wait()          # only passes if three checks are in flight at once
            time.sleep(0.01)
            return True

        config = {f"p{i}": {"type": "tool", "tool": "codex", "enabled": True} for i in range(3)}
        reports = check_all(config, Probes(command=slow_command))
        assert all(report.health is Health.OK for report in reports.values())

    def test_no_providers_is_an_empty_result_not_a_crash(self):
        assert check_all({}, probes()) == {}


# ------------------------------------------------------------------ roles


class TestRoleReadiness:
    def _reports(self, **health):
        return check_all(CONFIG["providers"], probes(**health))

    def test_a_role_with_everything_up_is_ok(self):
        reports = self._reports(commands={"codex", "claude", "tmux"},
                                secrets={"OPENROUTER_API_KEY"})
        assert role_report("reviewer", CONFIG["roles"]["reviewer"], reports).status is RoleStatus.OK

    def test_a_down_optional_provider_only_degrades(self):
        reports = self._reports(commands={"codex"}, secrets={"OPENROUTER_API_KEY"})
        report = role_report("reviewer", CONFIG["roles"]["reviewer"], reports)
        assert report.status is RoleStatus.DEGRADED
        assert report.optional_down == ("claude-interactive",)
        assert "quorum stands" in report.detail

    def test_a_down_required_provider_blocks_and_names_the_fallback(self):
        """AC: name the fallback rather than let the orchestrator improvise one."""
        reports = self._reports(commands={"claude", "tmux"}, secrets={"OPENROUTER_API_KEY"})
        report = role_report("reviewer", CONFIG["roles"]["reviewer"], reports)
        assert report.status is RoleStatus.BLOCKED
        assert report.required_down == ("codex",)
        assert report.fallback == ("claude-interactive",)
        assert "claude-interactive" in report.detail

    def test_a_blocked_role_with_no_healthy_fallback_says_so(self):
        report = role_report("reviewer", CONFIG["roles"]["reviewer"], self._reports())
        assert report.status is RoleStatus.BLOCKED
        assert report.fallback == ()
        assert "no healthy fallback" in report.detail

    def test_an_unverifiable_required_provider_blocks_the_role(self):
        """Unknown must not count as healthy when deciding whether a role can run."""
        reports = check_all(CONFIG["providers"],
                            probes(commands={"codex"}, secret_raises=True))
        report = role_report("reviewer", CONFIG["roles"]["reviewer"], reports)
        assert report.status is RoleStatus.BLOCKED
        assert "deepseek-flash" in report.required_down

    def test_a_provider_absent_from_config_blocks_rather_than_passes(self):
        spec = {"required": ["ghost"], "optional": []}
        assert role_report("r", spec, {}).status is RoleStatus.BLOCKED


# -------------------------------------------------------------- full sweep


class TestPreflight:
    def test_healthy_environment_reports_ok(self):
        result = preflight(CONFIG, probes(commands={"codex", "claude", "tmux"},
                                          secrets={"OPENROUTER_API_KEY"}))
        assert result["ok"] is True
        assert result["blocked_roles"] == []
        assert result["unverifiable_providers"] == []

    def test_a_blocked_role_is_listed(self):
        result = preflight(CONFIG, probes(secrets={"OPENROUTER_API_KEY"}))
        assert result["ok"] is False
        assert result["blocked_roles"] == ["reviewer"]

    def test_an_unverifiable_provider_makes_the_sweep_not_ok(self):
        """Isolated on purpose: the unknown provider is in NO role, so nothing
        is blocked and only the unknown itself can spoil `ok`."""
        config = {
            "providers": {
                "codex": {"type": "tool", "tool": "codex", "enabled": True},
                "mystery": {"type": "tool", "tool": "wat", "enabled": True},
            },
            "roles": {"reviewer": {"required": ["codex"], "optional": []}},
        }
        result = preflight(config, probes(commands={"codex"}))
        assert result["blocked_roles"] == [], "no role is blocked in this case"
        assert result["unverifiable_providers"] == ["mystery"]
        assert result["ok"] is False, "an unverifiable provider alone must spoil the sweep"

    def test_an_unverifiable_required_provider_also_blocks(self):
        result = preflight(CONFIG, probes(commands={"codex", "claude", "tmux"},
                                          secret_raises=True))
        assert result["ok"] is False
        assert "deepseek-flash" in result["unverifiable_providers"]
        assert result["blocked_roles"] == ["reviewer"]

    def test_roles_can_be_filtered(self):
        config = dict(CONFIG)
        config["roles"] = {"reviewer": CONFIG["roles"]["reviewer"],
                           "other": {"required": ["codex"], "optional": []}}
        result = preflight(config, probes(commands={"codex"}), roles=["other"])
        assert list(result["roles"]) == ["other"]

    def test_the_shipped_config_is_preflightable(self):
        """Every shipped provider must have known requirements, or the sweep
        reports it as unverifiable rather than quietly passing it."""
        config = json.loads((ROOT / "config" / "providers.json").read_text())
        result = preflight(config, probes())
        unknown = result["unverifiable_providers"]
        assert unknown == [], f"shipped providers with no preflight rule: {unknown}"


# -------------------------------------------------------------------- CLI


class TestCLI:
    def _run(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args],
                              capture_output=True, text=True)

    def test_exit_code_distinguishes_blocked_from_ok(self, tmp_path):
        config = tmp_path / "providers.json"
        config.write_text(json.dumps({
            "providers": {"codex": {"type": "tool", "tool": "codex", "enabled": True}},
            "roles": {"reviewer": {"required": ["codex"], "optional": []}},
        }))
        result = self._run("--config", str(config))
        payload = json.loads(result.stdout)
        # codex is unlikely to be installed in CI; either way the code matches.
        expected = 0 if payload["ok"] else 1
        assert result.returncode == expected

    def test_unreadable_config_is_its_own_exit_code(self, tmp_path):
        result = self._run("--config", str(tmp_path / "nope.json"))
        assert result.returncode == 3
        assert json.loads(result.stdout)["ok"] is False

    def test_human_output_flags_unverifiable_providers_loudly(self, tmp_path):
        config = tmp_path / "providers.json"
        config.write_text(json.dumps({
            "providers": {"mystery": {"type": "tool", "tool": "wat", "enabled": True}},
            "roles": {},
        }))
        result = self._run("--config", str(config), "--human")
        assert result.returncode == 2, "unverifiable is its own outcome, not success"
        assert "Unverified is not healthy" in result.stdout


# --------------------------------------------------------- phase latency


class TestPhaseSummary:
    def test_aggregates_per_phase_wall_clock(self):
        records = [
            {"event": "phase", "phase": "consults", "duration_ms": 1000.0, "outcome": "ok"},
            {"event": "phase", "phase": "consults", "duration_ms": 500.0, "outcome": "ok"},
            {"event": "phase", "phase": "implement", "duration_ms": 2000.0, "outcome": "ok"},
            {"event": "gate", "gate": "ratchet", "duration_ms": 9999.0},
        ]
        summary = phase_summary(records)
        assert summary["slowest"] == "implement"
        assert summary["total_ms"] == 3500.0
        consults = next(p for p in summary["phases"] if p["phase"] == "consults")
        assert consults["runs"] == 2
        assert consults["mean_ms"] == 750.0

    def test_a_phase_that_errored_is_counted_not_hidden(self):
        """A phase that blew up fast must not read as a phase that went well."""
        summary = phase_summary([
            {"event": "phase", "phase": "consults", "duration_ms": 10.0, "outcome": "error"},
        ])
        assert summary["phases"][0]["errors"] == 1

    def test_no_phase_records_is_an_empty_summary(self):
        summary = phase_summary([{"event": "gate", "gate": "x"}])
        assert summary["phases"] == []
        assert summary["slowest"] is None

    def test_phase_timer_emits_even_when_the_phase_raises(self, monkeypatch):
        import factory_log

        emitted = []
        monkeypatch.setattr(factory_log, "emit", lambda event, **fields: emitted.append(
            {"event": event, **fields}) or True)
        with pytest.raises(RuntimeError):
            with factory_log.phase_timer("boom", ticket="#1"):
                raise RuntimeError("phase failed")
        assert emitted and emitted[0]["outcome"] == "error"
        assert emitted[0]["phase"] == "boom"

    def test_phase_timer_records_a_successful_phase(self, monkeypatch):
        import factory_log

        emitted = []
        monkeypatch.setattr(factory_log, "emit", lambda event, **fields: emitted.append(
            {"event": event, **fields}) or True)
        with factory_log.phase_timer("consults", ticket="#1") as phase:
            phase.detail = "3 providers"
        assert emitted[0]["outcome"] == "ok"
        assert emitted[0]["detail"] == "3 providers"
        assert emitted[0]["duration_ms"] >= 0

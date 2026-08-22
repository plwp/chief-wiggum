"""Validated interpreter resolution (chief-wiggum#374).

The defect: `check_deps.py` verified keyring under the interpreter IT ran as,
which says nothing about the interpreter the skills invoke. So the property
under test throughout is that validation happens INSIDE the candidate, and that
a candidate missing something is never selected however convenient it is.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chief_wiggum.interpreter import (  # noqa: E402
    PACKAGE_FOR_IMPORT,
    PROFILES,
    Candidate,
    NoValidInterpreter,
    modules_for,
    probe,
    remediation,
    resolve,
)

ROOT = Path(__file__).resolve().parents[1]
ENV_PY = ROOT / "scripts" / "env.py"


# ------------------------------------------------------------------ probing


class TestProbe:
    def test_probes_the_candidate_not_the_current_process(self):
        """The whole defect: an answer about this process is the wrong answer."""
        result = probe(sys.executable, ["json"])
        assert result.valid
        assert result.version.startswith("3.")

    def test_a_missing_module_is_reported_not_raised(self):
        result = probe(sys.executable, ["definitely_not_a_real_module_xyz"])
        assert not result.valid
        assert result.missing == ("definitely_not_a_real_module_xyz",)
        assert result.error == ""

    def test_an_unrunnable_interpreter_is_an_error_not_a_pass(self):
        result = probe("/nonexistent/python", ["json"])
        assert not result.valid
        assert result.error
        assert "could not run" in result.error

    def test_a_candidate_that_exits_nonzero_is_not_valid(self, tmp_path):
        fake = tmp_path / "brokenpython"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
        result = probe(str(fake), ["json"])
        assert not result.valid
        assert "exited 3" in result.error

    def test_unparseable_probe_output_is_not_a_pass(self, tmp_path):
        fake = tmp_path / "chattypython"
        fake.write_text("#!/bin/sh\necho not json\n")
        fake.chmod(0o755)
        result = probe(str(fake), ["json"])
        assert not result.valid
        assert "unparseable" in result.error


# ----------------------------------------------------------------- profiles


class TestProfiles:
    def test_core_requires_keyring(self):
        assert "keyring" in modules_for(["core"])

    def test_profiles_union_without_duplicates(self):
        merged = modules_for(["core", "consult"])
        assert merged.count("keyring") == 1

    def test_an_unknown_profile_is_refused_by_name(self):
        with pytest.raises(ValueError, match="unknown profile"):
            modules_for(["not-a-profile"])

    def test_every_profile_module_has_a_package_mapping(self):
        """A remediation naming an import that pip cannot install is useless."""
        for profile, modules in PROFILES.items():
            for module in modules:
                assert module in PACKAGE_FOR_IMPORT, (
                    f"{profile} needs {module}, which has no install-name mapping"
                )


class TestRemediation:
    def test_names_the_interpreter_and_uses_uv(self):
        command = remediation("/opt/py/bin/python3.11", ["keyring"])
        assert "/opt/py/bin/python3.11" in command
        assert command.startswith("uv pip install --python ")
        assert "pip3 install" not in command

    def test_translates_import_names_to_package_names(self):
        assert "google-genai" in remediation("/x/python", ["google.genai"])

    def test_is_deterministic_and_deduplicated(self):
        assert remediation("/x", ["keyring", "keyring"]) == remediation("/x", ["keyring"])


# ---------------------------------------------------------------- resolving


class TestResolve:
    def test_finds_an_interpreter_that_satisfies_the_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        resolution = resolve(["core"], use_cache=False, cache_path=tmp_path / "cache.json")
        assert Path(resolution.python).exists()
        assert resolution.source == "CW_PYTHON"

    def test_the_override_is_tried_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        resolution = resolve(["core"], use_cache=False, cache_path=tmp_path / "cache.json")
        assert resolution.considered[0].source == "CW_PYTHON"

    def test_an_impossible_requirement_fails_loudly_with_a_remediation(
        self, tmp_path, monkeypatch
    ):
        """AC: name the fix, do not emit a bare traceback."""
        monkeypatch.setitem(PROFILES, "impossible", ("definitely_not_a_real_module_xyz",))
        with pytest.raises(NoValidInterpreter) as excinfo:
            resolve(["impossible"], use_cache=False, cache_path=tmp_path / "cache.json")
        message = str(excinfo.value)
        assert "uv pip install --python" in message
        assert "CW_PYTHON" in message
        assert "Tried:" in message

    def test_the_failure_lists_every_candidate_it_tried(self, tmp_path, monkeypatch):
        monkeypatch.setitem(PROFILES, "impossible", ("definitely_not_a_real_module_xyz",))
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        with pytest.raises(NoValidInterpreter) as excinfo:
            resolve(["impossible"], use_cache=False, cache_path=tmp_path / "cache.json")
        assert "CW_PYTHON" in str(excinfo.value)


class TestCache:
    def test_a_resolution_is_cached(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        cache = tmp_path / "cache.json"
        resolve(["core"], use_cache=False, cache_path=cache)
        assert json.loads(cache.read_text())["python"]

    def test_a_cache_naming_a_vanished_interpreter_is_ignored(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({
            "python": "/gone/python", "source": "cache", "version": "3.11.0",
            "profiles": ["core"],
        }))
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        resolution = resolve(["core"], cache_path=cache)
        assert resolution.python != "/gone/python"

    def test_the_cache_is_revalidated_not_trusted(self, tmp_path, monkeypatch):
        """The defect being fixed is an interpreter changing underneath CW, so
        a cached answer that no longer satisfies the profile must be dropped."""
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({
            "python": sys.executable, "source": "cache", "version": "3.11.0",
            "profiles": ["impossible"],
        }))
        monkeypatch.setitem(PROFILES, "impossible", ("definitely_not_a_real_module_xyz",))
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        with pytest.raises(NoValidInterpreter):
            resolve(["impossible"], cache_path=cache)

    def test_a_cache_for_fewer_profiles_is_not_reused(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({
            "python": sys.executable, "source": "cache", "version": "3.11.0",
            "profiles": ["core"],
        }))
        monkeypatch.setitem(PROFILES, "extra", ("definitely_not_a_real_module_xyz",))
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        with pytest.raises(NoValidInterpreter):
            resolve(["core", "extra"], cache_path=cache)

    def test_a_corrupt_cache_is_ignored_rather_than_fatal(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache.json"
        cache.write_text("{not json")
        monkeypatch.setenv("CW_PYTHON", sys.executable)
        assert resolve(["core"], cache_path=cache).python


class TestCandidateModel:
    def test_a_candidate_with_an_error_is_never_valid(self):
        assert not Candidate("/x", "s", error="boom").valid

    def test_a_candidate_with_missing_modules_is_never_valid(self):
        assert not Candidate("/x", "s", missing=("keyring",)).valid


# --------------------------------------------------------------------- CLI


class TestEnvPythonCLI:
    def _run(self, *args, env=None):
        return subprocess.run([sys.executable, str(ENV_PY), "python", *args],
                              capture_output=True, text=True, env=env)

    def test_prints_a_usable_interpreter_path(self):
        result = self._run()
        assert result.returncode == 0, result.stderr
        assert Path(result.stdout.strip()).exists()

    def test_json_mode_shows_what_was_considered(self):
        result = self._run("--json", "--no-cache")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["python"]
        assert payload["considered"], "the resolution must show its working"

    def test_an_unknown_profile_exits_nonzero_with_a_named_error(self):
        result = self._run("--profile", "not-a-profile")
        assert result.returncode == 1
        assert "unknown profile" in result.stderr

    def test_the_resolved_interpreter_actually_imports_the_profile(self):
        """End to end: what it prints must be able to do what was asked."""
        result = self._run("--no-cache")
        assert result.returncode == 0, result.stderr
        interpreter = result.stdout.strip()
        check = subprocess.run([interpreter, "-c", "import keyring"],
                               capture_output=True, text=True)
        assert check.returncode == 0, (
            f"env.py python returned {interpreter}, which cannot import keyring"
        )


class TestKeychainRemediation:
    def test_the_missing_keyring_message_names_the_interpreter_and_uv(self):
        source = (ROOT / "scripts" / "keychain.py").read_text()
        assert "uv pip install --python" in source
        assert "pip3 install keyring" not in source, (
            "pip into an externally-managed system Python is what caused #374"
        )
        assert "CW_PYTHON" in source

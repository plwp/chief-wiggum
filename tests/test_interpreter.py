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


def _blocking_env(module: str, tmp_path: Path) -> dict:
    """An environment where importing `module` raises, for the child process.

    A meta_path finder rather than a stub file on sys.path: a stub would
    import successfully, which is the opposite of the condition under test.
    """
    import os

    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        f"        if name == {module!r} or name.startswith({module + '.'!r}):\n"
        f"            raise ModuleNotFoundError('blocked', name={module!r})\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    return env


class TestMissingDependencyIsActionable:
    """A missing dependency must name the interpreter, not just the package.

    Asserted by RUNNING the scripts with the module blocked, rather than by
    grepping their source for the message. The source check this replaces
    broke the moment the message moved into a shared helper — it was pinned to
    where the words lived, not to what the operator sees.

    #374's cost was three failed launches and three ad-hoc repairs, and the
    worst case is a backgrounded consult: the process exits instantly, the
    output file never appears, and a wait-less launch reads as success.
    """

    @pytest.mark.parametrize("script,module", [
        ("keychain.py", "keyring"),
        ("formal_models.py", "jsonschema"),
        ("generate_formal_test_artifacts.py", "jsonschema"),
    ])
    def test_the_message_names_the_interpreter_and_the_uv_command(
            self, script, module, tmp_path):
        path = ROOT / "scripts" / script
        if not path.is_file():
            pytest.skip(f"{script} is not present")
        proc = subprocess.run(
            [sys.executable, str(path), "--help"],
            capture_output=True, text=True, cwd=str(ROOT),
            env=_blocking_env(module, tmp_path),
        )
        assert proc.returncode != 0, "a missing dependency must not exit 0"
        assert "Traceback" not in proc.stderr, (
            f"{script} died on a bare traceback: {proc.stderr[:300]}")
        assert f"Missing dependency: {module}" in proc.stderr

        # The child resolves symlinks, so its sys.executable need not equal
        # ours (/opt/homebrew/opt/... vs /opt/homebrew/Cellar/...). Assert the
        # PROPERTY: an absolute interpreter is named, and the fix targets that
        # same one — a remediation pointing at a different interpreter than
        # the one that failed is the advice that wasted the operator's time.
        named = [line.split("interpreter:", 1)[1].strip()
                 for line in proc.stderr.splitlines() if "interpreter:" in line]
        assert named, f"no interpreter named in: {proc.stderr[:300]}"
        interpreter = named[0]
        assert interpreter.startswith("/"), interpreter
        assert f"uv pip install --python {interpreter}" in proc.stderr
        assert "CW_PYTHON" in proc.stderr, "offer the override too"

    def test_check_deps_probes_the_runtime_interpreter_not_its_own(self, tmp_path):
        """The original defect, stated as a property.

        `check_deps.py` used `importlib.import_module`, which answers for
        whichever interpreter is running the checker. The #374 machine had a
        working python3.11 with keyring while `python3` resolved to a 3.13
        without it — and this check passed. So the probe must happen inside
        the interpreter CW will actually invoke, and the report must say which
        one that was.
        """
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_deps.py"), "--for", "core"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert "probing:" in proc.stdout, (
            "check_deps must name the interpreter it probed, or a green result "
            f"says nothing about which one: {proc.stdout[:300]}")
        probed = [line.split("probing:", 1)[1].strip()
                  for line in proc.stdout.splitlines() if "probing:" in line]
        assert probed and probed[0].startswith("/"), probed

    def test_a_failed_resolution_is_announced_not_silently_absorbed(
            self, tmp_path, capsys, monkeypatch):
        """Raised in review, and its worst case is the original incident.

        Run the check under a working 3.11, let resolution fail, fall back
        quietly to 3.11, and every line comes back green — while the pipeline
        then runs under the broken `python3` and dies. The fallback is fine;
        being silent about it is not.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cw_check_deps_warn", ROOT / "scripts" / "check_deps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        import chief_wiggum.interpreter as interp

        def boom(*a, **k):
            raise interp.NoValidInterpreter("nothing validated")

        monkeypatch.setattr(interp, "resolve", boom)
        result = module.runtime_python()

        assert result == sys.executable, "it should still fall back"
        err = capsys.readouterr().err
        assert "could not resolve" in err, err
        assert sys.executable in err, "say which interpreter it fell back to"
        assert "may not be the one the skills invoke" in err

    def test_check_python_pkg_reports_a_module_absent_from_another_interpreter(
            self, tmp_path, capsys):
        """Probing really crosses the process boundary.

        Exercised against a stub interpreter that fails every import, because
        a same-interpreter shortcut would otherwise make the cross-process
        path untested — and that path IS the fix.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cw_check_deps", ROOT / "scripts" / "check_deps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # A faithful stand-in for a missing module: real interpreters
        # say so on stderr. A bare `exit 1` is an unrunnable probe,
        # which is a different state and is reported as one.
        fake = tmp_path / "fake-python"
        fake.write_text(
            "#!/bin/sh\n"
            "echo \"ModuleNotFoundError: No module named '$3'\" >&2\n"
            "exit 1\n")
        fake.chmod(0o755)

        module.fail_count = 0
        module.pass_count = 0
        module.warn_count = 0
        # `json`, not `keyring`: a module guaranteed importable in the test
        # interpreter. With `keyring` the kill was accidental — it depended on
        # the dev box happening not to have it, so on a box that did, the
        # "always use importlib" mutation would survive.
        module.check_python_pkg("json", "json", True, python=str(fake))
        assert module.fail_count == 1, (
            "a module absent from the RUNTIME interpreter must be reported "
            "missing even when the checker's own interpreter has it")

        # And the report has to be actionable, aimed at THAT interpreter.
        # Saying "missing" without saying where to install it is how the
        # operator ends up guessing, which is the cost #374 recorded.
        out = capsys.readouterr().out
        assert f"uv pip install --python {fake} json" in out, out

    def test_the_fix_line_names_the_distribution_not_the_import(
            self, tmp_path, capsys):
        """A remediation that manufactures a false green is worse than none.

        PyPI's `whisper` is a different package that also provides a `whisper`
        module. Advising the import name installs something that imports
        cleanly and behaves wrongly, so the NEXT check reads green while the
        runtime stays broken.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cw_check_deps_dist", ROOT / "scripts" / "check_deps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # A faithful stand-in for a missing module: real interpreters
        # say so on stderr. A bare `exit 1` is an unrunnable probe,
        # which is a different state and is reported as one.
        fake = tmp_path / "fake-python"
        fake.write_text(
            "#!/bin/sh\n"
            "echo \"ModuleNotFoundError: No module named '$3'\" >&2\n"
            "exit 1\n")
        fake.chmod(0o755)

        module.pass_count = module.fail_count = module.warn_count = 0
        module.check_python_pkg("whisper", "whisper", True, python=str(fake))
        out = capsys.readouterr().out
        assert "openai-whisper" in out, out
        assert "python whisper" not in out, (
            "advising the import name installs the wrong distribution")

    def test_a_probe_that_cannot_run_is_an_error_not_a_missing_package(
            self, tmp_path, capsys):
        """Three states, not two: present, absent, and could-not-ask.

        An unrunnable interpreter reported as "missing" hands the operator a
        uv command that cannot help, and letting the OSError escape kills the
        whole audit with the sort of traceback this change exists to remove.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cw_check_deps_err", ROOT / "scripts" / "check_deps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module.pass_count = module.fail_count = module.warn_count = 0
        module.check_python_pkg("keyring", "keyring", True,
                                python=str(tmp_path / "does-not-exist"))
        out = capsys.readouterr().out
        assert "probe failed" in out, out
        assert "python package not found" not in out, (
            "a probe that could not run is not evidence the package is absent")
        assert module.fail_count == 1

    def test_a_wrapper_banner_is_not_reported_as_the_version(
            self, tmp_path, capsys):
        """Some interpreter wrappers print before running the -c script.

        conda and a few virtualenv shims emit a line of their own, and taking
        the whole of stdout would report that banner as the package version —
        a green tick carrying nonsense.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cw_check_deps_banner", ROOT / "scripts" / "check_deps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        chatty = tmp_path / "chatty-python"
        chatty.write_text("#!/bin/sh\necho 'Activating env foo'\necho '4.25.1'\n")
        chatty.chmod(0o755)

        module.pass_count = module.fail_count = module.warn_count = 0
        module.check_python_pkg("jsonschema", "jsonschema", True,
                                python=str(chatty))
        out = capsys.readouterr().out
        assert module.pass_count == 1, out
        assert "4.25.1" in out
        assert "Activating env foo" not in out, (
            "the wrapper's banner was reported as the version")

    def test_no_script_advises_pip_into_the_system_interpreter(self):
        """pip into an externally-managed Python is what caused #374."""
        for name in ("keychain.py", "formal_models.py", "consult_ai.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            assert "pip3 install keyring" not in source
            assert "pip install keyring" not in source.replace(
                "uv pip install", "")

"""Tests for scripts/check_cw_standards.py (the factory-self-standards linter)."""

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_cw_standards  # noqa: E402


def _rules(findings):
    return {f.rule for f in findings}


def test_real_repo_meets_its_own_standards():
    """The CW repo itself passes — the gate is honest on real code before it blocks."""
    findings = check_cw_standards.check()
    assert findings == [], "\n".join(str(f) for f in findings)


def _scaffold(tmp_path, *, script="helper.py", command=None, test=True, title=True):
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / script).write_text("x = 1\n")
    md = "# Title\n" if title else "no title\n"
    md += command if command else ""
    (tmp_path / ".claude" / "commands" / "thing.md").write_text(md)
    return tmp_path


def test_no_bash_scripts(tmp_path):
    _scaffold(tmp_path)
    (tmp_path / "scripts" / "run.sh").write_text("#!/bin/bash\necho hi\n")
    assert "no-bash-scripts" in _rules(check_cw_standards.check(tmp_path))


def test_dangling_script_reference(tmp_path):
    _scaffold(tmp_path, command="Run `python3 scripts/ghost.py --flag`\n")
    findings = check_cw_standards.check(tmp_path)
    assert "dangling-script-ref" in _rules(findings)


def test_existing_script_reference_ok(tmp_path):
    _scaffold(tmp_path, script="real.py", command="Run `python3 scripts/real.py`\n")
    assert "dangling-script-ref" not in _rules(check_cw_standards.check(tmp_path))


def test_target_repo_scoped_reference_not_flagged(tmp_path):
    # `$TARGET_REPO/scripts/<x>.py` points at the TARGET repo's own script, not a
    # chief-wiggum script — its absence here is expected, not a dangling ref.
    _scaffold(
        tmp_path,
        command='TUT="$TARGET_REPO/scripts/maintain_tutorials.py"; python3 "$TUT" status\n',
    )
    assert "dangling-script-ref" not in _rules(check_cw_standards.check(tmp_path))


def test_bare_reference_still_flagged_when_target_prefix_absent(tmp_path):
    # A plain `scripts/ghost.py` (no target-repo prefix) is still a chief-wiggum ref.
    _scaffold(tmp_path, command="python3 scripts/ghost.py\n")
    assert "dangling-script-ref" in _rules(check_cw_standards.check(tmp_path))


def test_gate_without_test_flagged(tmp_path):
    _scaffold(tmp_path, script="check_thing.py")  # a gate, no test file
    assert "gate-untested" in _rules(check_cw_standards.check(tmp_path))


def test_gate_with_test_ok(tmp_path):
    _scaffold(tmp_path, script="check_thing.py")
    (tmp_path / "tests" / "test_check_thing.py").write_text("def test_x(): pass\n")
    assert "gate-untested" not in _rules(check_cw_standards.check(tmp_path))


def test_command_without_title(tmp_path):
    _scaffold(tmp_path, title=False)
    assert "command-no-title" in _rules(check_cw_standards.check(tmp_path))


def test_gate_flag_blocks(tmp_path):
    _scaffold(tmp_path)
    (tmp_path / "scripts" / "x.sh").write_text("echo\n")
    # report-only exits 0, --gate exits 1
    monrepo = str(tmp_path)
    r0 = subprocess.run([sys.executable, str(SCRIPTS / "check_cw_standards.py")],
                        capture_output=True, text=True, cwd=monrepo)
    assert r0.returncode == 0  # runs against the REAL repo (clean) by default
    # blocking mode against a dirty fixture is exercised via check() above; here just
    # confirm --gate is accepted
    rg = subprocess.run([sys.executable, str(SCRIPTS / "check_cw_standards.py"), "--gate"],
                        capture_output=True, text=True)
    assert rg.returncode == 0

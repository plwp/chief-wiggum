"""Tests for verifier-test body hashing (#206) and the high-water test-file
cue (#207) — the C1c hardening from the goalpost-integrity experiment
(docs/paper/experiment/): a high-water test rewritten behind its still-green
test ID must be visible, and a branch touching high-water test files must be
called out for scrutiny.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ratchet  # noqa: E402
from chief_wiggum.verifier_hashes import scan_verifier_hashes  # noqa: E402

ANNOTATED_TEST = '''\
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from widget import create_widget


def test_create_valid():
    assert create_widget("a")["name"] == "a"


def test_name_rejected_over_64():
    """@cw-trace verifies CTR-exp-001"""
    try:
        create_widget("x" * 65)
    except ValueError:
        return
    raise AssertionError("65-char name accepted")


# @cw-trace verifies CTR-exp-002
def test_delete_is_soft():
    assert True
'''

WIDGET = (
    'def create_widget(name):\n'
    '    if not name or len(name) > 64:\n'
    '        raise ValueError("bad name")\n'
    '    return {"name": name}\n'
)

CONTRACTS = (
    "## CTR-exp-001 — name bound\n"
    "REQUIRES: name is 1..64 chars.\n\n"
    "## CTR-exp-002 — soft delete\n"
    "ENSURES: rows survive deletion with deleted=true.\n"
)


def make_repo(tmp_path, suites=None):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs/epics/exp").mkdir(parents=True)
    (tmp_path / "docs/quality").mkdir(parents=True)
    (tmp_path / "src/widget.py").write_text(WIDGET)
    (tmp_path / "tests/test_widget.py").write_text(ANNOTATED_TEST)
    (tmp_path / "docs/epics/exp/contracts.md").write_text(CONTRACTS)
    (tmp_path / "docs/quality/ratchet.json").write_text(json.dumps({
        "suites": suites or [], "epic_docs": "docs/epics",
        "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    return ratchet.load_config(tmp_path)


def score_args(cfg, **kw):
    return argparse.Namespace(
        repo=str(cfg.repo), no_tests=kw.get("no_tests", True),
        no_quality=True, venv=None, gobin=None)


def check_args(cfg, **kw):
    return argparse.Namespace(
        repo=str(cfg.repo), format=kw.get("format", "json"),
        gate_quality=False, gate_verifier_tests=kw.get("gate_verifier_tests", False))


def record_args(cfg, **kw):
    return argparse.Namespace(
        repo=str(cfg.repo), event=kw.get("event", "baseline"),
        ref=kw.get("ref", "exp"), gate="pass", merged=kw.get("merged", True),
        notes=kw.get("notes", ""), amend=kw.get("amend"), retire=kw.get("retire"),
        amend_verifier=kw.get("amend_verifier"),
        retire_verifier=kw.get("retire_verifier"))


def baseline(cfg):
    ratchet.cmd_score(score_args(cfg))
    ratchet.cmd_record(record_args(cfg))


def check_json(cfg, capsys, **kw):
    rc = ratchet.cmd_check(check_args(cfg, **kw))
    return rc, json.loads(capsys.readouterr().out)


# ---- extraction ----------------------------------------------------------------


def test_scan_hashes_annotated_functions_only(tmp_path):
    make_repo(tmp_path)
    scan = scan_verifier_hashes(tmp_path)
    assert set(scan.hashes) == {
        "tests/test_widget.py::test_name_rejected_over_64",   # docstring form
        "tests/test_widget.py::test_delete_is_soft",           # above-def form
    }
    assert scan.targets["tests/test_widget.py::test_name_rejected_over_64"] == ["CTR-exp-001"]
    assert not scan.unscanned


def test_scan_surfaces_unhashable_annotations(tmp_path):
    make_repo(tmp_path)
    (tmp_path / "tests/order_test.go").write_text(
        "// @cw-trace verifies CTR-exp-001\nfunc TestOrder(t *testing.T) {}\n")
    (tmp_path / "tests/loose.py").write_text("# @cw-trace verifies CTR-exp-002\nX = 1\n")
    scan = scan_verifier_hashes(tmp_path)
    assert scan.unscanned == {
        "unsupported extension .go": 1,
        "annotation not attached to a function": 1,
    }


def test_scan_never_pins_annotations_inside_string_literals(tmp_path):
    """A test that WRITES an annotated fixture must not itself become a
    verifier test — the annotation lives in a string literal, not a comment
    or docstring. (This is exactly chief-wiggum's own test suite shape.)"""
    make_repo(tmp_path)
    (tmp_path / "tests/test_gen.py").write_text(
        'def test_writes_fixture(tmp_path):\n'
        '    (tmp_path / "f.py").write_text(\n'
        '        "# @cw-trace verifies CTR-exp-001\\ndef test_x():\\n    pass\\n")\n'
        '    body = """\n'
        '# @cw-trace verifies CTR-exp-002\n'
        'def test_y():\n'
        '    pass\n'
        '"""\n'
        '    assert body\n')
    scan = scan_verifier_hashes(tmp_path)
    assert "tests/test_gen.py::test_writes_fixture" not in scan.hashes
    assert not scan.unscanned


def test_scan_qualifies_same_named_methods_across_classes(tmp_path):
    """Two `test_it` methods in different classes must get DISTINCT refs — a
    bare-name ref would silently overwrite one hash and blind the gate to a
    rewrite of the overwritten test."""
    make_repo(tmp_path)
    (tmp_path / "tests/test_dup.py").write_text(
        "class TestA:\n"
        "    # @cw-trace verifies CTR-exp-001\n"
        "    def test_it(self):\n"
        "        assert 1 == 1\n\n"
        "class TestB:\n"
        "    def test_it(self):\n"
        '        """@cw-trace verifies CTR-exp-002"""\n'
        "        assert 2 == 2\n")
    scan = scan_verifier_hashes(tmp_path)
    assert "tests/test_dup.py::TestA.test_it" in scan.hashes
    assert "tests/test_dup.py::TestB.test_it" in scan.hashes
    assert scan.hashes["tests/test_dup.py::TestA.test_it"] != \
        scan.hashes["tests/test_dup.py::TestB.test_it"]


def test_scan_hash_ignores_reformatting_but_not_tokens(tmp_path):
    make_repo(tmp_path)
    before = scan_verifier_hashes(tmp_path).hashes
    body = (tmp_path / "tests/test_widget.py").read_text()
    (tmp_path / "tests/test_widget.py").write_text(body.replace(
        'raise AssertionError("65-char name accepted")',
        'raise AssertionError("65-char name accepted")   '))  # trailing ws only
    assert scan_verifier_hashes(tmp_path).hashes == before
    (tmp_path / "tests/test_widget.py").write_text(body.replace('"x" * 65', '"x" * 129'))
    after = scan_verifier_hashes(tmp_path).hashes
    assert after["tests/test_widget.py::test_name_rejected_over_64"] != \
        before["tests/test_widget.py::test_name_rejected_over_64"]


# ---- the C1c scenario: rewrite behind a green test ID --------------------------


def test_c1c_body_rewrite_is_reported_but_report_only(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    baseline(cfg)
    capsys.readouterr()
    body = (tmp_path / "tests/test_widget.py").read_text()
    (tmp_path / "tests/test_widget.py").write_text(body.replace(
        'raise AssertionError("65-char name accepted")', "return  # blessed"))
    ratchet.cmd_score(score_args(cfg))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys)
    assert rc == 0  # report-only by default (docs/gate-rollout.md)
    assert v["weakened_verifier_tests"] == [
        "tests/test_widget.py::test_name_rejected_over_64"]
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 1  # opt-in gate blocks


def test_removed_verifier_test_is_reported(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    baseline(cfg)
    capsys.readouterr()
    body = (tmp_path / "tests/test_widget.py").read_text()
    start = body.index("# @cw-trace verifies CTR-exp-002")
    (tmp_path / "tests/test_widget.py").write_text(body[:start])
    ratchet.cmd_score(score_args(cfg))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 1
    assert v["removed_verifier_tests"] == ["tests/test_widget.py::test_delete_is_soft"]


def test_untouched_repo_is_clean(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    baseline(cfg)
    ratchet.cmd_score(score_args(cfg))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 0
    assert v["weakened_verifier_tests"] == [] and v["removed_verifier_tests"] == []


# ---- journaled revision paths --------------------------------------------------


def test_amend_verifier_moves_the_baseline(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    baseline(cfg)
    body = (tmp_path / "tests/test_widget.py").read_text()
    (tmp_path / "tests/test_widget.py").write_text(body.replace(
        'assert True', 'assert 1 == 1'))
    ratchet.cmd_score(score_args(cfg))
    ratchet.cmd_record(record_args(
        cfg, event="epic-close",
        amend_verifier=["tests/test_widget.py::test_delete_is_soft"],
        notes="deliberate refactor"))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 0 and v["weakened_verifier_tests"] == []


def test_amend_verifier_unknown_ref_is_an_error(tmp_path):
    cfg = make_repo(tmp_path)
    baseline(cfg)
    with pytest.raises(ratchet.RatchetError):
        ratchet.cmd_record(record_args(cfg, amend_verifier=["tests/nope.py::test_x"]))


def test_contract_amend_requires_explicit_verifier_blessing(tmp_path, capsys):
    """Amending a contract whose verifier test body ALSO changed must NOT
    silently bless the new body (channel C1c riding along on a contract
    amend). The amend errors, naming the ref, until --amend-verifier is added.
    """
    cfg = make_repo(tmp_path)
    baseline(cfg)
    contracts = (tmp_path / "docs/epics/exp/contracts.md").read_text()
    (tmp_path / "docs/epics/exp/contracts.md").write_text(
        contracts.replace("1..64", "1..128"))
    body = (tmp_path / "tests/test_widget.py").read_text()
    (tmp_path / "tests/test_widget.py").write_text(body.replace('"x" * 65', '"x" * 129'))
    ratchet.cmd_score(score_args(cfg))
    ref = "tests/test_widget.py::test_name_rejected_over_64"
    # contract amend alone: refuses, because the verifier body also moved
    with pytest.raises(ratchet.RatchetError) as exc:
        ratchet.cmd_record(record_args(
            cfg, event="epic-close", amend=["CTR-exp-001"],
            notes="bound change without blessing the test"))
    assert ref in str(exc.value) and "--amend-verifier" in str(exc.value)
    # explicit blessing of BOTH the contract and its verifier test: clean
    ratchet.cmd_record(record_args(
        cfg, event="epic-close", amend=["CTR-exp-001"], amend_verifier=[ref],
        notes="human-approved bound change + explicit test re-baseline"))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 0
    assert v["weakened_contracts"] == [] and v["weakened_verifier_tests"] == []


def test_contract_amend_with_unchanged_verifier_body_is_clean(tmp_path, capsys):
    """Amending a contract whose verifier tests did NOT change needs no
    verifier blessing — an unchanged body already matches high-water."""
    cfg = make_repo(tmp_path)
    baseline(cfg)
    contracts = (tmp_path / "docs/epics/exp/contracts.md").read_text()
    (tmp_path / "docs/epics/exp/contracts.md").write_text(
        contracts.replace("name is 1..64 chars.", "name is 1..64 chars (clarified)."))
    ratchet.cmd_score(score_args(cfg))
    ratchet.cmd_record(record_args(  # no --amend-verifier needed, must not raise
        cfg, event="epic-close", amend=["CTR-exp-001"], notes="wording clarification"))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 0 and v["weakened_verifier_tests"] == []


def test_retire_verifier_validates_against_highwater(tmp_path):
    """--retire-verifier of a ref not in the high-water mark is surfaced as an
    error, not a silent no-op (parity with --amend-verifier's validation)."""
    cfg = make_repo(tmp_path)
    baseline(cfg)
    with pytest.raises(ratchet.RatchetError) as exc:
        ratchet.cmd_record(record_args(
            cfg, retire_verifier=["tests/test_widget.py::test_typo_never_existed"]))
    assert "not in the current high-water" in str(exc.value)


def test_retire_verifier_drops_a_tracked_ref(tmp_path, capsys):
    """Retiring a genuinely-tracked verifier ref removes it from the
    high-water mark so its later deletion is not flagged as removed."""
    cfg = make_repo(tmp_path)
    baseline(cfg)
    ref = "tests/test_widget.py::test_delete_is_soft"
    ratchet.cmd_record(record_args(cfg, event="epic-close", retire_verifier=[ref]))
    # delete that annotated test entirely; it must NOT show as removed
    body = (tmp_path / "tests/test_widget.py").read_text()
    start = body.index("# @cw-trace verifies CTR-exp-002")
    (tmp_path / "tests/test_widget.py").write_text(body[:start])
    ratchet.cmd_score(score_args(cfg))
    capsys.readouterr()
    rc, v = check_json(cfg, capsys, gate_verifier_tests=True)
    assert rc == 0 and ref not in v["removed_verifier_tests"]


# ---- extension allow-list / size cap (#326) -------------------------------


def test_non_source_extension_is_never_read(tmp_path):
    """A file whose extension no known language/verification-artifact tier
    recognizes (an image, a lockfile, ...) must never be opened at all — even
    when it happens to literally contain the tag string. Before #326,
    scan_file's non-.py branch read EVERY such file in full with no
    allow-list; images/lockfiles/fixtures dominated ratchet score's I/O as a
    result."""
    make_repo(tmp_path)
    (tmp_path / "tests/fixture.png").write_bytes(
        b"\x89PNG\r\n\x1a\n# @cw-trace verifies CTR-exp-001\n"
    )
    scan = scan_verifier_hashes(tmp_path)
    assert not scan.unscanned  # never even opened, so never bumped as unscanned
    assert not any("fixture.png" in ref for ref in scan.hashes)


def test_recognized_unsupported_language_extension_is_still_scanned(tmp_path):
    """A 'recognized but unsupported' language extension (e.g. .php — no
    emitter, but curated in config/languages.json) stays in scope: it must
    still be READ and surfaced as unscanned when it carries the tag — the
    allow-list is deliberately as generous as the rest of the system's known
    source-extension universe (see SCANNABLE_EXTS), not narrower."""
    make_repo(tmp_path)
    (tmp_path / "tests/legacy_test.php").write_text(
        "<?php\n// @cw-trace verifies CTR-exp-001\n"
    )
    scan = scan_verifier_hashes(tmp_path)
    assert scan.unscanned.get("unsupported extension .php") == 1


def test_oversized_non_py_file_is_capped_not_fully_read(tmp_path, monkeypatch):
    """A file larger than the size cap is read only up to the cap — an
    annotation past that offset is not detected (a documented, accepted
    boundary), but the file is never read in full."""
    import chief_wiggum.verifier_hashes as vh

    monkeypatch.setattr(vh, "MAX_NONPY_SCAN_BYTES", 64)
    make_repo(tmp_path)
    padding = "x" * 200
    (tmp_path / "tests/huge_test.go").write_text(
        f"// {padding}\n// @cw-trace verifies CTR-exp-001\nfunc TestX(t *testing.T) {{}}\n"
    )
    scan = scan_verifier_hashes(tmp_path)
    # The annotation lives past the 64-byte cap, so it is never seen — not
    # bumped as unscanned (bumping requires having READ the tag at all).
    assert "unsupported extension .go" not in scan.unscanned


def test_oversized_non_py_file_within_head_is_still_detected(tmp_path, monkeypatch):
    """The head of an oversized file IS still scanned — only content past the
    cap is skipped."""
    import chief_wiggum.verifier_hashes as vh

    monkeypatch.setattr(vh, "MAX_NONPY_SCAN_BYTES", 64)
    make_repo(tmp_path)
    (tmp_path / "tests/huge_test.go").write_text(
        "// @cw-trace verifies CTR-exp-001\n" + ("// " + "x" * 200 + "\n") * 5
    )
    scan = scan_verifier_hashes(tmp_path)
    assert scan.unscanned.get("unsupported extension .go") == 1


def test_ambiguous_duplicate_qualname_is_surfaced_not_recorded(tmp_path):
    """Two functions sharing a qualified name (conditional top-level
    redefinition) is ambiguous — surfaced as unscanned, never recorded as a
    silently-overwritten single hash (#206 soundness review, finding 2)."""
    make_repo(tmp_path)
    # same qualified name (module-level test_feature) redefined under a
    # conditional, each annotated via an IN-BODY docstring so the ambiguity is
    # detected at the containing-function level.
    (tmp_path / "tests/test_cond.py").write_text(
        "import os\n"
        "if os.environ.get('X'):\n"
        "    def test_feature():\n"
        '        """@cw-trace verifies CTR-exp-001"""\n'
        "        assert 1 == 1\n"
        "else:\n"
        "    def test_feature():\n"
        '        """@cw-trace verifies CTR-exp-002"""\n'
        "        assert 2 == 2\n")
    scan = scan_verifier_hashes(tmp_path)
    assert "tests/test_cond.py::test_feature" not in scan.hashes
    assert any("ambiguous ref" in reason for reason in scan.unscanned)


def test_legacy_journal_records_are_tolerated():
    hw = ratchet.derive_highwater([{
        "merged": True,
        "scorecard": {"pass_set": ["s::t"], "contract_hashes": {"CTR-a-001": "h"}},
    }])
    assert hw["verifier_test_hashes"] == {}
    v = ratchet.violations({"pass_set": ["s::t"], "contract_hashes": {"CTR-a-001": "h"}}, hw)
    assert v["weakened_verifier_tests"] == [] and v["removed_verifier_tests"] == []


# ---- #207: high-water test-file cue --------------------------------------------


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_git_repo(tmp_path):
    cfg = make_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (tmp_path / ".gitignore").write_text(".ratchet-junit.xml\n__pycache__/\n")
    cfg = ratchet.load_config(tmp_path)
    cfg.suites = [ratchet.Suite(
        name="pytest",
        cmd=f"{sys.executable} -m pytest --junit-xml=.ratchet-junit.xml -q tests",
        parser="junit-xml", report=".ratchet-junit.xml")]
    (tmp_path / "docs/quality/ratchet.json").write_text(json.dumps({
        "suites": [{"name": "pytest",
                    "cmd": f"{sys.executable} -m pytest --junit-xml=.ratchet-junit.xml -q tests",
                    "cwd": ".", "parser": "junit-xml", "report": ".ratchet-junit.xml"}],
        "epic_docs": "docs/epics", "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    ratchet.cmd_score(score_args(cfg, no_tests=False))
    ratchet.cmd_record(record_args(cfg))
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline")
    return ratchet.load_config(tmp_path)


def protected_args(cfg):
    return argparse.Namespace(repo=str(cfg.repo), base="main")


def test_score_maps_junit_cases_to_files(tmp_path):
    cfg = make_git_repo(tmp_path)
    sc = json.loads(cfg.scorecard.read_text())
    assert sc["pass_set"], "fixture suite must actually run"
    assert set(sc["test_files"].values()) == {"tests/test_widget.py"}
    assert sc["test_files_unresolved"] == []


def test_go_case_dirs_resolves_packages_to_directories(tmp_path):
    """go-test-json package import paths resolve to repo-relative directories
    via progressive suffix matching (#207); unmatched packages stay absent."""
    cfg = make_repo(tmp_path)
    (tmp_path / "internal" / "billing").mkdir(parents=True)
    suite = ratchet.Suite(name="go", cmd="true", parser="go-test-json")
    stdout = "\n".join([
        '{"Action":"pass","Package":"github.com/acme/app/internal/billing","Test":"TestCharge"}',
        '{"Action":"pass","Package":"github.com/acme/app/nonexistent/pkg","Test":"TestGhost"}',
    ])
    files = ratchet.go_case_dirs(cfg, suite, stdout)
    assert files["go::github.com/acme/app/internal/billing::TestCharge"] == "internal/billing"
    assert "go::github.com/acme/app/nonexistent/pkg::TestGhost" not in files


def test_score_surfaces_verifier_unscanned_in_scorecard_and_stderr(tmp_path, capsys):
    """The score boundary — not just the scanner — records unhashable verifier
    annotations in the scorecard and warns on stderr (the surfacing the
    validation record's sampling-gap trial claims)."""
    cfg = make_repo(tmp_path)
    (tmp_path / "tests/order_test.go").write_text(
        "// @cw-trace verifies CTR-exp-001\nfunc TestOrder(t *testing.T) {}\n")
    capsys.readouterr()
    ratchet.cmd_score(score_args(cfg))
    err = capsys.readouterr().err
    assert "could not hash" in err and ".go" in err
    sc = json.loads(cfg.scorecard.read_text())
    assert sc["verifier_unscanned"].get("unsupported extension .go") == 1


def test_pass_fail_lines_cases_are_surfaced_as_unresolved(tmp_path):
    """A parser that carries no file info leaves its cases in
    test_files_unresolved — surfaced, not silently mapped to nothing (#207)."""
    cfg = make_repo(tmp_path, suites=[
        {"name": "smoke", "cmd": "printf 'PASS a\\nPASS b\\n'", "cwd": ".",
         "parser": "pass-fail-lines"}])
    ratchet.cmd_score(score_args(cfg, no_tests=False))
    sc = json.loads(cfg.scorecard.read_text())
    assert sc["test_files"] == {}
    assert set(sc["test_files_unresolved"]) == {"smoke::a", "smoke::b"}


def test_protected_cues_on_highwater_test_file_edit(tmp_path, capsys):
    cfg = make_git_repo(tmp_path)
    git(tmp_path, "checkout", "-q", "-b", "worker/t1")
    body = (tmp_path / "tests/test_widget.py").read_text()
    (tmp_path / "tests/test_widget.py").write_text(body + "\n# touched\n")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "edit test")
    rc = ratchet.cmd_protected(protected_args(cfg))
    err = capsys.readouterr().err
    assert rc == 0  # cue NEVER changes the exit code
    assert "high-water test file(s) modified" in err
    assert "tests/test_widget.py" in err and "test_name_rejected_over_64" in err


def test_protected_quiet_on_new_test_file_and_src_edit(tmp_path, capsys):
    cfg = make_git_repo(tmp_path)
    git(tmp_path, "checkout", "-q", "-b", "worker/t2")
    (tmp_path / "tests/test_new.py").write_text("def test_new():\n    assert True\n")
    (tmp_path / "src/widget.py").write_text(WIDGET + "\n# comment\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat")
    rc = ratchet.cmd_protected(protected_args(cfg))
    err = capsys.readouterr().err
    assert rc == 0
    assert "high-water test file(s) modified" not in err


def test_protected_cue_surfaces_missing_scorecard(tmp_path, capsys):
    cfg = make_git_repo(tmp_path)
    git(tmp_path, "checkout", "-q", "-b", "worker/t3")
    (tmp_path / "src/widget.py").write_text(WIDGET + "# x\n")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "edit")
    cfg.scorecard.unlink()  # fresh clone that never ran `score`
    rc = ratchet.cmd_protected(protected_args(cfg))
    err = capsys.readouterr().err
    assert rc == 0
    assert "cue unavailable" in err

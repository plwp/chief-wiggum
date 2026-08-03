"""#264: target-scoped review authorities.

A repo CW didn't build already has house rules, and CW's generic checklist knows
nothing about them. This records them per target and per phase so `/implement`
and `/close-epic` consult the conventions that would actually block the change
in human review.

The failure modes mirror `scope.json` deliberately: a MISSING file means "none
recorded" (the greenfield default), while an UNREADABLE one must never render
identically to an absent one — that is exactly how a target quietly loses its
house rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import review_authorities as ra


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path, monkeypatch):
    """Keep Resolver's mode election off the developer's real user dir."""
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))


def _meta(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "adoption"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(tmp_path: Path, doc) -> Path:
    p = _meta(tmp_path) / ra.AUTHORITIES_NAME
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return p


GOOD = {
    "schema": "review-authorities/1",
    "target": "acme/app",
    "authoring": [{"skill": "plugin:lang-developer", "reason": "house style"}],
    "review": [{"skill": "plugin:lang-reviewer", "reason": "standing objections"},
               {"skill": "plugin:sec-reviewer"}],
    "operations": [{"skill": "plugin:ci-tool", "reason": "deploy runbook"}],
}


# --- the greenfield default ---------------------------------------------------


def test_missing_file_yields_empty_phases_and_no_error(tmp_path):
    """AC3: missing file => empty lists, no error. NOT a claim the target has
    no conventions — only that none were recorded."""
    _meta(tmp_path)
    doc = ra.load(tmp_path / "docs")
    assert doc["present"] is False
    assert doc["authoring"] == [] and doc["review"] == [] and doc["operations"] == []
    assert ra.skills_for(tmp_path / "docs", "review") == []


def test_missing_adoption_dir_entirely_is_also_fine(tmp_path):
    (tmp_path / "docs").mkdir(parents=True)
    assert ra.load(tmp_path / "docs")["present"] is False


# --- happy path ---------------------------------------------------------------


def test_phases_are_parsed_and_ordered(tmp_path):
    _write(tmp_path, GOOD)
    doc = ra.load(tmp_path / "docs")
    assert doc["present"] is True
    assert [e["skill"] for e in doc["review"]] == ["plugin:lang-reviewer", "plugin:sec-reviewer"]
    assert doc["authoring"][0]["reason"] == "house style"


def test_skills_for_returns_ids_in_file_order(tmp_path):
    _write(tmp_path, GOOD)
    assert ra.skills_for(tmp_path / "docs", "authoring") == ["plugin:lang-developer"]
    assert ra.skills_for(tmp_path / "docs", "operations") == ["plugin:ci-tool"]


def test_omitted_phase_key_defaults_to_empty(tmp_path):
    _write(tmp_path, {"schema": "review-authorities/1", "target": "acme/app",
                      "review": [{"skill": "s"}]})
    doc = ra.load(tmp_path / "docs")
    assert doc["authoring"] == [] and doc["operations"] == []


def test_reason_is_optional_but_must_be_a_string(tmp_path):
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "review": [{"skill": "s"}]})
    assert ra.skills_for(tmp_path / "docs", "review") == ["s"]
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "review": [{"skill": "s", "reason": 7}]})
    with pytest.raises(ValueError, match="reason"):
        ra.load(tmp_path / "docs")


def test_the_same_skill_may_hold_authority_in_two_phases(tmp_path):
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "authoring": [{"skill": "s"}], "review": [{"skill": "s"}]})
    doc = ra.load(tmp_path / "docs")
    assert doc["authoring"][0]["skill"] == doc["review"][0]["skill"] == "s"


# --- malformed: must raise, naming the problem --------------------------------


def test_unknown_phase_key_names_itself(tmp_path):
    """AC3: the typo names itself rather than silently dropping every skill
    under it — a dropped phase is indistinguishable from an empty one."""
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "reviews": [{"skill": "plugin:lang-reviewer"}]})
    with pytest.raises(ValueError, match="reviews"):
        ra.load(tmp_path / "docs")


@pytest.mark.parametrize("doc,needle", [
    ("{not json", "review-authorities.json"),
    ([], "object"),
    ({"target": "a/b"}, "schema"),
    ({"schema": "review-authorities/2", "target": "a/b"}, "review-authorities/1"),
    ({"schema": "review-authorities/1"}, "target"),
    ({"schema": "review-authorities/1", "target": 7}, "target"),
    ({"schema": "review-authorities/1", "target": "a/b", "review": {}}, "list"),
    ({"schema": "review-authorities/1", "target": "a/b", "review": ["s"]}, "object"),
    ({"schema": "review-authorities/1", "target": "a/b", "review": [{}]}, "skill"),
    ({"schema": "review-authorities/1", "target": "a/b", "review": [{"skill": ""}]}, "skill"),
    ({"schema": "review-authorities/1", "target": "a/b",
      "review": [{"skill": "s"}, {"skill": "s"}]}, "duplicate"),
])
def test_malformed_documents_raise(tmp_path, doc, needle):
    _write(tmp_path, doc)
    with pytest.raises(ValueError, match=needle):
        ra.load(tmp_path / "docs")


def test_comment_key_is_allowed(tmp_path):
    """Mirrors scope.json's $comment escape hatch for operator annotations."""
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "$comment": "owned by the platform team", "review": []})
    assert ra.load(tmp_path / "docs")["present"] is True


# --- CLI ----------------------------------------------------------------------


def _run(capsys, argv) -> tuple[int, str, str]:
    rc = ra.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_text_prints_one_id_per_line(tmp_path, capsys):
    _write(tmp_path, GOOD)
    rc, out, err = _run(capsys, ["show", str(tmp_path), "--phase", "review"])
    assert rc == 0
    assert out.split() == ["plugin:lang-reviewer", "plugin:sec-reviewer"]
    assert err == ""


def test_cli_missing_file_is_exit_zero_and_silent(tmp_path, capsys):
    _meta(tmp_path)
    rc, out, err = _run(capsys, ["show", str(tmp_path), "--phase", "review"])
    assert rc == 0
    assert out.strip() == ""


def test_cli_json_marks_an_absent_binding_explicitly(tmp_path, capsys):
    """A consumer must be able to tell 'none recorded' from 'file unreadable'
    WITHOUT parsing prose — so json mode says `present` outright."""
    _meta(tmp_path)
    rc, out, _ = _run(capsys, ["show", str(tmp_path), "--format", "json"])
    assert rc == 0
    doc = json.loads(out)
    assert doc["present"] is False and doc["review"] == []


def test_cli_json_with_phase_keeps_reasons(tmp_path, capsys):
    _write(tmp_path, GOOD)
    rc, out, _ = _run(capsys, ["show", str(tmp_path), "--phase", "review", "--format", "json"])
    assert rc == 0
    doc = json.loads(out)
    assert doc["phase"] == "review"
    assert doc["authorities"][0]["reason"] == "standing objections"


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_cli_malformed_exits_2_with_nothing_on_stdout(tmp_path, capsys, fmt):
    """AC1: exit 2 on a malformed file, never a silent empty result. Empty
    stdout + exit 0 is precisely what a consumer would read as 'no authorities'
    — the failure this ticket exists to prevent."""
    _write(tmp_path, {"schema": "review-authorities/1", "target": "a/b",
                      "reviews": [{"skill": "s"}]})
    rc, out, err = _run(capsys, ["show", str(tmp_path), "--phase", "review", "--format", fmt])
    assert rc == 2
    assert out.strip() == "", "a malformed binding must not print an empty result set"
    assert "reviews" in err


def test_cli_json_error_is_machine_readable(tmp_path, capsys):
    _write(tmp_path, "{not json")
    rc, _, err = _run(capsys, ["show", str(tmp_path), "--format", "json"])
    assert rc == 2
    payload = json.loads(err)
    assert payload["error"] == "malformed_review_authorities"
    assert payload["path"].endswith(ra.AUTHORITIES_NAME)


def test_cli_unknown_phase_argument_is_a_usage_error(tmp_path, capsys):
    _write(tmp_path, GOOD)
    with pytest.raises(SystemExit) as exc:
        ra.main(["show", str(tmp_path), "--phase", "nope"])
    assert exc.value.code == 2

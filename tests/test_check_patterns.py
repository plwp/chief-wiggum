"""Tests for scripts/check_patterns.py."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_patterns  # noqa: E402

ERRORS = check_patterns.ERROR


def _errors(findings):
    return [f for f in findings if f.severity == check_patterns.ERROR]


def _write(tmp_path, registry, manifests=None):
    """Materialize a fake registry + manifests under tmp_path/patterns."""
    pdir = tmp_path / "patterns"
    pdir.mkdir(exist_ok=True)
    (pdir / "registry.json").write_text(json.dumps(registry))
    for pid, manifest in (manifests or {}).items():
        d = pdir / pid
        d.mkdir(exist_ok=True)
        (d / "manifest.json").write_text(json.dumps(manifest))
        (d / "pattern.md").write_text(f"# {pid}\n")
    return pdir / "registry.json"


def _specified(pid, **extra):
    entry = {"id": pid, "status": "specified",
             "spec": f"patterns/{pid}/pattern.md",
             "manifest": f"patterns/{pid}/manifest.json"}
    entry.update(extra)
    return entry


def _manifest(pid, cluster=None, **extra):
    m = {"id": pid, "title": pid}
    if cluster is not None:
        m["invariants"] = {"cluster": cluster}
    m.update(extra)
    return m


GOOD_INV = {"id": "INV-XYZ-001", "statement": "must stay true"}


# --- the real registry must pass -------------------------------------------

def test_real_registry_has_no_errors():
    """The shipped registry satisfies the invariant-cluster model."""
    findings = check_patterns.validate()
    assert _errors(findings) == [], "\n".join(str(f) for f in _errors(findings))


def test_cli_exit_zero_on_real_registry():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_patterns.py")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the bar for `specified` ------------------------------------------------

def test_specified_without_cluster_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo")})  # no cluster
    errs = _errors(check_patterns.validate(path))
    assert any("non-empty invariant cluster" in e.message for e in errs)


def test_specified_with_cluster_passes(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert _errors(check_patterns.validate(path)) == []


# --- cluster entry validation ----------------------------------------------

def test_malformed_invariant_id_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    bad = [{"id": "inv-lowercase-1", "statement": "x"}]
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=bad)})
    assert any("malformed invariant id" in e.message for e in _errors(check_patterns.validate(path)))


def test_duplicate_invariant_id_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    dup = [dict(GOOD_INV), dict(GOOD_INV)]
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=dup)})
    assert any("duplicate invariant id" in e.message for e in _errors(check_patterns.validate(path)))


def test_missing_statement_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[{"id": "INV-XYZ-001"}])})
    assert any("missing `statement`" in e.message for e in _errors(check_patterns.validate(path)))


def test_realized_as_is_optional(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert _errors(check_patterns.validate(path)) == []


def test_malformed_realized_as_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    bad = [{"id": "INV-XYZ-001", "statement": "x", "realized_as": {"app": "a"}}]  # no code/id
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=bad)})
    assert any("realized_as" in e.message for e in _errors(check_patterns.validate(path)))


def test_sibling_branch_cluster_is_validated(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    manifest = _manifest("foo", cluster=[GOOD_INV])
    manifest["invariants"]["sibling_monotonic_branch"] = {"cluster": [{"id": "bad", "statement": "x"}]}
    path = _write(tmp_path, reg, {"foo": manifest})
    assert any("malformed invariant id" in e.message for e in _errors(check_patterns.validate(path)))


# --- cross-reference integrity ---------------------------------------------

def test_unknown_dependency_is_error(tmp_path):
    reg = {"patterns": [_specified("foo", depends_on="ghost")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert any("depends_on unknown" in e.message for e in _errors(check_patterns.validate(path)))


def test_specified_depends_on_candidate_is_warning_not_error(tmp_path):
    reg = {
        "patterns": [_specified("foo", depends_on="floor")],
        "candidates": [{"id": "floor", "status": "candidate"}],
    }
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    findings = check_patterns.validate(path)
    assert _errors(findings) == []
    assert any(f.severity == check_patterns.WARN and "not-yet-specified floor" in f.message for f in findings)


def test_manifest_id_mismatch_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("bar", cluster=[GOOD_INV])})
    assert any("!= registry id" in e.message for e in _errors(check_patterns.validate(path)))


def test_candidate_malformed_cluster_is_error(tmp_path):
    reg = {
        "patterns": [],
        "candidates": [{"id": "cand", "invariants": [{"id": "nope", "statement": "x"}]}],
    }
    path = _write(tmp_path, reg, {})
    assert any("malformed invariant id" in e.message for e in _errors(check_patterns.validate(path)))

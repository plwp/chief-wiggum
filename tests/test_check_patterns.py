"""Tests for scripts/check_patterns.py."""

import json
import re
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
             "invariants": "INV-XYZ-001",
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


def test_specified_missing_index_invariants_summary_is_error(tmp_path):
    reg = {"patterns": [_specified("foo", invariants="")], "candidates": []}  # index entry lacks the summary
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert any("index entry missing `invariants` summary" in e.message for e in _errors(check_patterns.validate(path)))


def test_specified_with_index_summary_passes(tmp_path):
    reg = {"patterns": [_specified("foo", invariants="INV-XYZ-001")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert _errors(check_patterns.validate(path)) == []


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


# --- referral-invite-loop promotion (#139) ----------------------------------

def test_real_registry_passes_check_patterns():
    """The shipped registry (incl. the promoted referral-invite-loop) validates."""
    findings = check_patterns.validate()
    assert _errors(findings) == [], _errors(findings)


def test_referral_invite_loop_is_specified_with_grounded_cluster():
    reg = json.loads((SCRIPTS.parent / "patterns" / "registry.json").read_text())
    entry = next((e for e in reg["patterns"] if e["id"] == "referral-invite-loop"), None)
    assert entry is not None, "referral-invite-loop must be a specified pattern, not a candidate"
    assert entry["status"] == "specified"
    assert entry.get("depends_on") == "elevated-access-session"
    assert not any(c["id"] == "referral-invite-loop" for c in reg.get("candidates", []))
    manifest = json.loads(
        (SCRIPTS.parent / "patterns" / "referral-invite-loop" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    ids = [e["id"] for e in cluster]
    assert ids == [f"INV-RIL-00{n}" for n in range(1, 7)]
    # the token-discipline invariants cite the in-repo elevated-access-session grounding
    grounded = [e for e in cluster if isinstance(e.get("realized_as"), dict)
                and "elevated-access-session" in e["realized_as"].get("code", "")]
    assert {e["id"] for e in grounded} == {"INV-RIL-001", "INV-RIL-002", "INV-RIL-003"}


# --- #139 final promotions: the last four candidates --------------------------

FINAL_PROMOTIONS = {
    "reconciliation-sweep": ("INV-RSW", 7, "fetch-on-webhook-reconcile"),
    "feature-entitlements": ("INV-FE", 6, "entitlement-overlay"),
    "self-serve-billing-portal": (
        "INV-SBP", 6, "fetch-on-webhook-reconcile, feature-entitlements"),
    "transactional-email-and-dunning": (
        "INV-TED", 7, "provider-neutral-adapter, feature-entitlements"),
}


def test_final_four_are_specified_and_candidates_is_empty():
    reg = json.loads((SCRIPTS.parent / "patterns" / "registry.json").read_text())
    assert reg.get("candidates") == [], "#139 done looks like: zero remaining candidates"
    for pid, (prefix, n, dep) in FINAL_PROMOTIONS.items():
        entry = next((e for e in reg["patterns"] if e["id"] == pid), None)
        assert entry is not None, f"{pid} must be a specified pattern"
        assert entry["status"] == "specified"
        assert entry.get("depends_on") == dep
        # the index's invariants summary stays in sync with the manifest cluster
        assert entry["invariants"] == f"{prefix}-001..00{n}", entry["invariants"]


def test_final_four_clusters_are_complete_and_grounding_is_honest():
    """Every invariant carries exactly one honest grounding class: in-repo
    provenance, mined-anonymized provenance (mechanism-described, never a
    path), or an explicit design-derived marker. The anonymization policy is
    enforced with real checks: no file:line refs, no path separators, no
    source-file extensions in private provenance."""
    path_like = re.compile(r":\d+|\.(go|ts|tsx|js|py|rb|java)\b|/")
    for pid, (prefix, n, _) in FINAL_PROMOTIONS.items():
        manifest = json.loads(
            (SCRIPTS.parent / "patterns" / pid / "manifest.json").read_text())
        cluster = check_patterns.cluster_entries(manifest["invariants"])
        ids = [e["id"] for e in cluster]
        assert ids == [f"{prefix}-00{i}" for i in range(1, n + 1)], ids
        for e in cluster:
            grounding = e.get("grounding", "")
            in_repo = (isinstance(e.get("realized_as"), dict)
                       and "chief-wiggum" in e["realized_as"]["app"])
            mined = grounding.startswith("mined-anonymized")
            is_design = grounding == "design-derived"
            assert in_repo or mined or is_design, (
                f"{e['id']} has no honest grounding class")
            if mined:
                ra = e.get("realized_as")
                assert isinstance(ra, dict), f"{e['id']} mined without realized_as mechanism"
                code = ra.get("code", "")
                assert not path_like.search(code), (
                    f"{e['id']} leaks path-like provenance: {code}")


def test_dunning_half_is_flagged_aspirational():
    manifest = json.loads((SCRIPTS.parent / "patterns" /
                           "transactional-email-and-dunning" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    dunning = next(e for e in cluster if e["id"] == "INV-TED-006")
    assert dunning.get("grounding") == "design-derived"
    assert "ASPIRATIONAL" in dunning["statement"], (
        "issue #139 flags the dunning half aspirational — the invariant must say so")

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
    # success_metrics is part of the bar for `specified` (#234) — default it so
    # fixtures exercise their own concern, not the metrics lint.
    m = {"id": pid, "title": pid,
         "success_metrics": {"metrics": [{"id": "m1", "goal": "down", "desc": "d"}]}}
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


# --- #234: success_metrics enforcement ---------------------------------------

def test_specified_missing_success_metrics_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    manifest = _manifest("foo", cluster=[GOOD_INV])
    del manifest["success_metrics"]
    path = _write(tmp_path, reg, {"foo": manifest})
    assert any("success_metrics.metrics" in e.message for e in _errors(check_patterns.validate(path)))


def test_specified_empty_success_metrics_is_error(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg,
                  {"foo": _manifest("foo", cluster=[GOOD_INV], success_metrics={"metrics": []})})
    assert any("success_metrics.metrics" in e.message for e in _errors(check_patterns.validate(path)))


def test_specified_with_success_metrics_passes(tmp_path):
    reg = {"patterns": [_specified("foo")], "candidates": []}
    path = _write(tmp_path, reg, {"foo": _manifest("foo", cluster=[GOOD_INV])})
    assert _errors(check_patterns.validate(path)) == []


def test_candidate_missing_success_metrics_is_warn_not_error(tmp_path):
    reg = {"patterns": [], "candidates": [{"id": "cand", "status": "candidate"}]}
    path = _write(tmp_path, reg, {})
    findings = check_patterns.validate(path)
    assert _errors(findings) == []
    assert any(f.severity == check_patterns.WARN and "success_metrics" in f.message
               for f in findings)


def test_candidate_with_success_metrics_gets_no_warn(tmp_path):
    reg = {"patterns": [], "candidates": [
        {"id": "cand", "status": "candidate",
         "success_metrics": {"metrics": [{"id": "m1", "goal": "down", "desc": "d"}]}}]}
    path = _write(tmp_path, reg, {})
    assert not any("success_metrics" in f.message for f in check_patterns.validate(path))


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


# --- #221: anonymization policy over the WHOLE patterns tree ------------------

PRIVATE_NAME = re.compile(r"dogeared|dgrd|booking-forms|windamere|duplicat-rex|safetrail",
                          re.IGNORECASE)
PATH_LIKE = re.compile(r":\d+|\.(go|ts|tsx|js|py|rb|java)\b")


def test_patterns_tree_names_no_private_repos():
    """The registry is public: no private repo/product name anywhere under
    patterns/ (manifests, specs, stack profiles, bindings, registry indexes)."""
    offenders = []
    for f in sorted((SCRIPTS.parent / "patterns").rglob("*")):
        if f.suffix not in (".json", ".md"):
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if PRIVATE_NAME.search(line):
                offenders.append(f"{f.relative_to(SCRIPTS.parent)}:{i}: {line.strip()[:80]}")
    assert offenders == [], "\n".join(offenders)


def test_all_manifest_private_provenance_is_path_free():
    """Every manifest cluster entry with non-chief-wiggum realized_as describes
    a mechanism — never a file:line or source-file path (policy applied to the
    pre-#139 mined manifests too, chief-wiggum#221)."""
    offenders = []
    for mf in sorted((SCRIPTS.parent / "patterns").glob("*/manifest.json")):
        manifest = json.loads(mf.read_text())
        for e in check_patterns.cluster_entries(manifest.get("invariants", {})):
            ra = e.get("realized_as")
            if not isinstance(ra, dict) or "chief-wiggum" in ra.get("app", ""):
                continue
            code = ra.get("code", "") + " " + ra.get("id", "")
            if PATH_LIKE.search(code) or "/" in ra.get("code", ""):
                offenders.append(f"{mf.parent.name}:{e.get('id')}: {ra}")
    assert offenders == [], "\n".join(str(o) for o in offenders)


# --- #229: platform-cost-observability ----------------------------------------

def test_platform_cost_observability_is_specified_with_honest_grounding():
    """The whole-bill spend surface: specified, mounted on the operator plane,
    one invariant reusing an in-repo cluster, a second borrowing a discipline
    without claiming realization, the rest honestly design-derived (no mined
    app surfaces its own platform spend yet)."""
    reg = json.loads((SCRIPTS.parent / "patterns" / "registry.json").read_text())
    entry = next((e for e in reg["patterns"] if e["id"] == "platform-cost-observability"), None)
    assert entry is not None, "platform-cost-observability must be a specified pattern"
    assert entry["status"] == "specified"
    assert entry.get("depends_on") == "multi-tenant-isolation"
    assert entry.get("feeds") == "improvement-loop"
    assert entry["invariants"] == "INV-PCO-001..007"

    manifest = json.loads(
        (SCRIPTS.parent / "patterns" / "platform-cost-observability" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    ids = [e["id"] for e in cluster]
    assert ids == [f"INV-PCO-00{n}" for n in range(1, 8)]
    # 001 cites the in-repo operator-plane cluster…
    grounded = {e["id"]: e["realized_as"]["code"] for e in cluster
                if isinstance(e.get("realized_as"), dict)
                and "chief-wiggum" in e["realized_as"]["app"]}
    assert set(grounded) == {"INV-PCO-001"}
    assert "multi-tenant-isolation" in grounded["INV-PCO-001"]
    # …and every other invariant is explicitly design-derived, never unmarked.
    for e in cluster:
        if e["id"] not in grounded:
            assert e.get("grounding") == "design-derived", e["id"]
    # 002 borrows the deployment-release discipline WITHOUT claiming realization
    # (INV-DRL-004 proves a deploy identity that cannot read secrets — a
    # different identity than the spend reader; codex review round 1).
    identity = next(e for e in cluster if e["id"] == "INV-PCO-002")
    assert "realized_as" not in identity
    assert "not realized by it" in identity["statement"]
    # the settling window is re-read + idempotently replaced, never frozen at
    # first write (codex review round 1 blocker)
    settling = next(e for e in cluster if e["id"] == "INV-PCO-004")
    assert "idempotently replaces" in settling["statement"]
    # …and finality is provisional: post-window corrections reopen the day
    # (codex round 2 — late-metered usage / invoice-time adjustments)
    assert "never immutability" in settling["statement"]
    # dead-man's-switch knob is per-source (codex round 2 MEDIUM)
    assert manifest["parameters"]["staleness_alert_after"]["type"] == "object"
    # completeness round: sources are disjoint (no cross-source double count),
    # a stalled ingest alerts out-of-band (dead-man's switch), and alert rungs
    # are send-once + re-armed — the three recurring failures the opus
    # completeness lens found unforbidden.
    attribution = next(e for e in cluster if e["id"] == "INV-PCO-005")
    assert "exactly once" in attribution["statement"]
    assert "superseded" in attribution["statement"]
    alerts = next(e for e in cluster if e["id"] == "INV-PCO-007")
    assert "absence of fresh data" in alerts["statement"].lower()
    assert "re-arms" in alerts["statement"]
    # the fidelity metric replaced the degenerate registration-share metric
    metric_ids = [m["id"] for m in manifest["success_metrics"]["metrics"]]
    assert "api_sourced_spend_share" in metric_ids
    assert "ledger_vs_invoice_variance" in metric_ids
    assert "spend_source_coverage" not in metric_ids
    # the whole-bill invariant names the off-cloud meters (LLM et al.)
    whole_bill = next(e for e in cluster if e["id"] == "INV-PCO-006")
    assert "LLM" in whole_bill["statement"]


def test_platform_cost_observability_gcp_binding_registered():
    """The stack binding exists, is honestly flagged aspirational, and the
    known-gaps ledger admits the surface is unbuilt."""
    stack_dir = SCRIPTS.parent / "patterns" / "stacks" / "gcp-serverless-saas"
    stack = json.loads((stack_dir / "manifest.json").read_text())
    binding = stack["bindings"].get("platform-cost-observability")
    assert binding is not None, "stack must register the platform-cost-observability binding"
    assert (stack_dir / binding["recipe"]).exists()
    assert "aspirational" in binding["source"]
    assert any("platform spend" in gap for gap in stack["known_gaps"])

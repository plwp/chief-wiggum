"""Tests for scripts/apply_pattern.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_pattern  # noqa: E402

FIXED_NOW = "2026-01-01T00:00:00+00:00"
REAL = "fetch-on-webhook-reconcile"


# --- resolution & binding ---------------------------------------------------

def test_build_plan_on_real_pattern_returns_cluster_and_unresolved():
    plan = apply_pattern.build_plan(REAL, {}, now=FIXED_NOW)
    ids = plan._adoption["invariants"]
    assert "INV-FOWR-001" in ids
    # resource/state_shape/projected_field/signing_secrets are required in the manifest
    assert set(plan.unresolved) >= {"resource", "state_shape", "projected_field", "signing_secrets"}


def test_provided_params_bind_and_reduce_unresolved():
    plan = apply_pattern.build_plan(
        REAL,
        {"resource": "subscription", "state_shape": "non-monotonic",
         "projected_field": "plan", "signing_secrets": "rotation"},
        now=FIXED_NOW)
    assert plan.bound["resource"] == "subscription"
    assert plan.unresolved == []


def test_unknown_pattern_raises():
    with pytest.raises(apply_pattern.ApplyError, match="unknown pattern"):
        apply_pattern.build_plan("nope", {}, now=FIXED_NOW)


def test_candidate_pattern_raises():
    with pytest.raises(apply_pattern.ApplyError, match="candidate"):
        apply_pattern.build_plan("reconciliation-sweep", {}, now=FIXED_NOW)  # still a candidate


def test_unknown_param_raises():
    with pytest.raises(apply_pattern.ApplyError, match="unknown parameter"):
        apply_pattern.build_plan(REAL, {"bogus": "x"}, now=FIXED_NOW)


# --- application ------------------------------------------------------------

def test_apply_writes_contract_pack_adoption_and_ratchet(tmp_path):
    plan = apply_pattern.build_plan(REAL, {"resource": "subscription"}, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=True)

    inv = tmp_path / "docs/patterns" / REAL / "invariants.md"
    assert inv.is_file()
    text = inv.read_text()
    assert "INV-FOWR-001" in text
    # reference-impl lines have balanced backticks (regression: rstrip ate the closer)
    for line in text.splitlines():
        if "_reference impl:_" in line and "`" in line:
            assert line.count("`") % 2 == 0, line

    adopted = json.loads((tmp_path / "docs/patterns/adopted.json").read_text())
    assert REAL in adopted["patterns"]
    assert adopted["patterns"][REAL]["applied_at"] == FIXED_NOW
    assert adopted["patterns"][REAL]["parameters"]["resource"] == "subscription"

    ratchet = json.loads((tmp_path / "docs/quality/ratchet.json").read_text())
    assert apply_pattern.PATTERN_GLOB in ratchet["protected_paths"]


def test_unresolved_required_params_written_as_tbd(tmp_path):
    plan = apply_pattern.build_plan(REAL, {}, now=FIXED_NOW)  # nothing bound
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    doc = (tmp_path / "docs/patterns" / REAL / "invariants.md").read_text()
    assert "TBD: bind `resource`" in doc


def test_apply_is_idempotent(tmp_path):
    for _ in range(2):
        plan = apply_pattern.build_plan(REAL, {"resource": "subscription"}, now=FIXED_NOW)
        apply_pattern.apply_plan(plan, tmp_path, write=True)
    adopted = json.loads((tmp_path / "docs/patterns/adopted.json").read_text())
    assert list(adopted["patterns"].keys()) == [REAL]
    ratchet = json.loads((tmp_path / "docs/quality/ratchet.json").read_text())
    assert ratchet["protected_paths"].count(apply_pattern.PATTERN_GLOB) == 1


def test_merge_into_existing_ratchet_preserves_and_dedupes(tmp_path):
    rp = tmp_path / "docs/quality/ratchet.json"
    rp.parent.mkdir(parents=True)
    rp.write_text(json.dumps({"suites": [{"name": "x"}], "protected_paths": ["docs/epics/**"]}))
    plan = apply_pattern.build_plan(REAL, {}, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    cfg = json.loads(rp.read_text())
    assert "docs/epics/**" in cfg["protected_paths"]          # preserved
    assert apply_pattern.PATTERN_GLOB in cfg["protected_paths"]  # added
    assert cfg["suites"] == [{"name": "x"}]                    # untouched


def test_dry_run_writes_nothing(tmp_path):
    plan = apply_pattern.build_plan(REAL, {}, now=FIXED_NOW)
    actions = apply_pattern.apply_plan(plan, tmp_path, write=False)
    assert actions  # a plan is produced
    assert not (tmp_path / "docs/patterns").exists()


def test_second_pattern_coexists_in_adopted(tmp_path):
    for pid in (REAL, "elevated-access-session"):
        plan = apply_pattern.build_plan(pid, {}, now=FIXED_NOW)
        apply_pattern.apply_plan(plan, tmp_path, write=True)
    adopted = json.loads((tmp_path / "docs/patterns/adopted.json").read_text())
    assert set(adopted["patterns"]) == {REAL, "elevated-access-session"}


# --- list-adopted (the /architect fold-in seam) -----------------------------

def test_list_adopted_empty_when_no_adoption(tmp_path):
    assert apply_pattern.list_adopted(tmp_path) == []


def test_list_adopted_returns_fresh_clusters(tmp_path):
    for pid in (REAL, "elevated-access-session"):
        apply_pattern.apply_plan(apply_pattern.build_plan(pid, {}, now=FIXED_NOW), tmp_path, write=True)
    adopted = {a["id"]: a for a in apply_pattern.list_adopted(tmp_path)}
    assert set(adopted) == {REAL, "elevated-access-session"}
    fowr_ids = [i["id"] for i in adopted[REAL]["invariants"]]
    assert "INV-FOWR-001" in fowr_ids
    # statements come fresh from the registry manifest, not the stamped copy
    assert all(i["statement"] for i in adopted[REAL]["invariants"])
    assert adopted[REAL]["contract_pack"] == f"docs/patterns/{REAL}/invariants.md"


def test_cli_list_adopted(tmp_path):
    apply_pattern.apply_plan(apply_pattern.build_plan(REAL, {}, now=FIXED_NOW), tmp_path, write=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "apply_pattern.py"),
         "--target-dir", str(tmp_path), "--list-adopted", "--format", "json"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out[0]["id"] == REAL and any(i["id"] == "INV-FOWR-001" for i in out[0]["invariants"])


# --- catalog (the /seed selection seam) -------------------------------------

def test_catalog_lists_specified_with_applies_when():
    items = {c["id"]: c for c in apply_pattern.catalog()}
    assert REAL in items
    assert items[REAL]["status"] == "specified"
    assert items[REAL]["applies_when"]  # non-empty selection criteria from the manifest
    # candidates are listed too, flagged
    assert any(c["status"] == "candidate" for c in apply_pattern.catalog())


def test_cli_catalog_needs_no_target(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "apply_pattern.py"), "--catalog", "--format", "json"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ids = {c["id"] for c in json.loads(proc.stdout)}
    assert {"multi-tenant-isolation", "tiered-subscription"} <= ids


def test_cli_apply_requires_target_dir():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "apply_pattern.py"), REAL],
        capture_output=True, text=True)
    assert proc.returncode == 2
    assert "target-dir" in proc.stderr


# --- CLI --------------------------------------------------------------------

def test_cli_dry_run(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "apply_pattern.py"), REAL,
         "--target-dir", str(tmp_path), "--dry-run", "--format", "json"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["pattern"] == REAL and out["dry_run"] is True
    assert not (tmp_path / "docs").exists()


# --- scaffold stamping (#135) -----------------------------------------------

MTI = "multi-tenant-isolation"
MTI_PARAMS = {"tenant_key": "provider_id", "resolver": "claim",
              "store": "firestore", "operator_routes": "/admin"}
MTI_TARGETS = ("internal/tenant/resolver.go", "internal/tenant/scoped_repo.go")


def test_scaffold_renders_into_plan_when_required_params_bound():
    plan = apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    assert set(plan.scaffold_files) == set(MTI_TARGETS)
    body = plan.scaffold_files["internal/tenant/scoped_repo.go"]
    # placeholders are bound, none survive
    assert "provider_id" in body and "firestore" in body
    assert "{{" not in body
    assert not plan.scaffold_skipped


def test_scaffold_skipped_when_required_param_unresolved():
    plan = apply_pattern.build_plan(MTI, {"tenant_key": "provider_id"}, now=FIXED_NOW)
    assert plan.scaffold_files == {}
    assert "scaffold not stamped" in plan.scaffold_skipped
    # contract pack still installs regardless
    assert f"docs/patterns/{MTI}/invariants.md" in plan.files


def test_scaffold_stamps_to_target_paths(tmp_path):
    plan = apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    for rel in MTI_TARGETS:
        assert (tmp_path / rel).is_file(), rel
    assert "package tenant" in (tmp_path / "internal/tenant/resolver.go").read_text()


def test_scaffold_is_idempotent_and_never_clobbers(tmp_path):
    plan = apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    edited = tmp_path / MTI_TARGETS[0]
    edited.write_text("// hand-edited\npackage tenant\n")
    # second apply (no force) must skip, preserving the hand edit
    actions = apply_pattern.apply_plan(
        apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW), tmp_path, write=True)
    assert any("skip scaffold" in a for a in actions)
    assert edited.read_text() == "// hand-edited\npackage tenant\n"


def test_scaffold_force_re_stamps(tmp_path):
    plan = apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    edited = tmp_path / MTI_TARGETS[0]
    edited.write_text("// hand-edited\n")
    actions = apply_pattern.apply_plan(
        apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW), tmp_path, write=True, force=True)
    assert any("re-stamp scaffold" in a for a in actions)
    assert "package tenant" in edited.read_text()


def test_scaffold_dry_run_writes_nothing(tmp_path):
    plan = apply_pattern.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    apply_pattern.apply_plan(plan, tmp_path, write=False)
    assert not (tmp_path / "internal").exists()


def test_cli_apply_stamps_scaffold(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "apply_pattern.py"), MTI,
         "--target-dir", str(tmp_path), "--now", FIXED_NOW,
         "--param", "tenant_key=provider_id", "--param", "resolver=claim",
         "--param", "store=firestore", "--param", "operator_routes=/admin"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "internal/tenant/resolver.go").is_file()


def test_scaffold_rejects_path_traversal_target(tmp_path, monkeypatch):
    # a pattern/param that renders a target escaping the repo must fail closed
    import apply_pattern as ap
    orig = ap.load_scaffold
    monkeypatch.setattr(ap, "load_scaffold", lambda pid, base=ap.ROOT: {
        "files": [{"template": "tenant_resolver.go.tmpl", "target": "../evil.go"}]})
    with pytest.raises(ap.ApplyError, match="repo-relative path"):
        ap.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)
    monkeypatch.setattr(ap, "load_scaffold", orig)


def test_scaffold_rejects_absolute_target(tmp_path, monkeypatch):
    import apply_pattern as ap
    monkeypatch.setattr(ap, "load_scaffold", lambda pid, base=ap.ROOT: {
        "files": [{"template": "tenant_resolver.go.tmpl", "target": "/etc/evil.go"}]})
    with pytest.raises(ap.ApplyError, match="repo-relative path"):
        ap.build_plan(MTI, MTI_PARAMS, now=FIXED_NOW)


def test_scaffold_fails_closed_on_unbound_body_placeholder(tmp_path, monkeypatch):
    import apply_pattern as ap
    # a template body referencing an unbound param must fail, not leak {{param}}
    (tmp_path / "s").mkdir()
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    pdir = tmp_path / "patterns" / MTI / "scaffold"
    pdir.mkdir(parents=True)
    (pdir / "t.tmpl").write_text("package x // {{unbound_param}}\n")
    (pdir / "scaffold.json").write_text(json.dumps(
        {"files": [{"template": "t.tmpl", "target": "x.go"}]}))
    with pytest.raises(ap.ApplyError, match="unbound param"):
        ap._render_scaffold(MTI, ap.load_scaffold(MTI, base=tmp_path), {}, base=tmp_path)


@pytest.mark.parametrize("bad,msg", [
    ("[]", "must be a JSON object"),
    ('{"files": {}}', "non-empty 'files'"),
    ('{"files": []}', "non-empty 'files'"),
    ('{"files": ["x"]}', "'template'\\+'target'"),
    ('{"files": [{"template": "t.tmpl"}]}', "'template'\\+'target'"),
])
def test_malformed_scaffold_manifest_is_clean_apply_error(tmp_path, bad, msg):
    import apply_pattern as ap
    pdir = tmp_path / "patterns" / MTI / "scaffold"
    pdir.mkdir(parents=True)
    (pdir / "scaffold.json").write_text(bad)
    with pytest.raises(ap.ApplyError, match=msg):
        ap.load_scaffold(MTI, base=tmp_path)

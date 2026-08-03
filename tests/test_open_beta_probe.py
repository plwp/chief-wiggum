"""Tests for the `open-beta-probe` pattern (chief-wiggum#256) and the
landing-page-smoke-test recalibration it motivates.

"Open beta is the new waitlist": build cost collapsed and the smoke-test signal
degraded, so `open-beta-probe` (strength 3-4, signup-code metered) is now often the
better first instrument than `landing-page-smoke-test` (strength 2). The mandatory
blast-radius declaration (INV-OBP-004) is the mechanical heart of the pattern: stamping
is refused without it, and an unbounded declaration forces compute-only/draft-only mode.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_pattern  # noqa: E402
import check_patterns  # noqa: E402

FIXED_NOW = "2026-01-01T00:00:00+00:00"
PATTERNS_DIR = Path(__file__).resolve().parent.parent / "patterns"


# ---- registry / invariant-cluster validity -------------------------------------

def test_open_beta_probe_is_registered_and_specified():
    reg = json.loads((PATTERNS_DIR / "registry.json").read_text())
    entry = next(e for e in reg["patterns"] if e["id"] == "open-beta-probe")
    assert entry["status"] == "specified"
    assert entry["category"] == "validation-experiment"
    assert entry["trust_class"] == "end-user-signal-driven"
    assert entry["invariants"] == "INV-OBP-001..006"


def test_open_beta_probe_manifest_cluster_is_well_formed():
    manifest = json.loads((PATTERNS_DIR / "open-beta-probe" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    assert [e["id"] for e in cluster] == [f"INV-OBP-00{i}" for i in range(1, 7)]
    assert manifest["success_metrics"]["metrics"]
    grounded = {e["id"] for e in cluster
                if isinstance(e.get("realized_as"), dict)
                and "chief-wiggum" in e["realized_as"]["app"]}
    assert grounded == {"INV-OBP-001", "INV-OBP-002"}
    for e in cluster:
        if e["id"] not in grounded:
            assert e.get("grounding") == "design-derived"


def test_check_patterns_script_is_clean_with_the_new_pattern():
    findings = check_patterns.validate(PATTERNS_DIR / "registry.json")
    errors = [f for f in findings if f.severity == check_patterns.ERROR]
    assert errors == [], "\n".join(str(f) for f in errors)


def test_blast_radius_invariant_names_compute_only_mode():
    manifest = json.loads((PATTERNS_DIR / "open-beta-probe" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    entry = next(e for e in cluster if e["id"] == "INV-OBP-004")
    stmt = entry["statement"].lower()
    assert "blast radius" in stmt or "blast_radius" in stmt.replace("-", "_")
    assert "compute-only" in stmt or "compute only" in stmt
    assert "mandatory" in stmt or "refuses" in stmt


def test_signup_code_metering_invariant_present():
    manifest = json.loads((PATTERNS_DIR / "open-beta-probe" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    entry = next(e for e in cluster if e["id"] == "INV-OBP-003")
    assert "code" in entry["statement"].lower()
    assert "revocable" in entry["statement"].lower() or "trackable" in entry["statement"].lower()


def test_blast_radius_is_a_required_parameter_with_no_default():
    manifest = json.loads((PATTERNS_DIR / "open-beta-probe" / "manifest.json").read_text())
    spec = manifest["parameters"]["blast_radius"]
    assert spec["required"] is True
    assert "default" not in spec
    assert set(spec["enum"]) == {"bounded", "unbounded"}


# ---- scaffold stamping ----------------------------------------------------------

def test_open_beta_probe_scaffold_skipped_without_blast_radius():
    """INV-OBP-004: stamping is refused (scaffold skipped) without blast_radius bound —
    the contract pack still installs (apply_pattern.py's standard missing-required-param
    behavior), but the scaffold itself does not render."""
    plan = apply_pattern.build_plan(
        "open-beta-probe", {"product_name": "Kennel Ledger"}, now=FIXED_NOW)
    assert plan.scaffold_files == {}
    assert "scaffold not stamped" in plan.scaffold_skipped
    assert "docs/patterns/open-beta-probe/invariants.md" in plan.files


def test_open_beta_probe_scaffold_stamps_with_bounded_blast_radius(tmp_path):
    plan = apply_pattern.build_plan(
        "open-beta-probe",
        {"product_name": "Kennel Ledger", "blast_radius": "bounded"},
        now=FIXED_NOW,
    )
    assert plan.unresolved == []
    targets = set(plan.scaffold_files)
    assert targets == {
        "experiments/open-beta-probe/blast_radius.json",
        "experiments/open-beta-probe/signup_codes.py",
        "experiments/open-beta-probe/README.md",
    }
    declaration = plan.scaffold_files["experiments/open-beta-probe/blast_radius.json"]
    assert '"blast_radius": "bounded"' in declaration
    assert "{{" not in declaration
    apply_pattern.apply_plan(plan, tmp_path, write=True)
    for rel in targets:
        assert (tmp_path / rel).is_file(), rel


def test_open_beta_probe_scaffold_stamps_with_unbounded_blast_radius_names_compute_only():
    plan = apply_pattern.build_plan(
        "open-beta-probe",
        {"product_name": "Rostering Bridge", "blast_radius": "unbounded"},
        now=FIXED_NOW,
    )
    assert plan.unresolved == []
    declaration = plan.scaffold_files["experiments/open-beta-probe/blast_radius.json"]
    assert '"blast_radius": "unbounded"' in declaration
    assert "compute-only" in declaration.lower()


def test_open_beta_probe_readme_states_the_sequencing_rule(tmp_path):
    plan = apply_pattern.build_plan(
        "open-beta-probe",
        {"product_name": "Kennel Ledger", "blast_radius": "bounded"},
        now=FIXED_NOW,
    )
    readme = plan.scaffold_files["experiments/open-beta-probe/README.md"]
    assert "assumption.py card" in readme
    assert "single-use" in readme.lower()
    assert "sequencing" in readme.lower()


def test_signup_codes_stub_names_returning_user_evidence_floor():
    plan = apply_pattern.build_plan(
        "open-beta-probe",
        {"product_name": "Kennel Ledger", "blast_radius": "bounded"},
        now=FIXED_NOW,
    )
    stub = plan.scaffold_files["experiments/open-beta-probe/signup_codes.py"]
    assert "did_real_work" in stub
    assert "returning_user_rate_pct" in stub


# ---- landing-page-smoke-test recalibration --------------------------------------

def test_landing_page_smoke_test_manifest_carries_degradation_note():
    manifest = json.loads(
        (PATTERNS_DIR / "landing-page-smoke-test" / "manifest.json").read_text())
    assert "DEGRADED" in manifest["$comment"]
    assert "open-beta-probe" in manifest["$comment"]
    assert any("NARROWED" in a for a in manifest["applies_when"])


def test_landing_page_smoke_test_pattern_md_references_open_beta_probe():
    text = (PATTERNS_DIR / "landing-page-smoke-test" / "pattern.md").read_text()
    assert "open-beta-probe" in text
    assert "Degradation note" in text or "degradation note" in text.lower()


def test_landing_page_smoke_test_still_passes_check_patterns():
    """The recalibration must not break the invariant-cluster model — this pattern
    keeps all 7 of its invariants (INV-LPS-001..007), just a narrower applies_when."""
    manifest = json.loads(
        (PATTERNS_DIR / "landing-page-smoke-test" / "manifest.json").read_text())
    cluster = check_patterns.cluster_entries(manifest["invariants"])
    assert [e["id"] for e in cluster] == [f"INV-LPS-00{i}" for i in range(1, 8)]

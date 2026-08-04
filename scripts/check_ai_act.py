#!/usr/bin/env python3
"""EU AI Act classification + in-force-layer gate for a target repo (chief-wiggum#316).

CW's only AI-regulatory hook before this ticket was one line in
`templates/compliance-requirements.md` behind the WRONG trigger — it fired on
regulated/sensitive *data*, which is not what the Act keys on. The Act triggers
on **AI functionality and where the output lands**, so a product with a
conversational agent and zero regulated data got zero AI Act treatment. This
gate checks `docs/compliance/ai-act.json` (schema: `templates/ai-act-schema.json`),
a standalone artifact produced by `/architect`, independent of
`docs/compliance-requirements.md`.

Scope — the IN-FORCE layer only (Decision 2, #316):

- **Art. 5 prohibitions** (in force since 2 Feb 2025) — a `tier: prohibited`
  feature is a hard-stop finding.
- **Art. 6(3)/(4) derogation documentation** — a `tier: high_risk_annex_iii`
  feature claiming the Art. 6(3) derogation must NAME which of the four
  conditions it relies on (Art. 6(4): the assessment must exist BEFORE market
  placement, or the claim cannot be made later). `performs_profiling: true`
  ALWAYS voids the derogation regardless of the claimed tier.
- **Art. 50 transparency** (in force since 2 Aug 2026) — a
  `tier: transparency_art50` feature must cite an Art. 50 obligation and carry
  at least one `evidence[]` handle.

**Out of scope, parked** (Decision 2): the Chapter III high-risk conformity
pack (Arts. 8-17, 43, 47-49, 72-73) — deferred by the Digital Omnibus to
2 Dec 2027 (standalone Annex III) / 2 Aug 2028 (Annex I embedded), and the
harmonised standards those obligations resolve against do not exist yet.

**The Art. 6(4) point, mechanized**: absence of a classification must never
read as a classification of "not high risk". `classification_status` is
`"missing"` (no `docs/compliance/ai-act.json` at all — an unmade legal claim,
`outcome=findings`) or `"recorded"` (the file exists; an empty `features: []`
is a genuine, explicit "no AI features here", `outcome=inapplicable`).

**Authority boundary** (stated once, not re-asserted per finding): this gate
checks that a disclosure obligation is DECLARED and an assessment EXISTS and
NAMES its condition — never that the disclosure is adequate to a "reasonably
well-informed" person, never that the derogation reasoning is legally sound.
It does not touch conformity assessment, CE marking, EU-database registration,
or post-market monitoring. Everything past that line is a `legal_signoff`
`TBD:` in the artifact itself.

**Known limitation (ships report-only, per docs/gate-rollout.md item 3)**:
this v1 validates the artifact's OWN declared fields for internal consistency
and Art. 5/6/50 completeness. It does NOT yet cross-check a `transparency_art50`
feature's `evidence[]` against `code_query.py`-scanned model-call sites, or a
`ui-spec.json` surface's first-interaction reachability — that deeper scan is
follow-up work once this ships and is dry-run against a real target.

Report-only by default (prints findings, exits 0). `--gate` makes it block
(exit 1 on any `fail`-severity finding), the way every CW gate graduates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts as _meta_location  # noqa: E402 - meta-location resolver (#213)

ARTIFACT_RELATIVE_PATH = Path("compliance") / "ai-act.json"

FEATURE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9]+-\d{3}$")

VALID_ROLES = {"provider", "deployer", "both"}
VALID_TIERS = {
    "prohibited", "high_risk_annex_i", "high_risk_annex_iii", "transparency_art50", "minimal",
}
VALID_DEROGATION_CONDITIONS = {
    "narrow_procedural_task",
    "improves_completed_human_activity",
    "detects_decision_patterns_without_replacing_human_review",
    "preparatory_task",
}


@dataclass
class Finding:
    severity: str  # "fail" | "warn"
    rule: str
    feature_id: str | None
    message: str

    def __str__(self) -> str:
        fid = f" {self.feature_id}" if self.feature_id else ""
        return f"  [{self.severity}] [{self.rule}]{fid}: {self.message}"


@dataclass
class Report:
    classification_status: str  # "missing" | "recorded"
    findings: list[Finding] = field(default_factory=list)
    n_features: int = 0
    n_prohibited: int = 0
    n_high_risk_annex_iii: int = 0
    n_transparency: int = 0
    unparsed_reason: str | None = None
    artifact_path: str = ""

    @property
    def outcome(self) -> str:
        """The standard four-state gate outcome (#289), plus the Art. 6(4)
        distinction carried separately in ``classification_status`` — a
        missing artifact is ``findings``, never a silent ``pass``."""
        if self.unparsed_reason:
            return "error"
        if self.classification_status == "missing":
            return "findings"
        if not self.n_features:
            return "inapplicable"
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        return {
            "features_declared": self.n_features,
            "prohibited_tier": self.n_prohibited,
            "high_risk_annex_iii_tier": self.n_high_risk_annex_iii,
            "transparency_art50_tier": self.n_transparency,
        }

    def to_dict(self) -> dict:
        return {
            "classification_status": self.classification_status,
            "outcome": self.outcome,
            "measured": self.measured,
            "artifact_path": self.artifact_path,
            "unparsed_reason": self.unparsed_reason,
            "findings": [asdict(f) for f in self.findings],
        }


def artifact_path(target_repo: Path) -> Path:
    resolver = _meta_location.Resolver.resolve(target_repo)
    return resolver.meta_root / ARTIFACT_RELATIVE_PATH


def _empty_report(path: Path, *, unparsed_reason: str | None = None) -> Report:
    return Report(
        classification_status="missing",
        artifact_path=str(path),
        unparsed_reason=unparsed_reason,
    )


def _check_feature(feat: object) -> tuple[list[Finding], str | None, str | None]:
    """Return (findings, tier-or-None, feature_id-or-None) for one feature entry."""
    if not isinstance(feat, dict):
        return [Finding("fail", "malformed_feature", None, f"feature entry is not an object: {feat!r}")], None, None

    findings: list[Finding] = []
    fid = feat.get("feature_id")
    if not isinstance(fid, str) or not FEATURE_ID_RE.match(fid):
        findings.append(Finding("fail", "malformed_feature_id", fid if isinstance(fid, str) else None,
            "feature_id missing or not the stable KIND-SLUG-NNN shape"))

    role = feat.get("role")
    if role not in VALID_ROLES:
        findings.append(Finding("fail", "invalid_role", fid,
            f"role {role!r} not in {sorted(VALID_ROLES)}"))

    tier = feat.get("tier")
    if tier not in VALID_TIERS:
        findings.append(Finding("fail", "invalid_tier", fid,
            f"tier {tier!r} not in {sorted(VALID_TIERS)}"))
        return findings, None, fid

    performs_profiling = feat.get("performs_profiling")
    if not isinstance(performs_profiling, bool):
        findings.append(Finding("fail", "missing_profiling_flag", fid,
            "performs_profiling must be an explicit boolean — profiling is ALWAYS "
            "high-risk (Art. 6(3)) and voids any derogation, so this can't be left implicit"))
    elif performs_profiling and tier == "minimal":
        findings.append(Finding("fail", "profiling_misclassified", fid,
            "performs_profiling=true but tier=minimal — profiling is always high-risk; this tier cannot stand"))

    if tier == "prohibited":
        findings.append(Finding("fail", "art5_prohibited_practice", fid,
            "tier=prohibited — Art. 5 prohibited-practice screen hit; this is a hard "
            "stop to route the feature away from, not a design choice to document around"))

    if tier == "high_risk_annex_iii":
        area = feat.get("annex_iii_area")
        if not isinstance(area, int) or not (1 <= area <= 8):
            findings.append(Finding("fail", "annex_iii_area_missing", fid,
                "tier=high_risk_annex_iii requires annex_iii_area in 1..8"))
        derogation = feat.get("derogation_assessment")
        condition = derogation.get("condition") if isinstance(derogation, dict) else None
        if condition not in VALID_DEROGATION_CONDITIONS:
            findings.append(Finding("fail", "annex_iii_undocumented_assessment", fid,
                "Art. 6(4): a provider claiming an Annex III system is NOT high-risk "
                "must document that assessment BEFORE market placement — no condition "
                "named here means the claim cannot be made"))
    elif feat.get("annex_iii_area") is not None:
        findings.append(Finding("warn", "annex_iii_area_on_wrong_tier", fid,
            "annex_iii_area is set but tier is not high_risk_annex_iii"))

    if tier == "transparency_art50":
        obligations = feat.get("obligations") or []
        if not isinstance(obligations, list) or not any("50" in str(o) for o in obligations):
            findings.append(Finding("fail", "art50_obligation_undeclared", fid,
                "tier=transparency_art50 but obligations[] cites no Art. 50 reference"))
        evidence = feat.get("evidence") or []
        if not isinstance(evidence, list) or not evidence:
            findings.append(Finding("warn", "art50_no_evidence", fid,
                "tier=transparency_art50 with no evidence[] @cw-trace handle — the "
                "obligation is declared but not yet shown wired"))

    return findings, tier, fid


def load(target_repo: Path) -> Report:
    path = artifact_path(target_repo)
    if not path.is_file():
        return _empty_report(path)

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return _empty_report(path, unparsed_reason=f"{type(exc).__name__}: {exc}")

    if not isinstance(data, dict):
        return _empty_report(path, unparsed_reason="ai-act.json is not a JSON object")

    features = data.get("features")
    if not isinstance(features, list):
        return _empty_report(path, unparsed_reason="ai-act.json has no 'features' array")

    all_findings: list[Finding] = []
    n_prohibited = n_high_risk = n_transparency = 0
    for feat in features:
        findings, tier, _fid = _check_feature(feat)
        all_findings.extend(findings)
        if tier == "prohibited":
            n_prohibited += 1
        elif tier == "high_risk_annex_iii":
            n_high_risk += 1
        elif tier == "transparency_art50":
            n_transparency += 1

    eu_scope = data.get("eu_scope")
    if eu_scope not in ("in_scope", "out_of_scope", "TBD"):
        all_findings.append(Finding("fail", "eu_scope_undeclared", None,
            "top-level eu_scope must be one of in_scope|out_of_scope|TBD — never a silent absence"))

    return Report(
        classification_status="recorded",
        findings=all_findings,
        n_features=len(features),
        n_prohibited=n_prohibited,
        n_high_risk_annex_iii=n_high_risk,
        n_transparency=n_transparency,
        artifact_path=str(path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="Target repo checkout to scan")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true", help="Exit 1 on any 'fail'-severity finding (blocking mode)")
    args = parser.parse_args(argv)

    report = load(Path(args.repo))
    fail_count = sum(1 for f in report.findings if f.severity == "fail")

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"Measured: {report.measured}")
        print(f"Classification status: {report.classification_status} "
              f"(artifact: {report.artifact_path})")
        if report.unparsed_reason:
            print(f"\nERROR: {report.artifact_path} exists and could not be read — "
                  f"{report.unparsed_reason} (its declarations are unknown, not a clean result)")
        elif report.classification_status == "missing":
            print("\nFINDINGS: no docs/compliance/ai-act.json found — classification "
                  "never produced. Art. 6(4): the assessment must exist BEFORE market "
                  "placement, not be reconstructed after. This is distinct from a "
                  "recorded 'no AI features' — that would be classification_status=recorded "
                  "with an empty features[] and outcome=inapplicable.")
        elif report.outcome == "inapplicable":
            print("\nINAPPLICABLE: ai-act.json recorded with an explicit empty features[] "
                  "— no AI functionality to screen.")
        elif not report.findings:
            print("\nOK: all declared features pass the Art. 5/6/50 in-force screen.")
        else:
            print(f"\n{len(report.findings)} finding(s) ({fail_count} fail, "
                  f"{len(report.findings) - fail_count} warn):\n")
            for f in report.findings:
                print(f)

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        from factory_log import emit_gate
        emit_gate("check_ai_act", "fail" if (fail_count or report.unparsed_reason
                  or report.classification_status == "missing") else "pass",
                  caught=fail_count, repo=str(args.repo))
    except Exception:
        pass

    if not args.gate:
        return 0
    blocking = fail_count > 0 or bool(report.unparsed_reason) or report.classification_status == "missing"
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Gate-of-gates: enforce the gate-validation protocol (docs/gate-validation.md, #168).

`docs/gate-rollout.md` says a gate ships report-only and is "validated on a
real, already-shipped repo before it is wired as a blocker" — but that rule was
prose, checked by convention. This script makes it mechanical: it loads a
per-gate ``validation/<gate>.json`` record (schema:
``templates/gate-validation-record-schema.json``) and reports whether that
gate has EARNED the right to run in blocking (``--gate``) mode.

A record passes only when:

1. It validates against the schema (well-formed authority boundary, at least
   one seeded-defect trial, at least one clean-corpus run).
2. Every seeded-defect trial has ``passed: true`` (``result == expected`` —
   an ``expected: "no-fire"`` trial passing does NOT mean the gate caught
   nothing useful; it means a documented scope boundary held).
3. The MANDATORY evasion seed classes are present and passed:
   ``evasion-omission``, ``evasion-config-indirection``,
   ``evasion-sampling-gap`` always; ``evasion-concurrency`` unless the record
   declares ``concurrency_applicable: false`` (with a ``concurrency_note``);
   ``instrumentation-deleted`` when ``telemetry_dependent: true``.
4. Every clean-corpus run has ``passed: true`` AND non-empty, not-all-zero
   ``coverage`` — a clean run that proves nothing was exercised is not
   evidence.
5. The record's own ``status`` field is ``"passed"``.

Report-only by default (prints the verdict, exits 0). ``--gate`` makes it
block — this is the mode ``/close-epic`` runs before it will pass ``--gate
coverage`` through to ``check_traceability.py`` / ``check_single_writer.py``
(or any other checker that adopts this protocol).

Exit codes: 0 = ok (or report-only), 1 = gate violation (missing/failing
record) under ``--gate``, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "gate-validation-record-schema.json"
DEFAULT_VALIDATION_DIR = "docs/quality/validation"

# Evasion seed classes every gate's record must attempt. Concurrency is
# conditional (mandated unless the record declares it inapplicable);
# instrumentation-deleted is conditional on telemetry_dependent.
ALWAYS_MANDATORY_EVASIONS = ("evasion-omission", "evasion-config-indirection", "evasion-sampling-gap")
CONCURRENCY_CLASS = "evasion-concurrency"
INSTRUMENTATION_CLASS = "instrumentation-deleted"


@dataclass
class GateValidationReport:
    gate: str
    validation_dir: str
    record_found: bool = False
    schema_errors: list[str] = field(default_factory=list)
    missing_seed_classes: list[str] = field(default_factory=list)
    failed_trials: list[dict] = field(default_factory=list)
    failed_clean_runs: list[dict] = field(default_factory=list)
    status_field: str | None = None
    record: dict | None = None

    @property
    def passing(self) -> bool:
        return (
            self.record_found
            and not self.schema_errors
            and not self.missing_seed_classes
            and not self.failed_trials
            and not self.failed_clean_runs
            and self.status_field == "passed"
        )

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "validation_dir": self.validation_dir,
            "record_found": self.record_found,
            "passing": self.passing,
            "schema_errors": self.schema_errors,
            "missing_seed_classes": self.missing_seed_classes,
            "failed_trials": self.failed_trials,
            "failed_clean_runs": self.failed_clean_runs,
            "status_field": self.status_field,
        }


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def load_record(gate: str, validation_dir: str | Path) -> dict | None:
    path = Path(validation_dir) / f"{gate}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _schema_errors(record: dict, schema: dict) -> list[str]:
    try:
        import jsonschema  # noqa: PLC0415
    except ImportError:  # pragma: no cover - jsonschema is a project dependency
        return []
    validator = jsonschema.Draft7Validator(schema)
    return [e.message for e in validator.iter_errors(record)]


def _has_nonzero_coverage(coverage: object) -> bool:
    """True if `coverage` is a non-empty dict carrying at least one truthy/positive value."""
    if not isinstance(coverage, dict) or not coverage:
        return False
    for v in coverage.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False


def required_seed_classes(record: dict) -> list[str]:
    """The evasion/instrumentation seed classes THIS record must carry, given its
    own telemetry_dependent / concurrency_applicable declarations."""
    required = list(ALWAYS_MANDATORY_EVASIONS)
    if record.get("concurrency_applicable", True):
        required.append(CONCURRENCY_CLASS)
    if record.get("telemetry_dependent", False):
        required.append(INSTRUMENTATION_CLASS)
    return required


def check(gate: str, validation_dir: str | Path, schema: dict | None = None) -> GateValidationReport:
    schema = schema or load_schema()
    report = GateValidationReport(gate=gate, validation_dir=str(validation_dir))
    record = load_record(gate, validation_dir)
    if record is None:
        report.schema_errors = [f"no validation record found at {Path(validation_dir) / (gate + '.json')}"]
        return report
    report.record_found = True
    report.record = record

    errs = _schema_errors(record, schema)
    if errs:
        report.schema_errors = errs
        # Malformed records can't be trusted for the finer-grained checks below.
        return report

    trials = record.get("seeded_defect_trials", []) or []
    passed_classes = {t.get("seed_class") for t in trials if t.get("passed")}
    report.missing_seed_classes = [c for c in required_seed_classes(record) if c not in passed_classes]
    report.failed_trials = [t for t in trials if not t.get("passed")]

    clean_runs = record.get("clean_corpus_runs", []) or []
    report.failed_clean_runs = [
        r for r in clean_runs if not r.get("passed") or not _has_nonzero_coverage(r.get("coverage"))
    ]

    report.status_field = record.get("status")
    return report


def render_text(report: GateValidationReport) -> str:
    lines = [
        f"# Gate Validation — {report.gate}",
        "",
        f"Record: {'found' if report.record_found else 'MISSING'} ({report.validation_dir}/{report.gate}.json)",
        f"Verdict: {'PASSING' if report.passing else 'NOT VALIDATED'}",
    ]
    if report.schema_errors:
        lines += ["", "## Schema/record errors", ""] + [f"- {e}" for e in report.schema_errors]
    if report.missing_seed_classes:
        lines += ["", "## Missing mandatory seed classes", ""] + [f"- {c}" for c in report.missing_seed_classes]
    if report.failed_trials:
        lines += ["", "## Failed seeded-defect trials", ""]
        lines += [f"- {t.get('seed_id', '?')} ({t.get('seed_class', '?')}): "
                   f"expected={t.get('expected')} result={t.get('result')}" for t in report.failed_trials]
    if report.failed_clean_runs:
        lines += ["", "## Failed/unproven clean-corpus runs", ""]
        lines += [f"- {r.get('repo', '?')}@{r.get('sha', '?')}: passed={r.get('passed')} "
                   f"coverage={r.get('coverage')}" for r in report.failed_clean_runs]
    if report.record_found and not report.schema_errors:
        lines += ["", f"Record status field: {report.status_field}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate-of-gates: does this gate have a passing gate-validation record?",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("gate", help="Gate name, e.g. check_single_writer")
    parser.add_argument(
        "--validation-dir", default=DEFAULT_VALIDATION_DIR,
        help=f"Directory containing <gate>.json validation records (default: {DEFAULT_VALIDATION_DIR})",
    )
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--gate", dest="gate_mode", action="store_true",
                        help="Fail (exit 1) when the named gate lacks a passing validation record")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load gate-validation schema: {exc}", file=sys.stderr)
        return 2

    report = check(args.gate, args.validation_dir, schema=schema)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        from factory_log import emit_gate  # noqa: PLC0415
        caught = 0 if report.passing else 1
        emit_gate("check_gate_validation", "fail" if caught else "pass", caught=caught)
    except Exception:
        pass

    if args.gate_mode and not report.passing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

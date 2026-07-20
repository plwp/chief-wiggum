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
2. Its PROVENANCE holds — the record cannot simply be copied, forged, or left
   to go stale:
   - the record's ``gate`` field names the gate being checked (a record copied
     from another gate's file grants nothing);
   - its ``scanner_version`` matches the gate's LIVE ``--scanner-version``
     output (when the gate script supports it) — a record authored against an
     older scanner is stale and must be re-run;
   - its ``ratchet_record_id`` is corroborated by the ratchet journal sitting
     beside the validation dir (``<validation-dir>/../ratchet-journal.jsonl``):
     the id must exist in a hash-chain-verified journal, in a
     ``gate-validation`` event whose ``ref`` names this gate. The chain is the
     tamper-evidence (docs/ratchet.md); a record without a journaled,
     chain-verified entry has no provenance.
3. Every seeded-defect trial's outcome is DERIVED, not trusted: a trial passes
   iff ``result`` matches ``expected`` (``fire``→``fired``,
   ``no-fire``→``not-fired``) AND its own ``passed`` flag agrees. A forged
   ``passed: true`` on a trial whose result contradicts its expectation fails
   the record.
4. The MANDATORY seed classes are present with genuinely-passing trials:
   ``direct`` always (the protocol's sanity check); ``evasion-omission``,
   ``evasion-config-indirection``, ``evasion-sampling-gap`` always;
   ``evasion-concurrency`` unless the record declares
   ``concurrency_applicable: false`` (with a ``concurrency_note``);
   ``instrumentation-deleted`` when ``telemetry_dependent: true``.
5. Every clean-corpus run is derived too: it needs ``passed: true`` AND
   ``findings: 0`` AND non-empty, not-all-zero ``coverage`` — "no findings"
   with nothing exercised (or with findings quietly non-zero) is not evidence.
6. The record's own ``status`` field is ``"passed"``.

Report-only by default (prints the verdict, exits 0). ``--gate`` makes it
block — this is the mode ``/close-epic`` runs before it will pass ``--gate
coverage`` through to ``check_traceability.py`` / ``check_single_writer.py``
(or any other checker that adopts this protocol).

Exit codes: 0 = ok (or report-only), 1 = gate violation (missing/failing
record) under ``--gate``, 2 = usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import stable_hash  # noqa: E402

# Single definition site (INV-fh-004): this used to be a second, independently
# spelled DEFAULT_VALIDATION_DIR here (a relative string) that happened to
# differ in form from factory_log.py's (an absolute path) — imported, not
# redefined, so the two can never silently diverge again.
# @cw-trace guards INV-fh-004
from factory_log import DEFAULT_VALIDATION_DIR  # noqa: E402

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "gate-validation-record-schema.json"
JOURNAL_NAME = "ratchet-journal.jsonl"

# Seed classes every gate's record must carry with genuinely-passing trials.
# `direct` is the protocol's sanity check (docs/gate-validation.md); the
# evasion trio is unconditional; concurrency is mandated unless the record
# declares it inapplicable; instrumentation-deleted is conditional on
# telemetry_dependent.
DIRECT_CLASS = "direct"
ALWAYS_MANDATORY_EVASIONS = ("evasion-omission", "evasion-config-indirection", "evasion-sampling-gap")
CONCURRENCY_CLASS = "evasion-concurrency"
INSTRUMENTATION_CLASS = "instrumentation-deleted"

EXPECTED_TO_RESULT = {"fire": "fired", "no-fire": "not-fired"}

RID_RE = re.compile(r"rec-\d+")


@dataclass
class GateValidationReport:
    gate: str
    validation_dir: str
    record_found: bool = False
    schema_errors: list[str] = field(default_factory=list)
    provenance_errors: list[str] = field(default_factory=list)
    missing_seed_classes: list[str] = field(default_factory=list)
    failed_trials: list[dict] = field(default_factory=list)
    failed_clean_runs: list[dict] = field(default_factory=list)
    status_field: str | None = None
    record: dict | None = None

    @property
    def passing(self) -> bool:
        """No blocking without a passing record: validity is read via
        ``passing == True`` here, never inferred from the default exit code
        (0 in report-only mode even when not validated).
        @cw-trace guards INV-fh-003 CTR-fh-043"""
        return (
            self.record_found
            and not self.schema_errors
            and not self.provenance_errors
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
            "provenance_errors": self.provenance_errors,
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


def corpus_digest(root: str | Path) -> str:
    """Content digest of a validation corpus directory — the ``sha`` a record
    pins its trials to when the corpus is an in-repo fixture tree rather than a
    git SHA of an external repo. Any change to any file under the corpus
    changes the digest, so a record authored against an older corpus is
    detectably stale (tests re-derive this and compare). This is the mechanism
    that lets saas_gate/quality_slop_gate records pin a FIXTURE/recorded target
    (never a live URL / AI band) with reproducible, staleness-checked clean runs.
    @cw-trace guards CTR-fh-044"""
    h = hashlib.sha256()
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


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


def trial_genuinely_passed(trial: dict) -> bool:
    """A trial passes iff its ``result`` matches its ``expected`` outcome AND its
    own ``passed`` flag agrees. Derived, never trusted: a forged ``passed: true``
    on a trial whose result contradicts its expectation does not count."""
    derived = EXPECTED_TO_RESULT.get(trial.get("expected")) == trial.get("result")
    return derived and trial.get("passed") is True


def required_seed_classes(record: dict) -> list[str]:
    """The seed classes THIS record must carry genuinely-passing trials for,
    given its own telemetry_dependent / concurrency_applicable declarations."""
    required = [DIRECT_CLASS, *ALWAYS_MANDATORY_EVASIONS]
    if record.get("concurrency_applicable", True):
        required.append(CONCURRENCY_CLASS)
    if record.get("telemetry_dependent", False):
        required.append(INSTRUMENTATION_CLASS)
    return required


def _live_scanner_version(gate: str, scripts_dir: Path) -> str | None:
    """The gate's CURRENT ``--scanner-version`` output, or None when the gate
    script is absent or doesn't support the flag (nothing to compare against)."""
    script = scripts_dir / f"{gate}.py"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--scanner-version"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _ratchet_provenance_errors(gate: str, record: dict, validation_dir: Path) -> list[str]:
    """Corroborate the record's ``ratchet_record_id`` against the hash-chained
    ratchet journal beside the validation dir. The chain is the tamper-evidence
    (docs/ratchet.md): a record id that isn't journaled — or a journal whose
    chain doesn't verify — grants no provenance."""
    rid_field = record.get("ratchet_record_id")
    rid_match = RID_RE.fullmatch(rid_field.strip()) if isinstance(rid_field, str) else None
    if rid_match is None:
        return [
            f"ratchet_record_id {rid_field!r} is not a journaled record id (rec-NNNNN) — "
            "validation results must be recorded via the ratchet journal (docs/gate-validation.md)"
        ]
    rid = rid_match.group(0)
    journal = validation_dir.resolve().parent / JOURNAL_NAME
    if not journal.is_file():
        return [f"ratchet journal not found at {journal} — cannot corroborate ratchet_record_id {rid}"]
    try:
        entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    except json.JSONDecodeError:
        return [f"ratchet journal at {journal} is unreadable — cannot corroborate {rid}"]
    prev = "genesis"
    for i, entry in enumerate(entries):
        body = {k: v for k, v in entry.items() if k != "record_hash"}
        expect = stable_hash(prev, json.dumps(body, sort_keys=True))
        if entry.get("record_hash") != expect:
            return [f"ratchet journal chain broken at entry {i} ({entry.get('record_id', '?')}) — fail closed"]
        prev = expect
    match = next((e for e in entries if e.get("record_id") == rid), None)
    if match is None:
        return [f"ratchet_record_id {rid} not found in the ratchet journal at {journal}"]
    errors: list[str] = []
    if match.get("event") != "gate-validation":
        errors.append(
            f"journal entry {rid} has event {match.get('event')!r}, expected 'gate-validation'"
        )
    if match.get("ref") != gate:
        errors.append(
            f"journal entry {rid} ref {match.get('ref')!r} does not name gate {gate!r}"
        )
    return errors


def check(
    gate: str,
    validation_dir: str | Path,
    schema: dict | None = None,
    scripts_dir: str | Path | None = None,
) -> GateValidationReport:
    schema = schema or load_schema()
    scripts_dir = Path(scripts_dir) if scripts_dir else Path(__file__).resolve().parent
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

    # Provenance: the record must be FOR this gate, current, and journaled.
    if record.get("gate") != gate:
        report.provenance_errors.append(
            f"record's gate field is {record.get('gate')!r}, not {gate!r} — a record copied "
            "from another gate grants no authority"
        )
    live = _live_scanner_version(gate, scripts_dir)
    if live is not None and record.get("scanner_version") != live:
        report.provenance_errors.append(
            f"scanner_version mismatch: record has {record.get('scanner_version')!r} but the live "
            f"gate reports {live!r} — the record is stale; re-run the trials against the current "
            "gate and re-author the record"
        )
    report.provenance_errors.extend(_ratchet_provenance_errors(gate, record, Path(validation_dir)))

    # Trials: pass/fail is DERIVED (result vs expected), never trusted from the flag.
    trials = record.get("seeded_defect_trials", []) or []
    report.failed_trials = [t for t in trials if not trial_genuinely_passed(t)]
    passed_classes = {t.get("seed_class") for t in trials if trial_genuinely_passed(t)}
    report.missing_seed_classes = [c for c in required_seed_classes(record) if c not in passed_classes]

    # Clean-corpus runs: derived too — zero findings AND real coverage.
    clean_runs = record.get("clean_corpus_runs", []) or []
    report.failed_clean_runs = [
        r for r in clean_runs
        if not (r.get("passed") is True and r.get("findings") == 0 and _has_nonzero_coverage(r.get("coverage")))
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
    if report.provenance_errors:
        lines += ["", "## Provenance errors", ""] + [f"- {e}" for e in report.provenance_errors]
    if report.missing_seed_classes:
        lines += ["", "## Missing mandatory seed classes", ""] + [f"- {c}" for c in report.missing_seed_classes]
    if report.failed_trials:
        lines += ["", "## Failed seeded-defect trials (pass/fail derived from result vs expected)", ""]
        lines += [f"- {t.get('seed_id', '?')} ({t.get('seed_class', '?')}): "
                   f"expected={t.get('expected')} result={t.get('result')} passed={t.get('passed')}"
                   for t in report.failed_trials]
    if report.failed_clean_runs:
        lines += ["", "## Failed/unproven clean-corpus runs", ""]
        lines += [f"- {r.get('repo', '?')}@{r.get('sha', '?')}: passed={r.get('passed')} "
                   f"findings={r.get('findings')} coverage={r.get('coverage')}"
                   for r in report.failed_clean_runs]
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
        help=f"Directory containing <gate>.json validation records (default: {DEFAULT_VALIDATION_DIR}; "
             "the ratchet journal that corroborates them is expected at its sibling "
             f"../{JOURNAL_NAME})",
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

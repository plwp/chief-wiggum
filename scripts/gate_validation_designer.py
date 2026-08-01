#!/usr/bin/env python3
"""Gate-validation designer — audits the per-gate validation records (#218).

The gate-validation protocol (docs/gate-validation.md) is enforced by
``check_gate_validation.py``, which BLOCKS on a non-passing record. This tool is
the other half the deferred-rigor index promised once the gate count made the
manual protocol a real burden (trigger: >5 validated gates): a DESIGNER that
reads each gate's record + authority-boundary statement and *proposes* — the
mandatory seed-class matrix, the gaps in it, the boundary claims no trial
proves, the seeds nothing can re-execute, and (via mutation testing) the
detection logic no seed pins.

**REPORT-ONLY ALWAYS.** Every subcommand prints findings and exits 0. The
designer proposes, never blocks: per docs/gate-rollout.md it must earn any
blocking authority through its own validation record, like every other gate —
and it does not have one yet.

Subcommands:

- ``audit [<gate>|--all]`` — per record: propose the mandatory seed-class
  matrix (direct + the evasion trio + concurrency-where-applicable +
  instrumentation-deleted-where-telemetry-dependent) and flag
  (a) mandatory classes with no genuinely-passing trial,
  (b) trials whose seed_id has no re-executable entry in the retroactive
  suite (``tests/test_gate_validation_retroactive.py``; seeds found in
  another suite are located and reported, not flagged),
  (c) authority-boundary claims that name a limit but have no no-fire trial
  proving the boundary — an HONEST keyword-overlap heuristic, every match
  labeled proven-confident/proven-uncertain and possibly wrong,
  (d) clean-corpus runs whose coverage evidence is missing or all-zero (a
  no-op wearing a green checkmark), and
  (e) drift between the record's trials and its extracted seeds file.
- ``matrix`` — the one-screen gates x seed-classes coverage table.
- ``escapes [<gate>]`` — the INDEPENDENT escape intake (the settled #218
  note): reads ONLY the factory log (``factory_log.py bug --missed-by``
  events) and the records — never the designer's own proposals — and joins
  each escape against the named gate's certified classes: a class the record
  certified CAUGHT -> DEMOTION (docs/gate-validation.md); a certified
  no-fire boundary -> consistent, no demotion, stated.
- ``extract-seeds <gate>|--all`` — materialize the record's trials into
  ``<validation-dir>/seeds/<gate>.seeds.json`` so seeds version
  independently of gate code (``seed_version`` never moves with
  ``scanner_version``). The file is a DERIVED artifact: re-authoring a
  record means re-running extract-seeds, and ``audit`` reports drift.
- ``mutate <gate>`` — best-effort mutation-testing leg: runs mutmut (when
  installed) scoped to the gate's script with the gate's own unit tests as
  the runner. A mutant that SURVIVES the gate's tests is detection logic no
  seed pins — each survivor is proposed as a new seed-class candidate.
  When mutmut is absent: skipped WITH instructions, never silent. Runtime
  is bounded: the run gets ``--max-mutants x --per-mutant-seconds`` wall
  time (defaults 20 x 30s) and the candidate list is capped at
  ``--max-mutants``.

Exit codes: 0 always (report-only; it proposes, never blocks). 2 = usage.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_gate_validation import (  # noqa: E402
    _has_nonzero_coverage,
    load_record,
    required_seed_classes,
    trial_genuinely_passed,
)
from factory_log import DEFAULT_VALIDATION_DIR, log_path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTS_DIR = ROOT / "tests"
RETROACTIVE_SUITE = "test_gate_validation_retroactive.py"

# The protocol's full seed-class inventory, in presentation order.
SEED_CLASSES = (
    "direct",
    "evasion-omission",
    "evasion-config-indirection",
    "evasion-sampling-gap",
    "evasion-concurrency",
    "instrumentation-deleted",
)

BANNER = (
    "gate_validation_designer: REPORT-ONLY — proposes, never blocks (exit 0 always). "
    "Per docs/gate-rollout.md it earns blocking authority only via its own "
    "validation record, which it does not yet have."
)

# --- boundary-claim heuristic --------------------------------------------------

# Generic protocol/prose words that carry no matching signal between an
# authority-boundary assumption and a trial description.
_STOPWORDS = frozenset({
    "design", "designed", "designs", "scope", "never", "only", "with", "that",
    "this", "these", "those", "which", "from", "into", "over", "under", "when",
    "must", "does", "correctly", "boundary", "certified", "documented", "fire",
    "fires", "fired", "finding", "findings", "check", "checks", "checked",
    "gate", "record", "trial", "seed", "expected", "will", "would", "there",
    "here", "than", "then", "have", "been", "being", "such", "each", "every",
    "rather", "itself", "because", "where", "while", "still", "also", "very",
})

# Markers that make an assumption read as a LIMIT claim (something the gate
# does NOT do / cannot see) — the kind of claim a no-fire trial should prove.
_LIMIT_MARKERS = (
    "not ", "never", "out of scope", "excluded", "exclude", "exempt", "only",
    "skip", "absent", "says nothing", "no ", "invisible", "cannot", "blind",
    "out of this record",
)


def _tokens(text: str) -> set[str]:
    """Significant, lightly-stemmed tokens for the overlap heuristic."""
    out = set()
    for tok in re.findall(r"[a-z0-9_]+", text.lower()):
        if len(tok) < 4 or tok in _STOPWORDS:
            continue
        stemmed = tok.rstrip("s").rstrip("d")
        out.add(stemmed if len(stemmed) >= 4 else tok)
    return out


def _is_limit_claim(assumption: str) -> bool:
    low = assumption.lower()
    return any(m in low for m in _LIMIT_MARKERS)


def boundary_coverage(record: dict) -> list[dict]:
    """Match each authority-boundary assumption against the genuinely-passing
    no-fire trials. HEURISTIC: token overlap, confidence-labeled — a match may
    be wrong in both directions and is a scrutiny cue, never a verdict."""
    assumptions = (record.get("authority_boundary") or {}).get("assumptions") or []
    no_fire = [t for t in record.get("seeded_defect_trials") or []
               if t.get("expected") == "no-fire" and trial_genuinely_passed(t)]
    rows = []
    for assumption in assumptions:
        a_tokens = _tokens(assumption)
        best_trial, best_overlap = None, 0
        for t in no_fire:
            overlap = len(a_tokens & _tokens(str(t.get("injected", ""))))
            if overlap > best_overlap:
                best_trial, best_overlap = t, overlap
        if best_overlap >= 3:
            verdict = "proven-confident"
        elif best_overlap >= 1:
            verdict = "proven-uncertain"
        else:
            verdict = "unproven"
        rows.append({
            "assumption": assumption,
            "limit_claim": _is_limit_claim(assumption),
            "verdict": verdict,
            "trial": best_trial.get("seed_id") if best_trial else None,
            "overlap": best_overlap,
        })
    return rows


# --- seed re-execution scan ----------------------------------------------------


def seed_execution_locations(seed_ids: list[str], tests_dir: Path) -> dict[str, str]:
    """Where each seed_id is re-executable: 'retroactive' (the protocol home,
    tests/test_gate_validation_retroactive.py), 'other:<file>' (a companion
    suite, e.g. saas_gate's fixture-server tests), or 'missing'."""
    tests_dir = Path(tests_dir)
    retro = tests_dir / RETROACTIVE_SUITE
    retro_text = retro.read_text() if retro.is_file() else ""
    others = sorted(p for p in tests_dir.glob("test_*.py") if p.name != RETROACTIVE_SUITE)
    out: dict[str, str] = {}
    for sid in seed_ids:
        if sid in retro_text:
            out[sid] = "retroactive"
            continue
        hit = next((p for p in others if sid in p.read_text()), None)
        out[sid] = f"other:{hit.name}" if hit else "missing"
    return out


# --- seeds files: versioned separately from gate code --------------------------


def seeds_from_record(record: dict) -> list[dict]:
    return [
        {
            "seed_id": t.get("seed_id"),
            "seed_class": t.get("seed_class"),
            "seed_version": t.get("seed_version"),
            "corpus": t.get("repo"),
            "corpus_digest": t.get("sha"),
            "injected": t.get("injected"),
            "expected": t.get("expected"),
        }
        for t in record.get("seeded_defect_trials") or []
    ]


def seeds_path(gate: str, validation_dir: str | Path) -> Path:
    return Path(validation_dir) / "seeds" / f"{gate}.seeds.json"


def extract_seeds(gate: str, validation_dir: str | Path) -> Path:
    """Materialize the record's trials into <validation-dir>/seeds/<gate>.seeds.json.

    Deterministic (no timestamps, no absolute paths) so regeneration from an
    unchanged record is byte-identical — the seeds file is a DERIVED artifact
    whose staleness is detectable, and its ``seed_version`` fields version the
    seeds independently of the gate's own ``scanner_version`` (the settled
    #218 note: a gate implementation can't quietly 'pass' by editing its seed
    suite in lockstep with a weakened scanner)."""
    record = load_record(gate, validation_dir)
    if record is None:
        raise FileNotFoundError(f"no validation record for {gate} in {validation_dir}")
    path = seeds_path(gate, validation_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "gate": gate,
        "extracted_from": f"{gate}.json",
        "note": (
            "Derived artifact (chief-wiggum#218): seeds versioned separately from gate "
            "code. Regenerate with `gate_validation_designer.py extract-seeds "
            f"{gate}` whenever the record's trials change — `audit` reports drift "
            "between record and seeds file otherwise."
        ),
        "seeds": seeds_from_record(record),
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def seeds_drift(record: dict, seeds_file: Path) -> list[str]:
    """Differences between the record's trials and the extracted seeds file."""
    try:
        stored = json.loads(seeds_file.read_text()).get("seeds") or []
    except (OSError, json.JSONDecodeError):
        return [f"seeds file {seeds_file.name} is unreadable"]
    expected = {s["seed_id"]: s for s in seeds_from_record(record)}
    got = {s.get("seed_id"): s for s in stored}
    drift = []
    for sid in sorted(set(expected) - set(got)):
        drift.append(f"{sid}: in record but missing from seeds file")
    for sid in sorted(set(got) - set(expected)):
        drift.append(f"{sid}: in seeds file but not in record")
    for sid in sorted(set(expected) & set(got)):
        diffs = [k for k in expected[sid] if expected[sid][k] != got[sid].get(k)]
        if diffs:
            drift.append(f"{sid}: fields differ from record: {', '.join(diffs)}")
    return drift


# --- the audit -----------------------------------------------------------------


def proposed_matrix(record: dict) -> dict[str, dict]:
    """The mandatory seed-class matrix THIS record must satisfy, with a status
    per class: covered / missing / failing / n/a / n/a-unjustified."""
    required = set(required_seed_classes(record))
    trials_by_class: dict[str, list[dict]] = {}
    for t in record.get("seeded_defect_trials") or []:
        trials_by_class.setdefault(t.get("seed_class"), []).append(t)
    matrix: dict[str, dict] = {}
    for cls in SEED_CLASSES:
        trials = trials_by_class.get(cls, [])
        passing = [t for t in trials if trial_genuinely_passed(t)]
        entry: dict = {"required": cls in required,
                       "trials": [t.get("seed_id") for t in trials],
                       "passing": [t.get("seed_id") for t in passing]}
        if passing:
            entry["status"] = "covered"
            entry["why"] = f"{len(passing)} genuinely-passing trial(s)"
        elif cls in required and trials:
            entry["status"] = "failing"
            entry["why"] = "trial(s) present but none genuinely passes (result vs expected)"
        elif cls in required:
            entry["status"] = "missing"
            entry["why"] = "mandatory class with no trial at all"
        elif cls == "evasion-concurrency":
            note = record.get("concurrency_note")
            if note:
                entry["status"] = "n/a"
                entry["why"] = note
            else:
                entry["status"] = "n/a-unjustified"
                entry["why"] = ("concurrency_applicable: false but no concurrency_note — "
                                "the protocol requires a justification, not a silent omission")
        elif cls == "instrumentation-deleted":
            entry["status"] = "n/a"
            entry["why"] = ("telemetry_dependent: false — no instrumentation channel to "
                            "delete (protocol: omit this seed)")
        else:  # pragma: no cover - the trio and direct are always required
            entry["status"] = "n/a"
            entry["why"] = "not required by this record's declarations"
        matrix[cls] = entry
    return matrix


def audit_gate(gate: str, validation_dir: str | Path,
               tests_dir: str | Path | None = None) -> dict:
    """One gate's designer audit: proposed matrix + findings (a)-(e).

    Report-only data — callers render it; nothing here blocks anything."""
    validation_dir = Path(validation_dir)
    tests_dir = Path(tests_dir) if tests_dir else DEFAULT_TESTS_DIR
    record = load_record(gate, validation_dir)
    if record is None:
        return {"gate": gate, "record_found": False, "proposed_matrix": {},
                "boundary": [], "boundary_note": "", "seed_execution": {},
                "seeds_file": None,
                "findings": [{"kind": "no-record",
                              "detail": f"no validation record at "
                                        f"{validation_dir / (gate + '.json')}"}]}

    findings: list[dict] = []
    matrix = proposed_matrix(record)

    # (a) mandatory classes with no genuinely-passing trial
    for cls, entry in matrix.items():
        if entry["status"] in ("missing", "failing"):
            findings.append({"kind": "missing-mandatory-class", "seed_class": cls,
                             "detail": entry["why"]})
        elif entry["status"] == "n/a-unjustified":
            findings.append({"kind": "na-without-justification", "seed_class": cls,
                             "detail": entry["why"]})

    # (b) re-executable seeds — the retroactive suite is the protocol home
    seed_ids = [t.get("seed_id") for t in record.get("seeded_defect_trials") or []]
    execution = seed_execution_locations(seed_ids, tests_dir)
    for sid, loc in execution.items():
        if loc == "missing":
            findings.append({"kind": "no-reexecutable-seed", "seed_id": sid,
                             "detail": f"seed_id {sid!r} appears in no test suite under "
                                       f"{tests_dir.name}/ — the trial cannot be re-executed, "
                                       "so the record is an aspirational claim for it"})
        elif loc.startswith("other:"):
            findings.append({"kind": "seed-outside-retroactive-suite", "seed_id": sid,
                             "detail": f"re-executed in {loc.removeprefix('other:')} rather "
                                       f"than {RETROACTIVE_SUITE} (informational)"})

    # (c) boundary claims — honest heuristic, confidence-labeled
    boundary = boundary_coverage(record)
    for row in boundary:
        if row["limit_claim"] and row["verdict"] == "unproven":
            findings.append({"kind": "unproven-boundary-claim",
                             "assumption": row["assumption"],
                             "detail": "boundary statement names a limit but no "
                                       "genuinely-passing no-fire trial matches it "
                                       "(heuristic match — verify by reading the record)"})

    # (d) clean-corpus coverage evidence
    for run in record.get("clean_corpus_runs") or []:
        if not _has_nonzero_coverage(run.get("coverage")):
            findings.append({"kind": "empty-coverage-clean-run",
                             "detail": f"clean run on {run.get('repo', '?')} has missing or "
                                       "all-zero coverage — a 'no findings' with nothing "
                                       "exercised is a no-op wearing a green checkmark"})

    # (e) seeds file cross-check
    sfile = seeds_path(gate, validation_dir)
    if sfile.is_file():
        for d in seeds_drift(record, sfile):
            findings.append({"kind": "seeds-file-drift", "detail": d})
    else:
        findings.append({"kind": "no-seeds-file",
                         "detail": f"no {sfile.name} — run `gate_validation_designer.py "
                                   f"extract-seeds {gate}` to version the seeds "
                                   "independently of gate code"})

    return {
        "gate": gate,
        "record_found": True,
        "proposed_matrix": matrix,
        "boundary": boundary,
        "boundary_note": (
            "Boundary-claim matching is a keyword-overlap HEURISTIC: matches are "
            "labeled proven-confident/proven-uncertain and can be wrong in both "
            "directions (a lucky word overlap is not a proof; a reworded claim can "
            "hide a real proof). Treat unproven claims as scrutiny cues."
        ),
        "seed_execution": execution,
        "seeds_file": str(sfile) if sfile.is_file() else None,
        "findings": findings,
    }


# --- matrix view ---------------------------------------------------------------


def gates_with_records(validation_dir: str | Path) -> list[str]:
    return sorted(p.stem for p in Path(validation_dir).glob("*.json"))


def matrix_rows(validation_dir: str | Path, tests_dir: str | Path | None = None) -> list[dict]:
    rows = []
    for gate in gates_with_records(validation_dir):
        audit = audit_gate(gate, validation_dir, tests_dir=tests_dir)
        rows.append({"gate": gate,
                     "cells": {cls: audit["proposed_matrix"][cls]["status"]
                               for cls in SEED_CLASSES} if audit["record_found"] else {},
                     "findings": len([f for f in audit["findings"]
                                      if f["kind"] != "seed-outside-retroactive-suite"])})
    return rows


_CELL_TEXT = {"covered": "pass", "missing": "MISSING", "failing": "FAILING",
              "n/a": "n/a", "n/a-unjustified": "n/a(!)"}
_SHORT_CLASS = {"direct": "direct", "evasion-omission": "omission",
                "evasion-config-indirection": "config-ind",
                "evasion-sampling-gap": "sampling",
                "evasion-concurrency": "concurr",
                "instrumentation-deleted": "instr-del"}


def render_matrix(rows: list[dict]) -> str:
    width = max([len(r["gate"]) for r in rows] + [4]) + 2
    header = "GATE".ljust(width) + "".join(_SHORT_CLASS[c].ljust(12) for c in SEED_CLASSES)
    lines = [header, "-" * len(header)]
    for r in rows:
        cells = "".join(_CELL_TEXT.get(r["cells"].get(c, "?"), "?").ljust(12)
                        for c in SEED_CLASSES)
        lines.append(r["gate"].ljust(width) + cells)
    lines.append("")
    lines.append("pass = genuinely-passing trial(s); MISSING = mandatory class with no "
                 "trial; FAILING = trials present, none passes; n/a = not applicable "
                 "with justification; n/a(!) = inapplicability claimed WITHOUT justification")
    return "\n".join(lines)


# --- escapes: INDEPENDENT intake (reads only log + records) --------------------
#
# The settled #218 note (refuter #18-20): escapes feed demotion decisions
# INDEPENDENTLY of the designer that authored/proposed the seeds. This section
# deliberately consumes nothing from audit_gate/proposed_matrix — only the
# factory log's escape events and the raw records on disk.


def _certified_classes(record: dict) -> tuple[set[str], set[str]]:
    """(caught, boundary): seed classes the record certifies as CAUGHT
    (expected fire -> fired, passed) vs certified NON-coverage boundaries
    (expected no-fire -> not-fired, passed)."""
    caught, boundary = set(), set()
    for t in record.get("seeded_defect_trials") or []:
        if t.get("passed") is not True:
            continue
        if t.get("expected") == "fire" and t.get("result") == "fired":
            caught.add(t.get("seed_class"))
        elif t.get("expected") == "no-fire" and t.get("result") == "not-fired":
            boundary.add(t.get("seed_class"))
    return caught, boundary


def _demotion_instruction(gate: str, seed_class: str) -> str:
    # Verbatim-in-spirit from docs/gate-validation.md ("Demotion: an escape a
    # seed class should have caught").
    return (
        f"DEMOTE {gate}: (1) revert the gate to report-only — drop --gate/--gate "
        f"coverage from its workflow wiring (/architect, /close-epic) until "
        f"re-validated; (2) file a tracking ticket to re-derive and re-run seed "
        f"class {seed_class!r} — the seed as authored did not represent the real "
        "evasion technique that actually shipped, so the seed itself (not just "
        "the gate) needs revision (docs/gate-validation.md)."
    )


def escapes_view(log_file: str | Path, validation_dir: str | Path,
                 gate: str | None = None) -> list[dict]:
    """Join factory-log escape events against the records' certified classes.

    Reads ONLY the log and the records — never the designer's proposals — so a
    designer bug/bias cannot shape which escapes demote (independent intake)."""
    log_file = Path(log_file)
    events: list[dict] = []
    if log_file.is_file():
        for line in log_file.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows = []
    for e in events:
        if e.get("event") != "escape" or not e.get("missed_by"):
            continue
        if gate and e.get("missed_by") != gate:
            continue
        g = e["missed_by"]
        seed_class = e.get("seed_class")
        record = load_record(g, validation_dir)
        row = {"gate": g, "seed_class": seed_class, "summary": e.get("summary"),
               "severity": e.get("severity"), "ts": e.get("ts")}
        if record is None:
            row["verdict"] = "no-record"
            row["instruction"] = (
                f"no validation record for {g!r} — nothing to join against; the gate "
                "has no certified classes to be proven wrong about (and per "
                "INV-fh-003 it cannot be blocking without a record)")
        elif not seed_class:
            row["verdict"] = "unclassified"
            row["instruction"] = (
                "escape carries no seed_class — re-log with `factory_log.py bug "
                "--seed-class <class>` so the demotion join can run")
        else:
            caught, boundary = _certified_classes(record)
            if seed_class in caught:
                row["verdict"] = "DEMOTION"
                row["instruction"] = _demotion_instruction(g, seed_class)
            elif seed_class in boundary:
                row["verdict"] = "boundary-consistent"
                row["instruction"] = (
                    f"no demotion: {g}'s record certifies {seed_class!r} as a documented "
                    "NON-coverage boundary (a passing expected:no-fire trial) — the "
                    "escape is consistent with the stated authority boundary. Consider "
                    "whether the boundary itself should shrink, but the record was not "
                    "wrong about production recall.")
            else:
                row["verdict"] = "uncertified-class"
                row["instruction"] = (
                    f"no demotion mandated: {g}'s record never certified "
                    f"{seed_class!r} at all — the validation made no claim this escape "
                    "refutes. The class has no trial; derive one before the gate's "
                    "authority statement can speak to it.")
        rows.append(row)
    return rows


# --- mutate: best-effort mutmut leg --------------------------------------------


def _mutmut_available() -> bool:
    return importlib.util.find_spec("mutmut") is not None


def _parse_surviving(results_stdout: str) -> list[int]:
    """Extract surviving-mutant ids from `mutmut results` output: the id/range
    lines under the 'Survived' section (skipping counts in headers)."""
    survivors: list[int] = []
    in_survived = False
    for line in results_stdout.splitlines():
        stripped = line.strip()
        if "Survived" in stripped:
            in_survived = True
            continue
        if not in_survived:
            continue
        if not stripped or stripped.startswith("----"):
            continue
        if re.fullmatch(r"[\d,\-\s]+", stripped):
            for part in re.findall(r"\d+-\d+|\d+", stripped):
                if "-" in part:
                    lo, hi = part.split("-")
                    survivors.extend(range(int(lo), int(hi) + 1))
                else:
                    survivors.append(int(part))
        else:
            break  # next section (Killed/Timeout/...) — survived block is done
    return sorted(set(survivors))


def mutate_gate(gate: str, scripts_dir: str | Path, tests_path: str | Path | None = None,
                max_mutants: int = 20, per_mutant_seconds: int = 30,
                runner=subprocess.run, mutmut_available: bool | None = None) -> dict:
    """Best-effort mutation run scoped to the gate's script file, using the
    gate's own unit tests as the runner. Surviving mutants = detection logic
    not pinned by any seed -> each is a proposed new seed-class candidate.

    Bounded: the mutmut run gets max_mutants * per_mutant_seconds seconds of
    wall time (a timeout yields PARTIAL results — mutmut caches progress — and
    is reported, never silent), and the candidate list is capped at
    max_mutants. When mutmut is not installed the result is
    skipped-with-instructions, never silent."""
    scripts_dir = Path(scripts_dir)
    if mutmut_available is None:
        mutmut_available = _mutmut_available()
    if not mutmut_available:
        return {"status": "skipped", "instructions": (
            "mutmut is not installed — the mutation leg is skipped. To run it:\n"
            "  pip install mutmut   # in the CW venv (feedback: use a venv)\n"
            f"  python3 scripts/gate_validation_designer.py mutate {gate}\n"
            "Surviving mutants indicate detection logic no seed pins; each is a "
            "proposed new seed-class candidate for the record + seeds file.")}
    script = scripts_dir / f"{gate}.py"
    if not script.is_file():
        return {"status": "error", "detail": f"gate script not found: {script.name} "
                                             f"(looked in {scripts_dir})"}
    tests_path = Path(tests_path) if tests_path else scripts_dir.parent / "tests" / f"test_{gate}.py"
    timeout = max_mutants * per_mutant_seconds
    run_cmd = [sys.executable, "-m", "mutmut", "run",
               "--paths-to-mutate", str(script),
               "--tests-dir", str(tests_path.parent),
               "--runner", f"{sys.executable} -m pytest -x -q {tests_path}"]
    status, detail = "ran", ""
    try:
        runner(run_cmd, capture_output=True, text=True, timeout=timeout,
               cwd=scripts_dir.parent)
    except subprocess.TimeoutExpired:
        status = "partial"
        detail = (f"mutmut run timed out after {timeout}s (bounded by "
                  "--max-mutants x --per-mutant-seconds); results below are partial "
                  "(mutmut caches progress across runs)")
    except OSError as exc:
        return {"status": "error", "detail": f"could not run mutmut: {exc}"}
    results = runner([sys.executable, "-m", "mutmut", "results"],
                     capture_output=True, text=True, timeout=120,
                     cwd=scripts_dir.parent)
    survivors = _parse_surviving(results.stdout or "")
    candidates = [
        {"mutant_id": mid,
         "show": f"python -m mutmut show {mid}",
         "proposal": ("surviving mutant — detection logic not pinned by any seed; "
                      "derive a seed that kills it (a new seed-class candidate) and "
                      "add it to the record + seeds file")}
        for mid in survivors[:max_mutants]
    ]
    return {"status": status, "detail": detail, "surviving_mutants": survivors,
            "candidates": candidates, "truncated": len(survivors) > max_mutants,
            "raw_results": results.stdout or ""}


# --- rendering -----------------------------------------------------------------


def render_audit(audit: dict) -> str:
    lines = [f"# Designer audit — {audit['gate']}", ""]
    if not audit["record_found"]:
        lines.append(f"No validation record found ({audit['findings'][0]['detail']}).")
        return "\n".join(lines) + "\n"
    lines.append("## Proposed mandatory seed-class matrix")
    lines.append("")
    for cls in SEED_CLASSES:
        e = audit["proposed_matrix"][cls]
        mark = _CELL_TEXT[e["status"]]
        req = "required" if e["required"] else "n/a"
        lines.append(f"- {cls}: {mark} [{req}] — {e['why']}")
    real = [f for f in audit["findings"] if f["kind"] != "seed-outside-retroactive-suite"]
    info = [f for f in audit["findings"] if f["kind"] == "seed-outside-retroactive-suite"]
    lines += ["", f"## Findings ({len(real)})", ""]
    if real:
        for f in real:
            subject = f.get("seed_class") or f.get("seed_id") or ""
            if f.get("assumption"):
                a = f["assumption"]
                subject = f"\"{a[:90]}...\"" if len(a) > 90 else f"\"{a}\""
            lines.append(f"- [{f['kind']}] {subject} — {f['detail']}"
                         if subject else f"- [{f['kind']}] {f['detail']}")
    else:
        lines.append("- none")
    if info:
        lines += ["", "## Informational", ""]
        lines += [f"- [{f['kind']}] {f['seed_id']}: {f['detail']}" for f in info]
    lines += ["", "## Authority-boundary claims vs no-fire trials", ""]
    for b in audit["boundary"]:
        trial = f" (trial {b['trial']}, overlap {b['overlap']})" if b["trial"] else ""
        limit = "limit" if b["limit_claim"] else "non-limit"
        lines.append(f"- [{b['verdict']}/{limit}]{trial} {b['assumption']}")
    lines += ["", f"Note: {audit['boundary_note']}"]
    return "\n".join(lines) + "\n"


def render_escapes(rows: list[dict]) -> str:
    if not rows:
        return "No escape events with --missed-by found in the factory log.\n"
    lines = [f"# Escape intake — {len(rows)} escape(s) joined against validation records", ""]
    for r in rows:
        lines.append(f"- [{r['verdict']}] {r['gate']} / {r.get('seed_class') or 'no-class'} "
                     f"— {r.get('summary')!r} ({r.get('severity')})")
        lines.append(f"    {r['instruction']}")
    demotions = [r for r in rows if r["verdict"] == "DEMOTION"]
    if demotions:
        lines += ["", f"DEMOTIONS REQUIRED: {len(demotions)} — see instructions above."]
    return "\n".join(lines) + "\n"


def render_mutate(gate: str, result: dict) -> str:
    lines = [f"# Mutation leg — {gate} (status: {result['status']})", ""]
    if result["status"] == "skipped":
        lines.append(result["instructions"])
    elif result["status"] == "error":
        lines.append(result["detail"])
    else:
        if result.get("detail"):
            lines.append(result["detail"])
        survivors = result.get("surviving_mutants", [])
        lines.append(f"Surviving mutants: {len(survivors)}"
                     + (" (candidate list truncated to --max-mutants)"
                        if result.get("truncated") else ""))
        for c in result.get("candidates", []):
            lines.append(f"- mutant {c['mutant_id']}: {c['show']} — {c['proposal']}")
        if not survivors:
            lines.append("No survivors parsed — either every mutant was killed (good: the "
                         "gate's tests pin its detection logic) or the mutmut output "
                         "format was unrecognized; raw output follows.")
            lines += ["", result.get("raw_results", "")]
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate-validation designer (report-only always: proposes, never blocks).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p, gate_optional=True):
        if gate_optional:
            p.add_argument("gate", nargs="?", help="Gate name (omit with --all)")
            p.add_argument("--all", action="store_true", help="All gates with records")
        else:
            p.add_argument("gate", help="Gate name")
        p.add_argument("--validation-dir", default=DEFAULT_VALIDATION_DIR)
        p.add_argument("--tests-dir", default=str(DEFAULT_TESTS_DIR),
                       help="Tests dir scanned for re-executable seed_ids")
        p.add_argument("--format", choices=["text", "json"], default="text")

    _common(sub.add_parser("audit", help="Audit records against the protocol's matrix"))
    m = sub.add_parser("matrix", help="Gates x seed-classes coverage table")
    m.add_argument("--validation-dir", default=DEFAULT_VALIDATION_DIR)
    m.add_argument("--tests-dir", default=str(DEFAULT_TESTS_DIR))
    m.add_argument("--format", choices=["text", "json"], default="text")

    e = sub.add_parser("escapes", help="Independent escape intake (log + records only)")
    e.add_argument("gate", nargs="?", help="Filter to one gate")
    e.add_argument("--validation-dir", default=DEFAULT_VALIDATION_DIR)
    e.add_argument("--factory-log", default=str(log_path()),
                   help="Factory telemetry log (default: factory_log.py's log path)")
    e.add_argument("--format", choices=["text", "json"], default="text")

    x = sub.add_parser("extract-seeds", help="Materialize a record's trials into its seeds file")
    x.add_argument("gate", nargs="?", help="Gate name (omit with --all)")
    x.add_argument("--all", action="store_true")
    x.add_argument("--validation-dir", default=DEFAULT_VALIDATION_DIR)

    mu = sub.add_parser("mutate", help="Best-effort mutmut leg (skipped-with-instructions "
                                       "when mutmut is absent)")
    mu.add_argument("gate", help="Gate name")
    mu.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    mu.add_argument("--tests", default=None, help="Unit-test file for the gate "
                                                  "(default: tests/test_<gate>.py)")
    mu.add_argument("--max-mutants", type=int, default=20,
                    help="Bound: candidate cap AND (x --per-mutant-seconds) the run's "
                         "wall-time budget (default 20)")
    mu.add_argument("--per-mutant-seconds", type=int, default=30,
                    help="Per-mutant wall-time budget used to derive the run timeout "
                         "(default 30)")
    mu.add_argument("--format", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    out_json = getattr(args, "format", "text") == "json"
    if not out_json:
        print(BANNER)
        print()

    if args.cmd in ("audit",):
        gates = [args.gate] if args.gate else gates_with_records(args.validation_dir)
        audits = [audit_gate(g, args.validation_dir, tests_dir=args.tests_dir)
                  for g in gates]
        if out_json:
            print(json.dumps({"report_only": True, "gates": audits}, indent=2))
        else:
            for a in audits:
                print(render_audit(a))
    elif args.cmd == "matrix":
        rows = matrix_rows(args.validation_dir, tests_dir=args.tests_dir)
        if out_json:
            print(json.dumps({"report_only": True, "rows": rows}, indent=2))
        else:
            print(render_matrix(rows))
    elif args.cmd == "escapes":
        log_file = Path(args.factory_log)
        if not log_file.is_file():
            print(f"No factory log at {log_file} — nothing to intake.")
            return 0
        rows = escapes_view(log_file, args.validation_dir, gate=args.gate)
        if out_json:
            print(json.dumps({"report_only": True, "escapes": rows}, indent=2))
        else:
            print(render_escapes(rows))
    elif args.cmd == "extract-seeds":
        gates = [args.gate] if args.gate else gates_with_records(args.validation_dir)
        for g in gates:
            try:
                path = extract_seeds(g, args.validation_dir)
                print(f"extracted {path}")
            except FileNotFoundError as exc:
                print(f"skipped {g}: {exc}")
    elif args.cmd == "mutate":
        result = mutate_gate(args.gate, scripts_dir=args.scripts_dir,
                             tests_path=args.tests, max_mutants=args.max_mutants,
                             per_mutant_seconds=args.per_mutant_seconds)
        if out_json:
            print(json.dumps({"report_only": True, "gate": args.gate, **result}, indent=2))
        else:
            print(render_mutate(args.gate, result))
    return 0  # report-only ALWAYS: the designer proposes, never blocks


if __name__ == "__main__":
    sys.exit(main())

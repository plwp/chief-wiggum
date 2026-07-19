"""Suspect-link propagation sidecar + JUSTIFIED waiver records (#169).

Two cheap, high-value steals from safety-engineering tooling into the existing
single-repo trace graph (``check_traceability.py`` / ``docs/traceability.md``):

1. **Suspect propagation** (doorstop pattern). Every ``@cw-trace`` link records
   the definition hash of the ID it was verified against, captured at the time
   the link last passed review/gates — the exact contract-block hash
   ``ratchet.py`` already computes to detect weakening
   (``chief_wiggum.hashing.hash_epic_definitions``). Link-hash records live in
   a generated sidecar, ``docs/quality/trace-links.json``, written by
   ``check_traceability.py`` when its gate passes (never hand-maintained).
   When a contract/invariant definition changes, every link recorded against
   the OLD hash is SUSPECT: "code claims to guard CTR-X but CTR-X changed
   since that claim was validated" — reported distinctly from dangling (the
   target still exists, it just changed) or uncovered (a link DOES exist, it's
   just stale). Suspect is report-only initially (see docs/gate-rollout.md).

2. **JUSTIFIED waivers** (LOBSTER pattern). An uncovered/untested contract may
   carry a committed justification record — ``reason``, ``approver``,
   ``expiry``, and a required ``ticket`` ref (ticket-every-deferral: a
   justification without one is invalid, not silently accepted) — rendering
   as JUSTIFIED (distinct from both OK and a gap) and satisfying coverage
   without lying about it. Records live under
   ``docs/epics/<slug>/justifications/*.json``, one file per waiver, diffable.
   Mirrors ``check_infra_writer.py``'s ``Exemption`` / ``check_budget_tree.py``'s
   ``evidence: justified`` — same idiom, applied to the trace graph.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

# Sidecar location, relative to the target repo root (alongside the ratchet's
# own docs/quality/ state — see docs/ratchet.md).
SIDECAR_RELPATH = "docs/quality/trace-links.json"

# Justification records live under this subdirectory of an epic dir. It MUST
# be excluded from every epic-doc ID/hash scanner (extract_defined_ids,
# hash_epic_definitions, extract_coverage_requirements, scan_epic_annotations):
# a waiver record's own "id" field (the CTR/INV it waives) would otherwise be
# misread as a NEW stable-ID *declaration*, phantom-defining IDs that were
# never actually declared in the epic's contracts/invariants.
JUSTIFICATIONS_DIRNAME = "justifications"


def is_justification_path(root: str | Path, path: str | Path) -> bool:
    """True if ``path`` (under epic dir ``root``) lives inside the
    justifications/ subtree and must be excluded from epic-doc ID scanning."""
    try:
        rel_parts = Path(path).resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        rel_parts = Path(path).parts
    return JUSTIFICATIONS_DIRNAME in rel_parts


# ---- link-hash sidecar ---------------------------------------------------------


def _link_sort_key(link: dict) -> tuple:
    return (link.get("file", ""), link.get("line", 0), link.get("verb", ""), link.get("target", ""))


def build_sidecar(annotations, definition_hashes: dict, *, scanner_version: str | None = None) -> dict:
    """Build the trace-links.json sidecar body from the CURRENT scan.

    One record per annotation whose target has a known definition hash — an
    annotation targeting an undefined ID (dangling) has no hash to record and
    is excluded; it stays dangling's problem, not suspect's. Deterministically
    ordered so the sidecar diffs cleanly when committed.
    """
    links = []
    for ann in annotations:
        h = definition_hashes.get(ann.target)
        if h is None:
            continue
        links.append({
            "verb": ann.verb,
            "target": ann.target,
            "file": ann.file,
            "line": ann.line,
            "source_kind": ann.source_kind,
            "definition_hash": h,
        })
    links.sort(key=_link_sort_key)
    return {"scanner_version": scanner_version, "links": links}


def load_sidecar(path: str | Path) -> dict:
    """Load a previously-written sidecar. Missing/malformed degrades to empty —
    the first validation run simply has nothing to compare against yet."""
    p = Path(path)
    if not p.is_file():
        return {"links": []}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"links": []}
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        return {"links": []}
    return data


def write_sidecar(path: str | Path, body: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def find_suspect_links(sidecar: dict, current_hashes: dict) -> list[dict]:
    """Links recorded in ``sidecar`` whose target's CURRENT definition hash no
    longer matches the hash recorded when the link was last validated.

    A target that has disappeared entirely (no longer defined at all) is NOT
    suspect here — that's dangling's job. Suspect means "the definition
    changed", not "the definition is gone".
    """
    out = []
    for rec in sidecar.get("links", []):
        target = rec.get("target")
        recorded_hash = rec.get("definition_hash")
        current_hash = current_hashes.get(target)
        if current_hash is None:
            continue
        if current_hash != recorded_hash:
            out.append({**rec, "current_hash": current_hash})
    return sorted(out, key=_link_sort_key)


# ---- JUSTIFIED waivers ----------------------------------------------------------


_REQUIRED_JUSTIFICATION_FIELDS = ("id", "reason", "approver", "expiry", "ticket")


@dataclass
class Justification:
    id: str
    reason: str
    approver: str
    expiry: str  # ISO date (YYYY-MM-DD)
    ticket: str  # required — a justification without a ticket ref is invalid
    source: str  # file path, for reporting

    def is_expired(self, today: date) -> bool:
        try:
            return date.fromisoformat(self.expiry) < today
        except ValueError:
            return True  # unparseable expiry treated as expired, not a silent pass

    def to_dict(self) -> dict:
        return asdict(self)


def load_justifications(epic_dir: str | Path) -> tuple[dict[str, Justification], list[dict]]:
    """Load committed waiver records from ``<epic_dir>/justifications/*.json``.

    Missing directory degrades gracefully (no waivers declared). A malformed
    record (not an object, missing a required field, or — per the
    ticket-every-deferral doctrine — missing its ``ticket`` ref) is reported as
    invalid, never silently treated as a valid waiver.
    """
    root = Path(epic_dir) / "justifications"
    justifications: dict[str, Justification] = {}
    invalid: list[dict] = []
    if not root.is_dir():
        return justifications, invalid
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append({"source": str(path), "reason": f"cannot parse justification: {exc}"})
            continue
        if not isinstance(data, dict):
            invalid.append({"source": str(path), "reason": "justification must be a JSON object"})
            continue
        missing = [k for k in _REQUIRED_JUSTIFICATION_FIELDS if not data.get(k)]
        if missing:
            invalid.append({
                "source": str(path),
                "reason": f"missing field(s): {', '.join(missing)}",
            })
            continue
        jid = str(data["id"])
        justifications[jid] = Justification(
            id=jid,
            reason=str(data["reason"]),
            approver=str(data["approver"]),
            expiry=str(data["expiry"]),
            ticket=str(data["ticket"]),
            source=str(path),
        )
    return justifications, invalid

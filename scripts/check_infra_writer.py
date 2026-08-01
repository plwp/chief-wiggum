#!/usr/bin/env python3
"""Infra single-writer checker (#165): terraform drift as sanctioned-writer enforcement.

Extends the single-writer idiom (``check_single_writer.py``) to infrastructure.
"Terraform owns env/secrets; CI only pushes images" is a memory-file rule until
it's declared and checked mechanically. An **infra invariant** names a
``controls_field`` (e.g. ``infra.env-secrets``), its ``sanctioned_writers``
(e.g. ``["terraform"]``), and the ``terraform_root`` whose declared state must
match live state.

Pilot incident this targets: the Dogeared deploy's ``enable_cicd`` footgun — a
CI run that silently applied infra changes out-of-band, bypassing the
terraform-owns-infra contract. Nothing flagged it because no check inventories
*live* infra writers the way ``check_single_writer.py`` inventories *code*
writers of a field.

How it works:

1. Load declared infra invariants from a JSON config (default
   ``docs/system/infra-invariants.json``)::

       [{"id": "INV-infra-001",
         "controls_field": "infra.env-secrets",
         "sanctioned_writers": ["terraform"],
         "terraform_root": "infra/",
         "schedule_note": "run nightly via cron"}]

2. For each invariant, run ``terraform plan -detailed-exitcode`` (subprocess,
   ``-input=false -lock=false -no-color``) in its ``terraform_root``:

   - exit ``0``  -> clean, declared state matches live state.
   - exit ``2``  -> DRIFT: an unsanctioned write happened out-of-band.
   - exit ``1``  -> terraform ERROR (config/auth/network) — reported as
     ``error``, never conflated with drift.
   - ``terraform`` missing entirely -> ``{"available": false, ...}``, graceful
     degradation, exit 0 (mirrors ``lsp_query.py``'s missing-LSP-server path).

3. **Drift is an event, not just a state.** Every detected drift (exempted or
   not) appends an append-only JSONL record to ``docs/quality/infra-drift.jsonl``
   — ``{ts, invariant, root, plan_summary_first_40_lines}``. A later clean plan
   does NOT erase the journal entry; convergence is not innocence.

4. **Break-glass = committed exemption records.** ``docs/system/exemptions/*.json``
   — ``{scope, reason, expiry, approver, incident_ref}``. An ACTIVE exemption
   (``scope`` matches the invariant's ``controls_field`` and ``expiry`` hasn't
   passed) downgrades a drift finding to ``exempted`` (still journaled). An
   EXPIRED exemption is itself a finding — the break-glass window closed and
   nobody re-declared or cleaned it up.

Authority boundary (always stated in the report): this proves declared state
matches live state **at scan time**, for **scanned roots**; it does not prove
no out-of-band write occurred *between* scans (audit-log integration is a
deferred trigger item, same caveat ``docs/single-writer.md`` states for the
code-level checker's regex lens).

Report-only by default (prints findings, exit 0); ``--gate`` hard-fails (exit 1)
on any unexempted drift, expired exemption, ERROR (terraform exit 1, a missing
or repo-escaping ``terraform_root``, a failed journal write), or MALFORMED
declaration — a blocking gate must fail when it could not actually evaluate the
invariant. The single graceful-degradation exception is terraform not being
installed at all. Per ``docs/gate-rollout.md``, validate report-only on a real
repo before wiring ``--gate`` into a workflow.

Path resolution: each ``terraform_root`` is resolved relative to the **repo
root** — an explicit ``--repo``, else the nearest ancestor of the config file
containing ``.git``, else the config file's directory — never the caller's CWD.
Roots that escape that boundary (absolute paths, ``..``) are rejected. The
drift journal likewise lands in the target repo's ``docs/quality/``, not CWD.

Exit codes: 0 = ok / report-only, 1 = gate violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_CONFIG = Path("docs/system/infra-invariants.json")
# Repo-relative: resolved against the target repo's root (see _find_repo_root),
# never the caller's CWD.
DEFAULT_JOURNAL_REL = Path("docs/quality/infra-drift.jsonl")

AUTHORITY = (
    "proves declared state matches live state at scan time for scanned roots; "
    "does not prove no out-of-band write occurred between scans"
)

TERRAFORM_PLAN_ARGS = ["terraform", "plan", "-detailed-exitcode", "-input=false", "-lock=false", "-no-color"]

# Same id-body shape as check_single_writer.py / check_traceability.py, anchored
# to validate a whole id field (not embedded in prose): INV-<slug>-<NNN>.
_LOCAL_ID_BODY = r"INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}"

# Optional/guarded: use the shared id-body grammar (single source of truth for
# the stable-ID shape, #166) if it exists on this branch, else fall back to the
# local regex above. Keeps this checker working standalone off a `main` that
# predates scripts/chief_wiggum/trace_ids.py, while adopting the shared grammar
# automatically once it lands — we still only accept the INV kind here (infra
# invariants are INV-only), just delegate the <slug>-<NNN> suffix shape.
try:
    from chief_wiggum.trace_ids import ID_BODY as _SHARED_ID_BODY  # type: ignore
except ImportError:
    _SHARED_ID_BODY = None

_VALID_INV_ID_RE = re.compile(rf"^{_SHARED_ID_BODY or _LOCAL_ID_BODY}$", re.IGNORECASE)


def _valid_inv_id(node_id: str) -> bool:
    # The shared ID_BODY matches ANY declared kind (BR|CTR|INV|ARC|...); infra
    # invariants must specifically be INV, so require that prefix ourselves.
    return node_id.upper().startswith("INV-") and bool(_VALID_INV_ID_RE.match(node_id))


# --- data model --------------------------------------------------------------


@dataclass
class InfraInvariant:
    id: str
    controls_field: str
    sanctioned_writers: list[str]
    terraform_root: str
    schedule_note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Exemption:
    scope: str
    reason: str
    expiry: str  # ISO date (YYYY-MM-DD)
    approver: str
    incident_ref: str
    source: str  # file path, for reporting

    def is_expired(self, today: date) -> bool:
        try:
            return date.fromisoformat(self.expiry) < today
        except ValueError:
            return True  # unparseable expiry is treated as expired (a finding, not a pass)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InfraDriftReport:
    available: bool = True
    config: str = ""
    checked: list[dict] = field(default_factory=list)     # every invariant that ran cleanly
    drift: list[dict] = field(default_factory=list)        # unexempted drift (violations)
    exempted: list[dict] = field(default_factory=list)     # drift downgraded by an active exemption
    errors: list[dict] = field(default_factory=list)       # terraform errors / missing roots
    malformed: list[dict] = field(default_factory=list)    # bad invariant/exemption declarations
    expired_exemptions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    authority: str = AUTHORITY

    @property
    def counts(self) -> dict:
        return {
            "checked": len(self.checked),
            "drift": len(self.drift),
            "exempted": len(self.exempted),
            "errors": len(self.errors),
            "malformed": len(self.malformed),
            "expired_exemptions": len(self.expired_exemptions),
        }

    @property
    def gate_ok(self) -> bool:
        # --gate hard-fails on:
        #   - unexempted drift;
        #   - an expired (lapsed) exemption — the break-glass window closed and
        #     nobody re-declared or cleaned it up;
        #   - any ERROR (terraform exit 1, missing/escaping root, journal write
        #     failure) or MALFORMED declaration — a blocking gate must fail when
        #     it could not actually evaluate the invariant, otherwise "terraform
        #     is broken" silently reads as "no drift".
        # The single graceful-degradation exception is terraform not being
        # installed at all (available=False): that path returns before any
        # findings exist, by design (rollout behavior, mirrors lsp_query.py).
        return not (self.drift or self.expired_exemptions or self.errors or self.malformed)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "config": self.config,
            "counts": self.counts,
            "gate_ok": self.gate_ok,
            "checked": self.checked,
            "drift": self.drift,
            "exempted": self.exempted,
            "errors": self.errors,
            "malformed": self.malformed,
            "expired_exemptions": self.expired_exemptions,
            "warnings": self.warnings,
            "authority": self.authority,
        }


# --- parsing declarations ----------------------------------------------------


def _parse_invariants(raw: list) -> tuple[list[InfraInvariant], list[dict]]:
    invariants: list[InfraInvariant] = []
    malformed: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            malformed.append({"index": i, "reason": "entry is not a JSON object"})
            continue
        node_id = str(entry.get("id", ""))
        controls_field = entry.get("controls_field")
        sanctioned_writers = entry.get("sanctioned_writers")
        terraform_root = entry.get("terraform_root")

        reasons: list[str] = []
        if not _valid_inv_id(node_id):
            reasons.append(f"invalid or missing id {node_id!r} (expected INV-<slug>-<NNN>)")
        if not controls_field or not isinstance(controls_field, str):
            reasons.append("controls_field must be a non-empty string")
        if not sanctioned_writers or not isinstance(sanctioned_writers, list):
            reasons.append("sanctioned_writers must be a non-empty array")
        if not terraform_root or not isinstance(terraform_root, str):
            reasons.append("terraform_root must be a non-empty string")
        if reasons:
            malformed.append({"id": node_id or f"<index {i}>", "reason": "; ".join(reasons)})
            continue

        invariants.append(InfraInvariant(
            id=node_id,
            controls_field=str(controls_field),
            sanctioned_writers=[str(w) for w in sanctioned_writers],
            terraform_root=str(terraform_root),
            schedule_note=(str(entry["schedule_note"]) if entry.get("schedule_note") else None),
        ))
    return invariants, malformed


def load_invariants(config_path: str | Path) -> tuple[list[InfraInvariant], list[dict]]:
    path = Path(config_path)
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"config {path} must be a JSON array of invariants")
    return _parse_invariants(raw)


# --- exemptions ---------------------------------------------------------------


_REQUIRED_EXEMPTION_FIELDS = ("scope", "reason", "expiry", "approver", "incident_ref")


def load_exemptions(exemptions_dir: str | Path) -> tuple[list[Exemption], list[dict]]:
    """Load committed break-glass exemption records from ``exemptions_dir/*.json``.

    Missing directory degrades gracefully (no exemptions declared). A malformed
    record (not an object, or missing a required field) is reported, not raised.
    """
    root = Path(exemptions_dir)
    exemptions: list[Exemption] = []
    malformed: list[dict] = []
    if not root.exists():
        return exemptions, malformed
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            malformed.append({"source": str(path), "reason": f"cannot parse exemption: {exc}"})
            continue
        if not isinstance(data, dict):
            malformed.append({"source": str(path), "reason": "exemption must be a JSON object"})
            continue
        missing = [k for k in _REQUIRED_EXEMPTION_FIELDS if not data.get(k)]
        if missing:
            malformed.append({"source": str(path), "reason": f"missing field(s): {', '.join(missing)}"})
            continue
        exemptions.append(Exemption(
            scope=str(data["scope"]),
            reason=str(data["reason"]),
            expiry=str(data["expiry"]),
            approver=str(data["approver"]),
            incident_ref=str(data["incident_ref"]),
            source=str(path),
        ))
    return exemptions, malformed


def _find_active_exemption(exemptions: list[Exemption], scope: str, today: date) -> Exemption | None:
    for exemption in exemptions:
        if exemption.scope == scope and not exemption.is_expired(today):
            return exemption
    return None


# --- journal (drift is an event) ----------------------------------------------


def append_drift_journal(journal_path: str | Path, invariant_id: str, root: str, plan_output: str) -> None:
    """Append-only JSONL record — a later clean plan does NOT erase this entry."""
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "invariant": invariant_id,
        "root": root,
        "plan_summary_first_40_lines": plan_output.splitlines()[:40],
    }
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


# --- path resolution -----------------------------------------------------------


def _find_repo_root(config_path: Path) -> Path:
    """The repo root that ``terraform_root`` entries and the drift journal
    resolve against: the nearest ancestor of the config file containing
    ``.git``, else the config file's own directory. Never the caller's CWD —
    the check must behave identically wherever it is invoked from."""
    resolved = config_path.resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return parent
    return resolved.parent


def _resolve_terraform_root(repo_root: Path, declared: str) -> Path | None:
    """Resolve a declared ``terraform_root`` inside ``repo_root``.

    Returns ``None`` for roots that escape the repo boundary — absolute paths
    and ``..`` traversal are rejected: the config is committed data, and a
    declared root must not be able to point the scanner (or the journal)
    outside the repo it lives in.
    """
    declared_path = Path(declared)
    if declared_path.is_absolute():
        return None
    base = repo_root.resolve()
    resolved = (base / declared_path).resolve()
    if not resolved.is_relative_to(base):
        return None
    return resolved


# --- running terraform ---------------------------------------------------------


def _default_runner(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        TERRAFORM_PLAN_ARGS,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=300,
    )


Runner = Callable[[Path], subprocess.CompletedProcess]


def terraform_available() -> bool:
    return shutil.which("terraform") is not None


# --- top-level check -----------------------------------------------------------


def check(
    config_path: str | Path = DEFAULT_CONFIG,
    *,
    repo_root: str | Path | None = None,
    exemptions_dir: str | Path | None = None,
    journal_path: str | Path | None = None,
    runner: Runner | None = None,
    today: date | None = None,
    available: bool | None = None,
) -> InfraDriftReport:
    """Run the infra single-writer check.

    ``repo_root`` (CLI ``--repo``) overrides the boundary that ``terraform_root``
    entries and the drift journal resolve against; by default it is derived from
    the config file's location (see ``_find_repo_root``) — never the caller's CWD.
    ``runner``/``today``/``available`` are injectable seams for tests (mock
    subprocess, freeze the exemption-expiry clock, force the missing-terraform
    path) — production callers leave them ``None`` and get the real behavior.
    """
    config_path = Path(config_path)
    report = InfraDriftReport(config=str(config_path))

    is_available = terraform_available() if available is None else available
    report.available = is_available
    if not is_available:
        # The ONE graceful-degradation path (intended rollout behavior, mirrors
        # lsp_query.py's missing-server path): terraform isn't installed here,
        # so nothing was evaluated and nothing gates. Every other failure to
        # evaluate below is an error/malformed finding and DOES gate.
        report.warnings.append("terraform not installed; skipping infra-writer check (graceful degradation)")
        return report

    if not config_path.exists():
        report.warnings.append(f"no infra invariants declared (config not found: {config_path})")
        return report

    # From here on the repo HAS declared infra invariants — failing to read or
    # understand the declaration means the gate could not evaluate them, which
    # must be a gate-failing finding (malformed), not a warning.
    try:
        raw = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        report.malformed.append({"source": str(config_path), "reason": f"cannot parse config: {exc}"})
        return report
    if not isinstance(raw, list):
        report.malformed.append({
            "source": str(config_path),
            "reason": "config must be a JSON array of invariants",
        })
        return report

    invariants, malformed = _parse_invariants(raw)
    report.malformed = malformed
    if not invariants:
        if not raw:
            report.warnings.append("config declares no invariants; nothing to check")
        return report

    base_root = Path(repo_root).resolve() if repo_root is not None else _find_repo_root(config_path)

    exemptions_root = Path(exemptions_dir) if exemptions_dir is not None else config_path.parent / "exemptions"
    exemptions, exemption_malformed = load_exemptions(exemptions_root)
    report.malformed += exemption_malformed

    check_today = today or date.today()
    report.expired_exemptions = [
        {**e.to_dict(), "reason": "exemption expired"} for e in exemptions if e.is_expired(check_today)
    ]

    # The journal lands in the target repo's docs/quality/, not the caller's CWD.
    journal = Path(journal_path) if journal_path is not None else base_root / DEFAULT_JOURNAL_REL
    run = runner or _default_runner

    for inv in invariants:
        base = {
            "invariant_id": inv.id,
            "controls_field": inv.controls_field,
            "terraform_root": inv.terraform_root,
        }

        root = _resolve_terraform_root(base_root, inv.terraform_root)
        if root is None:
            report.errors.append({
                **base, "status": "error",
                "reason": f"terraform_root escapes repo root {base_root} "
                          "(absolute paths and .. traversal are rejected)",
            })
            continue
        if not root.is_dir():
            report.errors.append({
                **base, "status": "error",
                "reason": f"terraform_root not found: {root}",
            })
            continue

        try:
            proc = run(root)
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.errors.append({
                **base, "status": "error",
                "reason": f"terraform plan failed to run: {exc}",
            })
            continue

        exit_code = proc.returncode
        stdout = proc.stdout or ""
        base = {**base, "sanctioned_writers": inv.sanctioned_writers, "exit_code": exit_code}

        if exit_code == 0:
            report.checked.append({**base, "status": "clean"})
        elif exit_code == 2:
            # DRIFT: an unsanctioned write happened out-of-band. Record the
            # FINDING first — a journal-write failure must never swallow it.
            plan_excerpt = stdout.splitlines()[:10]
            active = _find_active_exemption(exemptions, inv.controls_field, check_today)
            if active:
                report.exempted.append({
                    **base, "status": "exempted", "plan_excerpt": plan_excerpt,
                    "exemption": active.to_dict(),
                })
            else:
                report.drift.append({**base, "status": "drift", "plan_excerpt": plan_excerpt})
            # Drift is an EVENT — journal it so a later clean plan (convergence)
            # never erases the evidence. A failed write is an explicit error
            # finding (gate-failing): silently losing the event would defeat
            # the append-only journal's purpose. It must not crash report-only
            # mode either — the full report still renders.
            try:
                append_drift_journal(journal, inv.id, inv.terraform_root, stdout)
            except OSError as exc:
                report.errors.append({
                    **base, "status": "error",
                    "reason": f"drift detected but journal write failed ({journal}): {exc}",
                })
        elif exit_code == 1:
            # terraform ERROR — never conflate with drift.
            report.errors.append({
                **base, "status": "error",
                "reason": (proc.stderr or "terraform plan exited 1").strip()[:2000],
            })
        else:
            report.errors.append({
                **base, "status": "error",
                "reason": f"unexpected terraform exit code {exit_code}",
            })

    return report


# --- rendering / CLI -----------------------------------------------------------


def render_text(report: InfraDriftReport) -> str:
    c = report.counts
    lines = [
        "# Infra Single-Writer Audit (terraform drift)",
        "",
        f"Authority: {report.authority}",
        "",
    ]
    if not report.available:
        lines.append("terraform: NOT AVAILABLE — check skipped (graceful degradation)")
        lines += [f"- {w}" for w in report.warnings]
        return "\n".join(lines) + "\n"

    lines.append(f"Config: {report.config}")
    lines.append(
        f"Checked: {c['checked']}  |  Drift: {c['drift']}  |  Exempted: {c['exempted']}  |  "
        f"Errors: {c['errors']}  |  Malformed: {c['malformed']}  |  Expired exemptions: {c['expired_exemptions']}"
    )
    lines.append(f"Gate: {'OK' if report.gate_ok else 'FINDINGS'}")

    if report.drift:
        lines += ["", "## Drift (unsanctioned out-of-band write)", ""]
        for d in report.drift:
            lines.append(f"- {d['invariant_id']} `{d['controls_field']}` root={d['terraform_root']}")
            lines += [f"    {ln}" for ln in d.get("plan_excerpt", [])]
    if report.exempted:
        lines += ["", "## Exempted drift (active break-glass exemption)", ""]
        for d in report.exempted:
            ex = d["exemption"]
            lines.append(
                f"- {d['invariant_id']} `{d['controls_field']}` root={d['terraform_root']} "
                f"exempted by {ex['source']} (expiry {ex['expiry']}, incident {ex['incident_ref']})"
            )
    if report.expired_exemptions:
        lines += ["", "## Expired exemptions (findings on their own)", ""]
        for e in report.expired_exemptions:
            lines.append(f"- {e['source']} scope=`{e['scope']}` expired {e['expiry']} (approver {e['approver']})")
    if report.errors:
        lines += ["", "## Errors (could not evaluate — gate-failing, but not drift)", ""]
        for e in report.errors:
            lines.append(f"- {e['invariant_id']} root={e['terraform_root']}: {e.get('reason', '')}")
    if report.malformed:
        lines += ["", "## Malformed declarations", ""]
        for m in report.malformed:
            lines.append(f"- {m.get('id', m.get('source', '?'))}: {m['reason']}")
    if report.checked and not report.drift:
        lines += ["", "## Clean", ""]
        for chk in report.checked:
            lines.append(f"- {chk['invariant_id']} `{chk['controls_field']}` root={chk['terraform_root']}: clean")
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Infra single-writer checker — terraform drift as sanctioned-writer enforcement"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to infra-invariants JSON config")
    parser.add_argument(
        "--repo",
        help="Target repo root that terraform_root entries and the drift journal resolve "
        "against (default: derived from the config file's location, never CWD)",
    )
    parser.add_argument(
        "--gate", action="store_true",
        help="Fail (exit 1) on unexempted drift, an expired exemption, or any error/malformed "
        "finding that prevented evaluating an invariant (terraform exit 1, missing/escaping "
        "root, failed journal write, bad declaration); default is report-only. Terraform not "
        "being installed at all remains graceful degradation (exit 0)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    report = check(args.config, repo_root=args.repo)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        caught = (len(report.drift) + len(report.expired_exemptions)
                  + len(report.errors) + len(report.malformed))
        emit_gate(
            "check_infra_writer",
            "fail" if caught else "pass",
            caught=caught,
            repo=os.path.basename(os.getcwd()),
        )
    except Exception:
        pass

    if args.gate and not report.gate_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

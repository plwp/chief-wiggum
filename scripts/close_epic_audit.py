#!/usr/bin/env python3
"""Close-epic audit orchestrator (P2-14).

`/close-epic` is mostly deterministic audit coordination: traceability coverage,
transition-map verification, unresolved markers, stitch audit, mutation-tooling
availability, and integration tests. The exploratory parts still launch agents,
but the audit *state* should be structured. This runner coordinates the existing
helpers, decides the workflow-level stop condition, and emits a manifest +
report.

Every sub-audit is injectable so the orchestration is testable without running
real tools.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_unresolved  # noqa: E402
from chief_wiggum import traceability as tr  # noqa: E402
from chief_wiggum import verification as ver  # noqa: E402

DEFAULT_MUTATION_TOOLS = ("mutmut", "cosmic-ray", "stryker")


@dataclass
class CloseEpicManifest:
    epic_dir: str
    target_repo: str
    traceability: dict | None = None
    unresolved: list[dict] = field(default_factory=list)
    blocked_tickets: list[int] = field(default_factory=list)
    transitions: dict | None = None
    stitch: dict | None = None
    mutation_tools_available: list[str] = field(default_factory=list)
    verification: dict | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def integration_ok(self) -> bool:
        # Missing verification is treated as "not run" rather than a pass.
        return bool(self.verification and self.verification.get("ok"))

    @property
    def blocked(self) -> bool:
        """Workflow-level stop condition: integration tests must pass and there
        must be no unresolved-marker-blocked tickets."""
        return (not self.integration_ok) or bool(self.blocked_tickets)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["blocked"] = self.blocked
        data["integration_ok"] = self.integration_ok
        return data

    def render_markdown(self) -> str:
        lines = ["# Close-Epic Audit", "", f"Epic: `{self.epic_dir}`", ""]
        status = "BLOCKED" if self.blocked else "ready to close"
        lines.append(f"**Status: {status}**")

        if self.traceability is not None:
            t = self.traceability
            lines += ["", "## Traceability", "", f"- Coverage: {t.get('coverage_pct')}% ({t.get('covered')}/{t.get('total')})"]
            if t.get("gaps"):
                lines.append(f"- Gaps: {len(t['gaps'])}")
        if self.blocked_tickets:
            lines += ["", "## Unresolved markers (blocking)", "", ", ".join(f"#{t}" for t in self.blocked_tickets)]
        elif self.unresolved:
            lines += ["", f"## Unresolved markers: {len(self.unresolved)} (non-blocking)"]
        if self.transitions is not None:
            lines += ["", "## Transition map", "", f"- {self.transitions.get('summary', 'see transition-map')}"]
        if self.stitch is not None:
            lines += ["", "## Stitch audit", "", f"- findings: {self.stitch.get('count', 0)}"]
        lines += ["", "## Mutation tooling", "", (", ".join(self.mutation_tools_available) or "none available")]
        if self.verification is not None:
            lines += ["", "## Integration / tests", "", f"- {'green' if self.integration_ok else 'FAILED — stop condition'}"]
        if self.warnings:
            lines += ["", "## Warnings", ""] + [f"- {w}" for w in self.warnings]
        return "\n".join(lines) + "\n"


def run_close_epic_audit(
    epic_dir: str | Path,
    target_repo: str | Path,
    *,
    scanner: Callable = check_unresolved.scan,
    blocked_fn: Callable = check_unresolved.blocked_tickets,
    verify_fn: Callable[[Path], dict] | None = None,
    transition_fn: Callable[[Path, Path], dict] | None = None,
    stitch_fn: Callable[[Path], dict] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    mutation_tools: tuple[str, ...] = DEFAULT_MUTATION_TOOLS,
) -> CloseEpicManifest:
    """Coordinate the deterministic close-epic audits into one manifest."""
    epic = Path(epic_dir)
    target = Path(target_repo)
    manifest = CloseEpicManifest(epic_dir=str(epic), target_repo=str(target))

    # Traceability coverage.
    trace_file = epic / "traceability.md"
    if trace_file.is_file():
        matrix = tr.parse_matrix(trace_file.read_text())
        manifest.traceability = tr.audit(matrix)
    else:
        manifest.warnings.append("no traceability.md found")

    # Unresolved markers + blocked tickets.
    if epic.exists():
        try:
            findings = scanner([epic])
            manifest.unresolved = [
                f.__dict__ if hasattr(f, "__dict__") else dict(f) for f in findings
            ]
            blocked = blocked_fn(findings)
            parsed = []
            for ref in blocked:
                try:
                    parsed.append(int(str(ref).lstrip("#")))
                except (ValueError, TypeError):
                    manifest.warnings.append(f"unparseable blocked ticket ref: {ref!r}")
            manifest.blocked_tickets = sorted(set(parsed))
        except Exception as exc:  # noqa: BLE001
            manifest.warnings.append(f"unresolved scan failed: {exc}")

    # Transition-map audit (only when a state machine model exists).
    sm = epic / "models" / "state-machines.json"
    if sm.is_file() and transition_fn is not None:
        try:
            manifest.transitions = transition_fn(target, sm)
        except Exception as exc:  # noqa: BLE001
            manifest.warnings.append(f"transition audit failed: {exc}")
    elif not sm.is_file():
        manifest.warnings.append("no formal state-machine model; skipping transition audit")

    # Optional stitch audit.
    if stitch_fn is not None:
        try:
            manifest.stitch = stitch_fn(target)
        except Exception as exc:  # noqa: BLE001
            manifest.warnings.append(f"stitch audit failed: {exc}")

    # Mutation tooling availability.
    manifest.mutation_tools_available = [t for t in mutation_tools if which(t)]
    if not manifest.mutation_tools_available:
        manifest.warnings.append("no mutation-testing tool available (mutmut/cosmic-ray/stryker)")

    # Integration tests — the workflow-level stop condition.
    if verify_fn is None:
        def verify_fn(repo: Path) -> dict:
            return ver.verify(repo, ["test"]).to_dict()
    try:
        manifest.verification = verify_fn(target)
    except Exception as exc:  # noqa: BLE001
        manifest.warnings.append(f"verification failed to run: {exc}")

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close-epic audit orchestrator")
    parser.add_argument("--epic-dir", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--output-dir", help="Write manifest + report here")
    parser.add_argument("--markdown", action="store_true", help="Print the markdown report")
    args = parser.parse_args(argv)

    def transition_fn(target: Path, sm: Path) -> dict:
        home = Path(__file__).resolve().parent
        out = subprocess.run(
            ["python3", str(home / "verify_transitions.py"), str(target), str(sm), "--format", "json"],
            capture_output=True, text=True, timeout=120,
        )
        try:
            return json.loads(out.stdout or "{}")
        except json.JSONDecodeError:
            return {"summary": "transition verification produced no JSON"}

    manifest = run_close_epic_audit(args.epic_dir, args.target_repo, transition_fn=transition_fn)

    if args.output_dir:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "close-epic-manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2))
        (out / "close-epic-report.md").write_text(manifest.render_markdown())

    if args.markdown:
        print(manifest.render_markdown())
    else:
        print(json.dumps(manifest.to_dict(), indent=2))

    # Non-zero when the epic cannot be closed (integration failed or blocked tickets).
    return 1 if manifest.blocked else 0


if __name__ == "__main__":
    sys.exit(main())

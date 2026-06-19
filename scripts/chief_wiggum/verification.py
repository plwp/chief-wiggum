"""Project verification runner (P1-9).

`/implement`, `/implement-wave`, `/ship`, and `/close-epic` all repeat heuristics
for detecting how to test/lint/build/smoke a target repo (Go, Node, Python,
Docker, Playwright, Makefile). This turns that into a tested detector + command
planner that emits structured evidence (command, cwd, exit code, duration, log
tail) instead of terminal prose.

Detection and command *planning* are pure and unit-testable without executing
any build tool. Execution is a thin, injectable layer.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROFILES = ("test", "lint", "build", "smoke")

# Per-language command for each non-smoke profile.
LANG_COMMANDS: dict[str, dict[str, list[str]]] = {
    "go": {
        "test": ["go", "test", "./..."],
        "lint": ["go", "vet", "./..."],
        "build": ["go", "build", "./..."],
    },
    "node": {
        "test": ["npm", "test"],
        "lint": ["npm", "run", "lint"],
        "build": ["npm", "run", "build"],
    },
    "python": {
        "test": ["python3", "-m", "pytest"],
        "lint": ["python3", "-m", "ruff", "check", "."],
        "build": ["python3", "-m", "build"],
    },
}

# Match a make rule line (target[s] before ':'), excluding ':=' assignments.
# The captured group may hold several space-separated targets (grouped rule).
_MAKE_TARGET_LINE = re.compile(r"^([A-Za-z0-9_.\-/ ]+?):(?!=)")
_MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")


def _parse_make_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for line in text.splitlines():
        match = _MAKE_TARGET_LINE.match(line)
        if not match:
            continue
        for name in match.group(1).split():
            if not name.startswith("."):  # skip .PHONY etc.
                targets.add(name)
    return targets

Runner = Callable[[list[str], str], tuple[int, str]]
Clock = Callable[[], float]


@dataclass
class Detection:
    has_makefile: bool = False
    make_targets: tuple[str, ...] = ()
    has_go: bool = False
    has_python: bool = False
    has_node: bool = False
    node_scripts: tuple[str, ...] = ()
    has_docker_compose: bool = False
    has_playwright: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlannedStep:
    profile: str
    tool: str
    command: list[str]
    cwd: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StepEvidence:
    profile: str
    tool: str
    command: list[str]
    cwd: str
    exit_code: int | None = None
    duration_s: float | None = None
    log_tail: str = ""
    ok: bool = False
    planned_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationReport:
    repo: str
    profiles: list[str]
    detection: Detection
    steps: list[StepEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # Zero steps is NOT success: "nothing verified" must not green-light a
        # ship. A run is ok only if it executed (or planned) at least one step
        # and none failed.
        return bool(self.steps) and all(s.ok or s.planned_only for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "profiles": self.profiles,
            "detection": self.detection.to_dict(),
            "ok": self.ok,
            "steps": [s.to_dict() for s in self.steps],
            "warnings": self.warnings,
        }

    def render_markdown(self) -> str:
        lines = ["# Verification Report", "", f"Repo: `{self.repo}`", f"Profiles: {', '.join(self.profiles)}", ""]
        if not self.steps:
            lines.append("_No verification steps planned (nothing detected)._")
        for s in self.steps:
            cmd = " ".join(s.command)
            if s.planned_only:
                lines.append(f"- [plan] `{cmd}` (cwd `{s.cwd}`)")
            else:
                mark = "✓" if s.ok else "✗"
                dur = f"{s.duration_s:.1f}s" if s.duration_s is not None else "?"
                lines.append(f"- {mark} `{cmd}` — exit {s.exit_code} in {dur}")
                if not s.ok and s.log_tail:
                    lines.append("  ```")
                    lines += [f"  {ln}" for ln in s.log_tail.splitlines()[-15:]]
                    lines.append("  ```")
        if self.warnings:
            lines += ["", "## Warnings", ""] + [f"- {w}" for w in self.warnings]
        return "\n".join(lines) + "\n"


def detect_project(repo: str | Path) -> Detection:
    """Detect build tooling present in ``repo`` (pure filesystem inspection)."""
    root = Path(repo)
    det = Detection()

    makefile = next((root / n for n in _MAKEFILE_NAMES if (root / n).is_file()), None)
    if makefile is not None:
        det.has_makefile = True
        try:
            det.make_targets = tuple(sorted(_parse_make_targets(makefile.read_text())))
        except OSError:
            det.make_targets = ()

    det.has_go = (root / "go.mod").is_file()
    det.has_python = (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()

    pkg = root / "package.json"
    if pkg.is_file():
        det.has_node = True
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            det.node_scripts = tuple(sorted(scripts)) if isinstance(scripts, dict) else ()
        except (json.JSONDecodeError, OSError):
            det.node_scripts = ()

    det.has_docker_compose = any(
        (root / name).is_file()
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    )
    det.has_playwright = any(
        (root / name).is_file()
        for name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs")
    )
    return det


def plan_steps(repo: str | Path, profiles: Iterable[str], detection: Detection) -> list[PlannedStep]:
    """Plan verification commands for ``profiles`` given a detection (pure)."""
    root = str(repo)
    steps: list[PlannedStep] = []

    for profile in profiles:
        if profile == "smoke":
            if detection.has_docker_compose:
                # --wait blocks until services are healthy/running and fails if
                # they don't come up, so this is a bounded readiness check.
                steps.append(
                    PlannedStep("smoke", "docker-compose", ["docker", "compose", "up", "-d", "--wait"], root)
                )
            if detection.has_playwright:
                # --no-install runs only a locally-installed Playwright (never
                # fetches an unpinned package over the network).
                steps.append(
                    PlannedStep("smoke", "playwright", ["npx", "--no-install", "playwright", "test"], root)
                )
            continue

        # Prefer a Makefile target named exactly for the profile.
        if detection.has_makefile and profile in detection.make_targets:
            steps.append(PlannedStep(profile, "make", ["make", profile], root))
            continue

        if detection.has_go:
            steps.append(PlannedStep(profile, "go", LANG_COMMANDS["go"][profile], root))
        if detection.has_python:
            steps.append(PlannedStep(profile, "python", LANG_COMMANDS["python"][profile], root))
        if detection.has_node and profile in detection.node_scripts:
            steps.append(PlannedStep(profile, "node", LANG_COMMANDS["node"][profile], root))

    return steps


def _default_runner(command: list[str], cwd: str) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _log_tail(output: str, lines: int = 50) -> str:
    if lines <= 0:
        return ""
    return "\n".join(output.splitlines()[-lines:])


def verify(
    repo: str | Path,
    profiles: Iterable[str] = ("test",),
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
    clock: Clock | None = None,
    log_tail_lines: int = 50,
) -> VerificationReport:
    """Detect, plan, and (unless ``dry_run``) execute verification steps."""
    # Dedupe while preserving order so --profile test,test runs each step once.
    profiles = list(dict.fromkeys(profiles))
    detection = detect_project(repo)
    report = VerificationReport(repo=str(repo), profiles=profiles, detection=detection)

    planned = plan_steps(repo, profiles, detection)
    if not planned:
        report.warnings.append("no verification steps detected for the requested profiles")

    if dry_run:
        report.steps = [
            StepEvidence(p.profile, p.tool, p.command, p.cwd, planned_only=True) for p in planned
        ]
        return report

    import time

    now = clock or time.monotonic
    for step in planned:
        start = now()
        try:
            exit_code, output = runner(step.command, step.cwd)
        except Exception as exc:  # noqa: BLE001 - a missing tool shouldn't abort the run
            report.steps.append(
                StepEvidence(
                    step.profile, step.tool, step.command, step.cwd,
                    exit_code=None, duration_s=round(now() - start, 3),
                    log_tail=f"runner error: {exc}", ok=False,
                )
            )
            continue
        report.steps.append(
            StepEvidence(
                step.profile, step.tool, step.command, step.cwd,
                exit_code=exit_code, duration_s=round(now() - start, 3),
                log_tail=_log_tail(output, log_tail_lines), ok=(exit_code == 0),
            )
        )
    return report

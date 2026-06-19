#!/usr/bin/env python3
"""Install epic architecture artifacts into a target repo (P1-12).

`/architect` already has strong model generation/validation, but the final
artifact installation (validate -> create docs/epics/<slug>/ -> copy prose +
JSON -> generate machine/test views -> init transition map -> prepare issue
comment -> commit) is a long shell recipe that touches target-repo files. This
makes it one tested, reliable step.

Usage:
    python3 scripts/install_epic_artifacts.py \
      --source "$CW_TMP" --epic-dir "$EPIC_DIR" \
      --epic-name "Epic: Name" --epic-slug "$EPIC_SLUG" \
      --target-repo "$TARGET_REPO" [--no-commit] [--allow-dirty]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_models as rm  # noqa: E402
from chief_wiggum import gitops  # noqa: E402

REQUIRED_PROSE = (
    "contracts.md",
    "state-machines.md",
    "invariants.md",
    "adr.md",
    "integration-tests.md",
    "traceability.md",
)
OPTIONAL_PROSE = ("ui-spec.md",)
REQUIRED_MODELS = ("contracts.json", "state-machines.json")
OPTIONAL_MODELS = ("ui-spec.json",)

RenderFn = Callable[[Path, str, Path], list]
TransitionMapFn = Callable[[Path, Path, Path], None]
Runner = Callable[..., subprocess.CompletedProcess]


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InstallError(RuntimeError):
    """Raised when artifacts can't be installed (missing inputs, dirty repo)."""


@dataclass
class InstallResult:
    epic_dir: str
    copied: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    transition_map: str | None = None
    committed: bool = False
    issue_comment: str = ""
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_source(source: str | Path) -> list[str]:
    """Return the names of any required artifacts missing from ``source``."""
    src = Path(source)
    return [name for name in (*REQUIRED_PROSE, *REQUIRED_MODELS) if not (src / name).is_file()]


def render_issue_comment(epic_slug: str, epic_name: str, *, has_ui_spec: bool) -> str:
    lines = [
        "## Epic Architecture",
        "",
        f"Architecture artifacts for **{epic_name}** are committed under `docs/epics/{epic_slug}/`:",
        "",
        f"- [Contracts](../docs/epics/{epic_slug}/contracts.md) — REQUIRES/ENSURES for APIs and entities",
        f"- [State Machines](../docs/epics/{epic_slug}/state-machines.md) — valid transitions",
        f"- [Invariants](../docs/epics/{epic_slug}/invariants.md) — rules that must hold across all tickets",
        f"- [Integration Tests](../docs/epics/{epic_slug}/integration-tests.md) — cross-ticket specs",
        f"- [Traceability](../docs/epics/{epic_slug}/traceability.md) — which tests cover which AC",
        f"- [ADR](../docs/epics/{epic_slug}/adr.md) — architectural decisions",
    ]
    if has_ui_spec:
        lines.append(f"- [UI Spec](../docs/epics/{epic_slug}/ui-spec.md) — pages, components, interactions")
    return "\n".join(lines) + "\n"


def _default_transition_map(target_repo: Path, sm_json: Path, output: Path) -> None:
    home = Path(__file__).resolve().parent
    subprocess.run(
        [
            "python3", str(home / "verify_transitions.py"), str(target_repo), str(sm_json),
            "--output", str(output), "--format", "json",
        ],
        check=True,
        timeout=120,
    )


def install_epic_artifacts(
    source: str | Path,
    epic_dir: str | Path,
    *,
    epic_name: str,
    epic_slug: str,
    target_repo: str | Path,
    commit: bool = True,
    allow_dirty: bool = False,
    dry_run: bool = False,
    render_fn: RenderFn = rm.render_model,
    transition_map_fn: TransitionMapFn = _default_transition_map,
    git_runner: Runner = subprocess.run,
) -> InstallResult:
    """Validate, install, and optionally commit epic artifacts. Idempotent copy."""
    src = Path(source)
    target_repo = Path(target_repo).resolve()
    epic = Path(epic_dir).resolve()

    # Enforce the contract: artifacts install at <target_repo>/docs/epics/<slug>.
    # This prevents a bad/hostile --epic-dir writing outside the repo while
    # `git add docs/epics/...` stages a different path.
    if not _SLUG_RE.match(epic_slug):
        raise InstallError(f"invalid epic slug: {epic_slug!r}")
    expected = (target_repo / "docs" / "epics" / epic_slug).resolve()
    if epic != expected:
        raise InstallError(f"epic_dir must be {expected}, got {epic}")

    missing = validate_source(src)
    if missing:
        raise InstallError(f"missing required artifacts: {', '.join(missing)}")

    if commit and not dry_run and not allow_dirty:
        if not gitops.is_clean(target_repo, runner=git_runner):
            raise InstallError("target repo has uncommitted changes; pass allow_dirty to override")

    result = InstallResult(epic_dir=str(epic), dry_run=dry_run)
    models_dir = epic / "models"

    # The optional UI spec is a pair: install/link it only when BOTH the prose
    # and the model are present, so the comment can't link a spec with no model
    # (or a model be rendered but omitted from the comment).
    has_ui_spec_md = (src / "ui-spec.md").is_file()
    has_ui_spec_json = (src / "ui-spec.json").is_file()
    has_ui_spec = has_ui_spec_md and has_ui_spec_json
    if has_ui_spec_md != has_ui_spec_json:
        only = "ui-spec.md" if has_ui_spec_md else "ui-spec.json"
        result.warnings.append(f"incomplete UI spec ({only} present without its pair); skipping UI spec")
    result.issue_comment = render_issue_comment(epic_slug, epic_name, has_ui_spec=has_ui_spec)

    # Which optional artifacts to install (UI spec only when the pair is complete).
    prose_names = (*REQUIRED_PROSE, *(("ui-spec.md",) if has_ui_spec else ()))
    model_names = (*REQUIRED_MODELS, *(("ui-spec.json",) if has_ui_spec else ()))

    if dry_run:
        for name in (*prose_names, *model_names):
            if (src / name).is_file():
                result.copied.append(name)
        return result

    epic.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    # Prose artifacts.
    for name in prose_names:
        f = src / name
        if f.is_file():
            shutil.copy(f, epic / name)
            result.copied.append(name)

    # Model JSON.
    for name in model_names:
        f = src / name
        if f.is_file():
            shutil.copy(f, models_dir / name)
            result.copied.append(f"models/{name}")

    # Transition map baseline.
    sm_json = models_dir / "state-machines.json"
    if sm_json.is_file():
        out = models_dir / "transition-map.json"
        try:
            transition_map_fn(target_repo, sm_json, out)
            result.transition_map = str(out)
        except (subprocess.SubprocessError, OSError) as exc:
            result.warnings.append(f"transition map generation failed: {exc}")

    # Machine + test views for each model.
    for name in model_names:
        model_path = models_dir / name
        for view in ("machine", "test"):
            try:
                result.generated.extend(render_fn(model_path, view, models_dir))
            except Exception as exc:  # noqa: BLE001 - rendering errors shouldn't abort install
                result.warnings.append(f"render {name} ({view}) failed: {exc}")

    if commit:
        # Stage only this epic's directory, not every epic under docs/epics/.
        rel = str(epic.relative_to(target_repo))
        rc_add = git_runner(["git", "add", rel], cwd=str(target_repo), capture_output=True, text=True)
        if rc_add.returncode != 0:
            raise InstallError(f"git add failed: {(rc_add.stderr or '').strip()}")
        rc_commit = git_runner(
            ["git", "commit", "-m", f"arch: add epic architecture — {epic_name}"],
            cwd=str(target_repo), capture_output=True, text=True,
        )
        if rc_commit.returncode == 0:
            result.committed = True
        else:
            combined = ((rc_commit.stdout or "") + (rc_commit.stderr or "")).strip()
            # An idempotent rerun (artifacts already committed) is not a failure.
            if "nothing to commit" in combined:
                result.warnings.append("nothing to commit (artifacts already up to date)")
            else:
                raise InstallError(f"git commit failed: {combined}")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install epic architecture artifacts")
    parser.add_argument("--source", required=True, help="Dir holding the generated artifacts")
    parser.add_argument("--epic-dir", required=True, help="Target docs/epics/<slug> path")
    parser.add_argument("--epic-name", required=True)
    parser.add_argument("--epic-slug", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = install_epic_artifacts(
            args.source, args.epic_dir,
            epic_name=args.epic_name, epic_slug=args.epic_slug, target_repo=args.target_repo,
            commit=not args.no_commit, allow_dirty=args.allow_dirty, dry_run=args.dry_run,
        )
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Install product design artifacts into a target repo (P2-15).

`/design`'s creative direction generation stays human/agent-driven, but the
final `docs/design/` assembly (validate design.json -> render styleguide -> copy
mockups + screenshots -> verify reference-screenshot assets point at committed
files -> commit) is deterministic and should be checked. This makes it one
tested step, and reports surviving ``TBD:`` markers with their blocked
frontend-ticket impact.

Usage:
    python3 scripts/install_design_artifacts.py \
      --design-json "$DESIGN_TMP/design.json" \
      --mockups "$DESIGN_TMP/directions/<chosen>" \
      --screenshots "$DESIGN_TMP/reference" \
      --target-repo "$TARGET_REPO" [--no-commit] [--allow-dirty] [--dry-run]
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

import extract_design as ed  # noqa: E402
from chief_wiggum import gitops  # noqa: E402

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_MARKER = ("TBD", "UNRESOLVED", "PLACEHOLDER")

Runner = Callable[..., subprocess.CompletedProcess]


class DesignInstallError(RuntimeError):
    """Raised when design artifacts can't be installed."""


@dataclass
class DesignInstallResult:
    design_dir: str
    copied: list[str] = field(default_factory=list)
    styleguide: str | None = None
    broken_assets: list[str] = field(default_factory=list)
    tbd_markers: list[str] = field(default_factory=list)
    committed: bool = False
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def blocks_frontend(self) -> bool:
        return bool(self.tbd_markers)

    def to_dict(self) -> dict:
        return asdict(self)


def _find_markers(node, path: str = "") -> list[str]:
    """Collect TBD/UNRESOLVED/PLACEHOLDER markers from a design.json value tree."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_find_markers(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_find_markers(v, f"{path}[{i}]"))
    elif isinstance(node, str) and any(m in node for m in _MARKER):
        out.append(f"{path}: {node.strip()[:120]}")
    return out


def _assert_within(base: Path, child: Path) -> None:
    """Ensure ``child`` (possibly not yet created) resolves under ``base``.

    Walks to the deepest existing ancestor and checks it — catches a symlinked
    ``docs`` / ``docs/design`` that would let writes escape the target repo.
    """
    base = base.resolve()
    existing = child
    while not existing.exists():
        existing = existing.parent
    if not existing.resolve().is_relative_to(base):
        raise DesignInstallError(f"refusing to write outside the target repo: {child} -> {existing.resolve()}")


def _reference_assets(design: dict) -> list[str]:
    assets = design.get("assets") or []
    refs = []
    if isinstance(assets, list):
        for a in assets:
            if isinstance(a, dict) and a.get("type") == "reference-screenshot" and a.get("path"):
                refs.append(a["path"])
    return refs


def install_design_artifacts(
    design_json: str | Path,
    mockups_dir: str | Path,
    screenshots_dir: str | Path,
    target_repo: str | Path,
    *,
    commit: bool = True,
    allow_dirty: bool = False,
    dry_run: bool = False,
    git_runner: Runner = subprocess.run,
) -> DesignInstallResult:
    """Validate, assemble, and optionally commit ``docs/design/`` artifacts."""
    target_repo = Path(target_repo).resolve()
    design_dir = target_repo / "docs" / "design"

    # Validate the design contract first.
    try:
        design = json.loads(Path(design_json).read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise DesignInstallError(f"cannot read design.json: {exc}") from exc
    errors = ed.validate_design(design)
    if errors:
        raise DesignInstallError("invalid design.json: " + "; ".join(errors[:5]))

    if commit and not dry_run and not allow_dirty:
        try:
            clean = gitops.is_clean(target_repo, runner=git_runner)
        except gitops.GitSafetyError as exc:
            raise DesignInstallError(f"cannot check target repo state: {exc}") from exc
        if not clean:
            raise DesignInstallError("target repo has uncommitted changes; pass allow_dirty to override")

    result = DesignInstallResult(design_dir=str(design_dir), dry_run=dry_run)
    result.tbd_markers = _find_markers(design)
    if result.tbd_markers:
        result.warnings.append(
            f"{len(result.tbd_markers)} TBD marker(s) survive; dependent frontend tickets will be gated"
        )

    mockups = sorted(Path(mockups_dir).glob("*.html")) if Path(mockups_dir).is_dir() else []
    screenshots = (
        [p for p in sorted(Path(screenshots_dir).iterdir()) if p.suffix.lower() in IMAGE_EXTS]
        if Path(screenshots_dir).is_dir()
        else []
    )
    if not screenshots:
        result.warnings.append("no reference screenshots found; the design-fidelity gate baseline will be empty")
    if not mockups:
        result.warnings.append("no HTML mockups found in the chosen direction")

    # Verify every reference-screenshot asset is the exact repo-relative path of
    # a screenshot being installed — not just a basename match (which would let
    # an absolute path, URL, traversal, or wrong directory slip through).
    valid_paths = {f"docs/design/reference/{p.name}" for p in screenshots}
    for ref in _reference_assets(design):
        if ref not in valid_paths:
            result.broken_assets.append(ref)
    if result.broken_assets:
        result.warnings.append(
            f"{len(result.broken_assets)} reference-screenshot asset(s) point at files not being installed"
        )

    if dry_run:
        result.copied = (
            [f"mockups/{p.name}" for p in mockups]
            + [f"reference/{p.name}" for p in screenshots]
            + ["design.json", "styleguide.html"]
        )
        return result

    _assert_within(target_repo, design_dir)
    (design_dir / "mockups").mkdir(parents=True, exist_ok=True)
    (design_dir / "reference").mkdir(parents=True, exist_ok=True)

    shutil.copy(Path(design_json), design_dir / "design.json")
    result.copied.append("design.json")
    for p in mockups:
        shutil.copy(p, design_dir / "mockups" / p.name)
        result.copied.append(f"mockups/{p.name}")
    for p in screenshots:
        shutil.copy(p, design_dir / "reference" / p.name)
        result.copied.append(f"reference/{p.name}")

    # Render the styleguide from the (validated) tokens.
    styleguide = design_dir / "styleguide.html"
    styleguide.write_text(ed.render_styleguide(design))
    result.styleguide = str(styleguide)
    result.copied.append("styleguide.html")

    if commit:
        rc_add = git_runner(["git", "add", "docs/design"], cwd=str(target_repo), capture_output=True, text=True)
        if rc_add.returncode != 0:
            raise DesignInstallError(f"git add failed: {(rc_add.stderr or '').strip()}")
        # Reliable idempotency: if nothing under docs/design is staged, there's
        # nothing to commit (regardless of unrelated dirty files under --allow-dirty).
        staged = git_runner(
            ["git", "diff", "--cached", "--quiet", "--", "docs/design"],
            cwd=str(target_repo), capture_output=True, text=True,
        )
        if staged.returncode == 0:
            result.warnings.append("nothing to commit (design already up to date)")
        else:
            rc_commit = git_runner(
                ["git", "commit", "-m", "design: add product design contract"],
                cwd=str(target_repo), capture_output=True, text=True,
            )
            if rc_commit.returncode == 0:
                result.committed = True
            else:
                combined = ((rc_commit.stdout or "") + (rc_commit.stderr or "")).strip()
                raise DesignInstallError(f"git commit failed: {combined}")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install product design artifacts")
    parser.add_argument("--design-json", required=True)
    parser.add_argument("--mockups", required=True, help="Chosen direction dir with *.html mocks")
    parser.add_argument("--screenshots", required=True, help="Dir with approved screenshots")
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = install_design_artifacts(
            args.design_json, args.mockups, args.screenshots, args.target_repo,
            commit=not args.no_commit, allow_dirty=args.allow_dirty, dry_run=args.dry_run,
        )
    except DesignInstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Epic artifact discovery and context loading.

`/implement`, `/architect`, `/implement-wave`, and `/close-epic` all need to know
what epic context exists for a ticket and what gates apply (formal models, UI
spec, transition map, design contract, unresolved markers). Today each command
describes this in prose/shell. This module computes one structured inventory.

Side-effecting scanning (the unresolved-marker scan) is injected so the inventory
is unit-testable against a temp directory without depending on external tools.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import check_unresolved

# Prose artifacts written into docs/epics/<slug>/ by /architect and /close-epic.
EPIC_MARKDOWN = (
    "contracts.md",
    "state-machines.md",
    "invariants.md",
    "adr.md",
    "integration-tests.md",
    "traceability.md",
    "retrospective.md",
)

# Machine/test artifacts under docs/epics/<slug>/models/.
EPIC_MODELS = (
    "state-machines.json",
    "contracts.json",
    "ui-spec.json",
    "transition-map.json",
)

# Product-level design artifacts under docs/design/.
DESIGN_ARTIFACTS = (
    "design.json",
    "styleguide.html",
    "mockups",
    "reference",
)
# Design artifacts that are directories rather than files.
DESIGN_DIRS = frozenset({"mockups", "reference"})

# scanner(targets) -> list of unresolved findings; blocked_fn(findings) -> {ticket: count}
Scanner = Callable[[list[Path]], list]
BlockedFn = Callable[[list], dict]


@dataclass
class ArtifactInventory:
    target_repo: str
    epic_slug: str | None = None
    epic_dir: str | None = None
    epic_dir_exists: bool = False
    issue: int | None = None
    markdown_artifacts: dict[str, bool] = field(default_factory=dict)
    model_artifacts: dict[str, bool] = field(default_factory=dict)
    design_artifacts: dict[str, bool] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)
    blocked_tickets: list[int] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        # default=str keeps serialization robust to injected findings that may
        # carry Path or other non-JSON-native values.
        return json.dumps(self.to_dict(), indent=2, default=str)

    def render_markdown(self) -> str:
        lines = ["# Epic Artifact Inventory", ""]
        lines.append(f"- Target repo: `{self.target_repo}`")
        if self.epic_slug:
            status = "present" if self.epic_dir_exists else "missing"
            lines.append(f"- Epic: `{self.epic_slug}` ({status})")
        if self.issue is not None:
            lines.append(f"- Ticket: #{self.issue}")
        lines += ["", "## Flags", ""]
        for key in sorted(self.flags):
            lines.append(f"- {key}: {'yes' if self.flags[key] else 'no'}")
        present_md = [n for n, ok in self.markdown_artifacts.items() if ok]
        present_models = [n for n, ok in self.model_artifacts.items() if ok]
        if present_md:
            lines += ["", "## Prose artifacts", "", ", ".join(present_md)]
        if present_models:
            lines += ["", "## Model artifacts", "", ", ".join(present_models)]
        if self.blocked_tickets:
            lines += ["", "## Blocked by unresolved unknowns", ""]
            lines.append(", ".join(f"#{t}" for t in self.blocked_tickets))
        if self.warnings:
            lines += ["", "## Warnings", ""]
            lines += [f"- {w}" for w in self.warnings]
        return "\n".join(lines) + "\n"


def _presence(base: Path, names: Iterable[str], dir_names: Iterable[str] = ()) -> dict[str, bool]:
    """Map each name to whether it exists *as the right kind* (file vs dir)."""
    dir_set = set(dir_names)
    return {
        name: ((base / name).is_dir() if name in dir_set else (base / name).is_file())
        for name in names
    }


def build_inventory(
    repo_path: str | Path,
    *,
    epic_slug: str | None = None,
    issue: int | None = None,
    scanner: Scanner = check_unresolved.scan,
    blocked_fn: BlockedFn = check_unresolved.blocked_tickets,
) -> ArtifactInventory:
    """Discover epic / model / design artifacts and unresolved gates."""
    repo = Path(repo_path)
    inv = ArtifactInventory(target_repo=str(repo), epic_slug=epic_slug, issue=issue)

    epic_dir: Path | None = None
    if epic_slug:
        epic_dir = repo / "docs" / "epics" / epic_slug
        inv.epic_dir = str(epic_dir)
        inv.epic_dir_exists = epic_dir.exists()

    # Models that are present AND parse — only these drive the HAS_* flags, so a
    # malformed model can't make a downstream step read/generate from broken JSON.
    valid_models: set[str] = set()
    if epic_dir and epic_dir.exists():
        inv.markdown_artifacts = _presence(epic_dir, EPIC_MARKDOWN)
        models_dir = epic_dir / "models"
        inv.model_artifacts = _presence(models_dir, EPIC_MODELS)
        for name, present in inv.model_artifacts.items():
            if not present:
                continue
            try:
                json.loads((models_dir / name).read_text())
                valid_models.add(name)
            except (json.JSONDecodeError, OSError) as exc:
                inv.warnings.append(f"malformed model artifact {name}: {exc}")
    else:
        inv.markdown_artifacts = dict.fromkeys(EPIC_MARKDOWN, False)
        inv.model_artifacts = dict.fromkeys(EPIC_MODELS, False)
        if epic_slug:
            inv.warnings.append(f"epic directory does not exist: {inv.epic_dir}")

    design_dir = repo / "docs" / "design"
    if design_dir.exists():
        inv.design_artifacts = _presence(design_dir, DESIGN_ARTIFACTS, DESIGN_DIRS)
    else:
        inv.design_artifacts = dict.fromkeys(DESIGN_ARTIFACTS, False)

    # Unresolved-marker scan over the epic dir (models + prose).
    if epic_dir and epic_dir.exists():
        try:
            findings = scanner([epic_dir])
        except Exception as exc:  # noqa: BLE001 - never let a scan error abort discovery
            inv.warnings.append(f"unresolved scan failed: {exc}")
            findings = []
        inv.unresolved = [_finding_to_dict(f) for f in findings]
        try:
            blocked = blocked_fn(findings)
        except Exception as exc:  # noqa: BLE001
            inv.warnings.append(f"blocked-ticket computation failed: {exc}")
            blocked = {}
        # Parse each ref independently — one non-numeric ref (e.g. "AC-1") must
        # not drop the rest of the blocked tickets.
        parsed: list[int] = []
        for ref in blocked:
            try:
                parsed.append(_ticket_int(ref))
            except (ValueError, TypeError):
                inv.warnings.append(f"unparseable blocked ticket ref: {ref!r}")
        inv.blocked_tickets = sorted(set(parsed))

    inv.flags = {
        "HAS_EPIC": bool(epic_dir and epic_dir.exists()),
        "HAS_FORMAL_MODELS": "state-machines.json" in valid_models
        or "contracts.json" in valid_models,
        "HAS_UI_SPEC": "ui-spec.json" in valid_models,
        "HAS_TRANSITION_MAP": "transition-map.json" in valid_models,
        "HAS_DESIGN": inv.design_artifacts.get("design.json", False),
        "HAS_UNRESOLVED": bool(inv.unresolved),
    }
    return inv


def _finding_to_dict(finding) -> dict:
    if hasattr(finding, "__dataclass_fields__"):
        return asdict(finding)
    if isinstance(finding, dict):
        return finding
    return {"text": str(finding)}


def _ticket_int(ticket) -> int:
    return int(str(ticket).lstrip("#"))

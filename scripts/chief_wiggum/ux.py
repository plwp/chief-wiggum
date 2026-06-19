"""UX and design-fidelity mechanics (P1-10).

`/implement` Step 9 is high-value but currently long, fragile prose with inline
shell/Python. The judgment-heavy review stays with an agent, but the mechanical
parts — deciding whether a ticket is frontend, checking the ui-spec's design
binding, discovering reference screenshots, and planning screenshot capture —
should be tested code. This module produces a UX manifest the agent consumes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

# File signals that a change touches the frontend.
FRONTEND_EXTS = frozenset(
    {".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".css", ".scss", ".sass", ".less", ".html"}
)
FRONTEND_DIR_HINTS = frozenset(
    {"components", "component", "pages", "page", "views", "view", "app", "ui", "styles", "style", "frontend", "client", "web"}
)
FRONTEND_LABELS = frozenset({"frontend", "ui", "ux", "design", "css", "styling"})

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


@dataclass
class FrontendImpact:
    is_frontend: bool
    frontend_files: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def detect_frontend_impact(changed_files: list[str], labels: list[str] | None = None) -> FrontendImpact:
    """Decide whether a ticket touches the frontend from its diff paths + labels."""
    labels = [str(label).lower() for label in (labels or [])]
    frontend_files: list[str] = []
    for path in changed_files:
        p = Path(path)
        parts = {part.lower() for part in p.parts}
        if p.suffix.lower() in FRONTEND_EXTS or (parts & FRONTEND_DIR_HINTS):
            frontend_files.append(path)

    reasons: list[str] = []
    if frontend_files:
        reasons.append(f"{len(frontend_files)} frontend file(s) changed")
    matched_labels = sorted(set(labels) & FRONTEND_LABELS)
    if matched_labels:
        reasons.append(f"frontend label(s): {', '.join(matched_labels)}")

    return FrontendImpact(
        is_frontend=bool(frontend_files or matched_labels),
        frontend_files=frontend_files,
        reasons=reasons,
    )


@dataclass
class DesignBinding:
    has_design_section: bool
    has_tokens: bool
    has_component_library: bool
    component_library: str | None = None
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def check_design_tokens(ui_spec: dict | None) -> DesignBinding:
    """Check the ui-spec ``design`` section binds tokens + a component library."""
    design = (ui_spec or {}).get("design") or {}
    has_design = bool(design)
    tokens = design.get("tokens") or {}
    lib = design.get("component_library")
    lib_name = lib.get("name") if isinstance(lib, dict) else lib

    missing: list[str] = []
    if not has_design:
        missing.append("design section")
    else:
        if not tokens:
            missing.append("tokens")
        if not lib_name:
            missing.append("component_library")

    return DesignBinding(
        has_design_section=has_design,
        has_tokens=bool(tokens),
        has_component_library=bool(lib_name),
        component_library=lib_name,
        missing=missing,
    )


def discover_reference_screenshots(design_dir: str | Path | None, ui_spec: dict | None = None) -> list[str]:
    """Find approved reference screenshots from docs/design/ and the ui-spec."""
    found: list[str] = []
    if design_dir:
        base = Path(design_dir)
        for sub in ("reference", "reference-screenshots"):
            d = base / sub
            if d.is_dir():
                found.extend(
                    str(p) for p in sorted(d.iterdir()) if p.suffix.lower() in IMAGE_EXTS
                )
    # Asset references declared in the ui-spec design section.
    design = (ui_spec or {}).get("design") or {}
    assets = design.get("assets") or []
    if isinstance(assets, list):
        for asset in assets:
            ref = asset.get("path") if isinstance(asset, dict) else asset
            if isinstance(ref, str) and Path(ref).suffix.lower() in IMAGE_EXTS:
                found.append(ref)
    # Deduplicate, preserve order.
    seen: set[str] = set()
    return [f for f in found if not (f in seen or seen.add(f))]


@dataclass
class CapturePlan:
    tool: str | None
    available: bool
    blocker: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def plan_screenshot_capture(
    *,
    browser_use_available: bool = False,
    playwright_available: bool = False,
    has_design_contract: bool = False,
) -> CapturePlan:
    """Pick a screenshot tool, or report a structured blocker.

    For a frontend ticket *with* a design contract, missing tooling is a blocker
    (the design-fidelity gate can't run); without a contract it's just skipped.
    """
    if browser_use_available:
        return CapturePlan(tool="browser-use", available=True)
    if playwright_available:
        return CapturePlan(tool="playwright", available=True)
    blocker = (
        "no screenshot tooling (browser-use / Playwright) available; "
        "design-fidelity gate cannot run"
        if has_design_contract
        else None
    )
    return CapturePlan(tool=None, available=False, blocker=blocker)


@dataclass
class UXManifest:
    frontend: FrontendImpact
    design_binding: DesignBinding
    reference_screenshots: list[str]
    capture_plan: CapturePlan
    screenshot_dir: str | None = None

    @property
    def should_run_gate(self) -> bool:
        return self.frontend.is_frontend

    @property
    def blocked(self) -> bool:
        return bool(self.capture_plan.blocker)

    def to_dict(self) -> dict:
        return {
            "should_run_gate": self.should_run_gate,
            "blocked": self.blocked,
            "frontend": self.frontend.to_dict(),
            "design_binding": self.design_binding.to_dict(),
            "reference_screenshots": self.reference_screenshots,
            "capture_plan": self.capture_plan.to_dict(),
            "screenshot_dir": self.screenshot_dir,
        }

    def render_markdown(self) -> str:
        lines = ["# UX Gate Manifest", ""]
        lines.append(f"- Frontend ticket: {'yes' if self.frontend.is_frontend else 'no'}")
        for r in self.frontend.reasons:
            lines.append(f"  - {r}")
        if not self.frontend.is_frontend:
            lines.append("- Design-fidelity gate skipped (no frontend impact).")
            return "\n".join(lines) + "\n"
        db = self.design_binding
        lines.append(f"- Design contract: {'present' if db.has_design_section else 'absent'}")
        if db.missing:
            lines.append(f"  - missing: {', '.join(db.missing)}")
        if db.component_library:
            lines.append(f"  - component library: {db.component_library}")
        lines.append(f"- Reference screenshots: {len(self.reference_screenshots)}")
        lines.append(f"- Capture tool: {self.capture_plan.tool or 'none'}")
        if self.capture_plan.blocker:
            lines.append(f"- ⚠️ BLOCKER: {self.capture_plan.blocker}")
        return "\n".join(lines) + "\n"


def build_ux_manifest(
    changed_files: list[str],
    *,
    labels: list[str] | None = None,
    ui_spec: dict | None = None,
    design_dir: str | Path | None = None,
    browser_use_available: bool = False,
    playwright_available: bool = False,
    screenshot_dir: str | None = None,
) -> UXManifest:
    frontend = detect_frontend_impact(changed_files, labels)
    binding = check_design_tokens(ui_spec)
    refs = discover_reference_screenshots(design_dir, ui_spec)
    plan = plan_screenshot_capture(
        browser_use_available=browser_use_available,
        playwright_available=playwright_available,
        has_design_contract=frontend.is_frontend and binding.has_design_section,
    )
    return UXManifest(
        frontend=frontend,
        design_binding=binding,
        reference_screenshots=refs,
        capture_plan=plan,
        screenshot_dir=screenshot_dir,
    )

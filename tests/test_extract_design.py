"""Tests for extract_design.py: mock token extraction, validation, styleguide."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import extract_design as ed  # noqa: E402
import formal_models as fm  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "extract_design.py"

MOCK = """<!doctype html>
<html><head><style>
:root {
  --color-primary: #1b6e5a;
  --color-surface: #f7f5f0;
  --color-cta: var(--color-primary);
  --font-heading: "Fraunces", serif;
  --font-body: "Inter", sans-serif;
  --text-base: 1rem;
  --text-xl: 1.5rem;
  --space-md: 16px;
  --radius-card: 12px;
  --shadow-card: 0 2px 8px rgba(0, 0, 0, .12), 0 1px 2px rgba(0, 0, 0, .08);
  --bogus-token: 42px;
}
.card { border-radius: var(--radius-card); }
</style></head><body><div class="card">hi</div></body></html>
"""


def _extract(tmp_path: Path, mock_text: str = MOCK) -> tuple[dict, list[str]]:
    mock = tmp_path / "mock.html"
    mock.write_text(mock_text)
    return ed.extract(mock, "net-new", [], None)


def test_tokens_mapped_to_schema_buckets(tmp_path):
    design, _ = _extract(tmp_path)
    tokens = design["tokens"]
    assert tokens["colors"]["primary"] == "#1b6e5a"
    assert tokens["colors"]["surface"] == "#f7f5f0"
    assert tokens["typography"]["fonts"]["heading"] == '"Fraunces", serif'
    assert tokens["typography"]["scale"]["xl"] == "1.5rem"
    assert tokens["spacing"]["md"] == "16px"
    assert tokens["radii"]["card"] == "12px"
    assert "rgba(0, 0, 0, .12)" in tokens["shadows"]["card"]


def test_var_references_resolved(tmp_path):
    design, _ = _extract(tmp_path)
    assert design["tokens"]["colors"]["cta"] == "#1b6e5a"


def test_unrecognised_prefix_reported_not_mapped(tmp_path):
    design, skipped = _extract(tmp_path)
    assert "--bogus-token" in skipped
    assert "bogus" not in json.dumps(design)


def test_extracted_design_validates_standalone_and_in_full_spec(tmp_path):
    design, _ = _extract(tmp_path)
    assert ed.validate_design(design) == []
    spec = {
        "pages": {"home": {"route": "/", "layout": "centered"}},
        "navigation": {"initial": "home", "states": {"home": {"route": "/"}}},
        "design": design,
    }
    assert fm.validate(spec, "ui-spec") == []


def test_missing_primary_fails_validation(tmp_path):
    mock = MOCK.replace("--color-primary: #1b6e5a;", "")
    design, _ = _extract(tmp_path, mock)
    errors = ed.validate_design(design)
    assert any("primary" in e for e in errors)


def test_styleguide_renders_tokens(tmp_path):
    design, _ = _extract(tmp_path)
    design["voice"] = {"tone": "warm, encouraging"}
    page = ed.render_styleguide(design)
    assert "#1b6e5a" in page
    assert "Fraunces" in page
    assert "warm, encouraging" in page
    assert page.startswith("<!doctype html>")


def test_cli_extract_and_validate_roundtrip(tmp_path):
    mock = tmp_path / "mock.html"
    mock.write_text(MOCK)
    out = tmp_path / "design.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "extract",
            str(mock),
            "--source-kind",
            "net-new",
            "--reference",
            "docs/design/reference/home.png",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--bogus-token" in result.stderr  # skipped property is loud
    design = json.loads(out.read_text())
    assert design["source"] == {"kind": "net-new", "references": ["docs/design/reference/home.png"]}

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", str(out)], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "valid" in result.stdout


def test_cli_exit_codes(tmp_path):
    missing = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "extract",
            str(tmp_path / "nope.html"),
            "--source-kind",
            "net-new",
        ],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2

    mock = tmp_path / "bare.html"
    mock.write_text("<html><style>:root { --color-accent: #fff; }</style></html>")
    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), "extract", str(mock), "--source-kind", "net-new"],
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 1
    assert "primary" in invalid.stderr

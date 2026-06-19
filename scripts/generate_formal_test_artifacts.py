#!/usr/bin/env python3
"""Generate all model-derived test artifacts for a ticket or wave (P1-6).

`/implement` Step 5 and `/implement-wave` Step 4a both duplicate a sequence of
`render_models.py` / `formal_models.py` calls to turn the epic's formal models
into mechanical test artifacts (test paths, test plan, contract assertions,
Hypothesis skeleton, guard templates). This wraps that sequence into one
idempotent operation that emits a manifest of generated files plus a markdown
summary — suitable to hand straight into a sub-agent prompt.

Usage:
    python3 scripts/generate_formal_test_artifacts.py <models-dir> --output <dir>
    python3 scripts/generate_formal_test_artifacts.py <models-dir> --output <dir> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import formal_models as fm  # noqa: E402
import render_models as rm  # noqa: E402

# Source models we know how to derive test artifacts from, with the schema each
# filename is required to hold (so a mislabeled file is flagged, not rendered).
EXPECTED_SCHEMA = {
    "state-machines.json": "state-machine",
    "contracts.json": "contracts",
    "ui-spec.json": "ui-spec",
}
MODEL_FILES = tuple(EXPECTED_SCHEMA)
# Views that produce *test* artifacts (skip the human/markdown view).
TEST_VIEWS = ("machine", "test")


@dataclass
class ModelResult:
    name: str
    # "ok" (valid + files) | "empty" (valid, no test artifacts, e.g. ui-spec) |
    # "missing" | "invalid" | "malformed"
    status: str
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class GenerationManifest:
    models_dir: str
    output_dir: str
    results: list[ModelResult] = field(default_factory=list)

    @property
    def generated_files(self) -> list[str]:
        files: list[str] = []
        for r in self.results:
            files.extend(r.files)
        return files

    @property
    def ok(self) -> bool:
        # A run is OK unless a present model is invalid or malformed. A valid
        # model that yields no test artifacts ("empty", e.g. ui-spec) is fine.
        return all(r.status in ("ok", "empty", "missing") for r in self.results)

    def to_dict(self) -> dict:
        return {
            "models_dir": self.models_dir,
            "output_dir": self.output_dir,
            "ok": self.ok,
            "generated_files": self.generated_files,
            "results": [asdict(r) for r in self.results],
        }

    def render_markdown(self) -> str:
        lines = ["# Formal Test Artifacts", "", f"From `{self.models_dir}` -> `{self.output_dir}`", ""]
        for r in self.results:
            lines.append(f"- **{r.name}**: {r.status}" + (f" ({len(r.files)} files)" if r.files else ""))
            for err in r.errors:
                lines.append(f"  - {err}")
        if self.generated_files:
            lines += ["", "## Generated files", ""]
            lines += [f"- `{f}`" for f in self.generated_files]
        return "\n".join(lines) + "\n"


def _generate_one(model_path: Path, output_dir: Path, views=TEST_VIEWS) -> ModelResult:
    name = model_path.name
    expected = EXPECTED_SCHEMA.get(name)
    try:
        model = fm._load_json(model_path)
    except (json.JSONDecodeError, OSError) as exc:
        return ModelResult(name, "malformed", errors=[str(exc)])

    # The file must hold the schema its name implies; a contracts.json that is
    # actually a state machine must be rejected, not rendered as a state machine.
    try:
        detected = fm.detect_schema_type(model)
    except Exception as exc:  # noqa: BLE001 - unknown schema
        return ModelResult(name, "invalid", errors=[str(exc)])
    if expected and detected != expected:
        return ModelResult(
            name, "invalid", errors=[f"expected {expected} schema, found {detected}"]
        )

    errors = fm.validate(model, expected or detected)
    if errors:
        return ModelResult(name, "invalid", errors=list(errors))

    files: list[str] = []
    for view in views:
        files.extend(rm.render_model(model_path, view, output_dir))
    # Deduplicate while preserving order (machine + test views can overlap).
    seen: set[str] = set()
    deduped = [f for f in files if not (f in seen or seen.add(f))]
    # A valid model that produces no test artifacts (e.g. ui-spec, which has no
    # machine/test view) is "empty", not a failure.
    status = "ok" if deduped else "empty"
    return ModelResult(name, status, files=deduped)


def generate_artifacts(
    models_dir: str | Path,
    output_dir: str | Path,
    *,
    views=TEST_VIEWS,
    write_manifest: bool = True,
) -> GenerationManifest:
    """Generate test artifacts for every known model present in ``models_dir``.

    Idempotent: re-running overwrites the generated files in ``output_dir``.
    """
    models = Path(models_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Clear artifacts a prior run generated so the output dir stays a faithful
    # set even when a model later goes missing/invalid.
    prior_manifest = out / "formal-artifacts-manifest.json"
    if prior_manifest.exists():
        try:
            for stale in json.loads(prior_manifest.read_text()).get("generated_files", []):
                Path(stale).unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            pass

    manifest = GenerationManifest(models_dir=str(models), output_dir=str(out))
    for name in MODEL_FILES:
        path = models / name
        if not path.exists():
            manifest.results.append(ModelResult(name, "missing"))
            continue
        manifest.results.append(_generate_one(path, out, views=views))

    if write_manifest:
        (out / "formal-artifacts-manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2)
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate model-derived test artifacts")
    parser.add_argument("models_dir", help="Directory containing the epic's model JSON files")
    parser.add_argument("--output", required=True, help="Output directory for generated artifacts")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Emit manifest JSON (default)")
    out.add_argument("--markdown", action="store_true", help="Emit markdown summary")
    args = parser.parse_args(argv)

    manifest = generate_artifacts(args.models_dir, args.output)
    if args.markdown:
        print(manifest.render_markdown())
    else:
        print(json.dumps(manifest.to_dict(), indent=2))
    # Non-zero if any present model is invalid or malformed.
    return 0 if manifest.ok else 1


if __name__ == "__main__":
    sys.exit(main())

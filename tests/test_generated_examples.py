from pathlib import Path

import pytest

import render_models

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "formal-methods" / "examples"
GENERATED = EXAMPLES / "generated"

NODE_DEPS_MISSING = not (Path(__file__).resolve().parents[1] / "node_modules" / "xstate").is_dir()
needs_node_deps = pytest.mark.skipif(
    NODE_DEPS_MISSING,
    reason="node_modules/xstate not installed (run `npm install` in the repo root; "
           "a fresh worktree has none)",
)


def assert_matches_generated(tmp_path: Path, model_name: str, expected_files: list[str]):
    render_models.render_model(EXAMPLES / model_name, "all", tmp_path)

    for filename in expected_files:
        assert (tmp_path / filename).read_text() == (GENERATED / filename).read_text()


@needs_node_deps
def test_state_machine_generated_artifacts_are_fresh(tmp_path):
    assert_matches_generated(
        tmp_path,
        "order-lifecycle.state-machine.json",
        [
            "order-lifecycle.state-machine.md",
            "xstate-machine.json",
            "test_state_machine.py",
            "test-paths.json",
            "test-plan.md",
        ],
    )


def test_contract_generated_artifacts_are_fresh(tmp_path):
    assert_matches_generated(
        tmp_path,
        "order-lifecycle.contracts.json",
        [
            "order-lifecycle.contracts.md",
            "contracts_deal.py",
            "guards.py",
            "guards.go",
            "contract-assertions.md",
        ],
    )

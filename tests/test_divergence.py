"""Shared quorum classifier (scripts/divergence.py) — the primitive behind both
chief-wiggum#253's name intersection-discard and #254's strategy convergent-labelling."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import divergence  # noqa: E402


def test_classify_marks_two_plus_providers_as_convergent():
    entries = divergence.classify({
        "codex": ["alpha", "beta"],
        "opus": ["alpha"],
    })
    by_name = {e["name"]: e for e in entries}
    assert by_name["alpha"]["convergent"] is True
    assert by_name["alpha"]["sources"] == ["codex", "opus"]
    assert by_name["beta"]["convergent"] is False
    assert by_name["beta"]["sources"] == ["codex"]


def test_classify_normalizes_case_and_whitespace():
    entries = divergence.classify({"a": [" Alpha "], "b": ["alpha"]})
    assert len(entries) == 1
    assert entries[0]["name"] == "alpha"
    assert entries[0]["convergent"] is True


def test_classify_three_way_agreement():
    entries = divergence.classify({"a": ["x"], "b": ["x"], "c": ["x"]})
    assert entries[0]["sources"] == ["a", "b", "c"]
    assert entries[0]["convergent"] is True


def test_classify_empty_input():
    assert divergence.classify({}) == []


def test_discard_convergent_splits_survivors_and_discarded():
    entries = divergence.classify({"a": ["x", "y"], "b": ["x"]})
    survivors, discarded = divergence.discard_convergent(entries)
    assert {e["name"] for e in survivors} == {"y"}
    assert {e["name"] for e in discarded} == {"x"}


def test_label_convergent_keeps_everything():
    entries = divergence.classify({"a": ["x", "y"], "b": ["x"]})
    labelled = divergence.label_convergent(entries)
    assert {e["name"] for e in labelled} == {"x", "y"}
    by_name = {e["name"]: e for e in labelled}
    assert by_name["x"]["convergent"] is True
    assert by_name["y"]["convergent"] is False

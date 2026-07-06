"""Tests for the tutorial-video producer's deterministic pieces.

Network (TTS), browser (Playwright), and ffmpeg stages are exercised by the
workflow's own dry-run/probe steps; here we pin down the pure logic: storyboard
validation, URL resolution, pacing math, the ffmpeg audio filter graph, and
SRT generation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tutorial_video as tv


def _board(**overrides) -> dict:
    board = {
        "title": "Demo",
        "base_url": "http://localhost:3000",
        "scenes": [
            {
                "id": "intro",
                "title": "Intro",
                "narration": "Welcome to the demo.",
                "actions": [{"type": "goto", "url": "/"}],
            }
        ],
    }
    board.update(overrides)
    return board


# --- validation ---------------------------------------------------------------


def test_valid_storyboard_has_no_errors():
    assert tv.validate_storyboard(_board()) == []


def test_missing_title_and_scenes():
    assert "missing top-level 'title'" in tv.validate_storyboard({"scenes": [_board()["scenes"][0]]})
    assert tv.validate_storyboard({"title": "x", "scenes": []}) == ["'scenes' must be a non-empty list"]


def test_duplicate_scene_ids_rejected():
    scene = _board()["scenes"][0]
    errors = tv.validate_storyboard(_board(scenes=[scene, dict(scene)]))
    assert any("duplicate scene id" in e for e in errors)


def test_scene_requires_narration_and_actions():
    errors = tv.validate_storyboard(_board(scenes=[{"id": "a", "narration": " ", "actions": []}]))
    assert any("missing 'narration'" in e for e in errors)
    assert any("'actions' must be a non-empty list" in e for e in errors)


def test_unknown_action_type_rejected():
    board = _board()
    board["scenes"][0]["actions"].append({"type": "teleport"})
    assert any("unknown action type 'teleport'" in e for e in tv.validate_storyboard(board))


def test_action_field_requirements():
    assert tv.validate_action({"type": "click"}, "x") == ["x (click) missing 'selector'"]
    assert tv.validate_action({"type": "fill", "selector": "#a"}, "x") == ["x (fill) missing 'value'"]
    assert tv.validate_action({"type": "scroll"}, "x") == ["x (scroll) needs 'selector' or 'y'"]
    assert tv.validate_action({"type": "scroll", "y": 200}, "x") == []
    assert any("must be a number" in e for e in tv.validate_action({"type": "wait", "seconds": "2"}, "x"))


def test_first_action_should_be_goto():
    board = _board()
    board["scenes"][0]["actions"] = [{"type": "click", "selector": "text=Go"}]
    assert any("should be a 'goto'" in e for e in tv.validate_storyboard(board))


# --- url resolution -----------------------------------------------------------


def test_resolve_url_relative_and_absolute():
    assert tv.resolve_url("/pricing", "http://localhost:3000") == "http://localhost:3000/pricing"
    assert tv.resolve_url("pricing", "http://localhost:3000/app/") == "http://localhost:3000/app/pricing"
    assert tv.resolve_url("https://x.test/a", "http://localhost") == "https://x.test/a"


def test_resolve_url_relative_without_base_raises():
    with pytest.raises(ValueError, match="base_url"):
        tv.resolve_url("/pricing", None)


# --- pacing -------------------------------------------------------------------


def test_scene_hold_covers_narration():
    # 10s narration, 3s of actions elapsed -> hold the remainder plus tail
    assert tv.scene_hold_seconds(3.0, 10.0, tail=0.5) == pytest.approx(7.5)
    # actions already outlasted narration -> no hold
    assert tv.scene_hold_seconds(12.0, 10.0, tail=0.5) == 0.0


# --- ffmpeg filter graph --------------------------------------------------


def test_build_audio_filter_single_input_skips_amix():
    graph = tv.build_audio_filter([0])
    assert "adelay=0:all=1[a1]" in graph
    assert "amix" not in graph
    assert graph.endswith("[aout]")


def test_build_audio_filter_multiple_inputs():
    graph = tv.build_audio_filter([0, 12500])
    assert "[1:a]" in graph and "[2:a]" in graph
    assert "adelay=12500:all=1[a2]" in graph
    assert "amix=inputs=2:duration=longest:normalize=0[aout]" in graph


# --- captions -------------------------------------------------------------


def test_srt_timestamp_format():
    assert tv.srt_timestamp(0) == "00:00:00,000"
    assert tv.srt_timestamp(3661.25) == "01:01:01,250"


def test_build_srt_uses_markers_and_narration():
    markers = [
        {"id": "a", "title": "A", "start": 0.5, "end": 9.0, "narration_duration": 4.0},
        {"id": "b", "title": "B", "start": 9.0, "end": 20.0, "narration_duration": 0.0},
    ]
    srt = tv.build_srt(markers, {"a": "First cue.", "b": "Second cue."})
    assert "1\n00:00:00,500 --> 00:00:04,500\nFirst cue." in srt
    # no narration duration -> cue spans the scene
    assert "2\n00:00:09,000 --> 00:00:20,000\nSecond cue." in srt


# --- CLI ----------------------------------------------------------------------


def test_cli_validate_ok(tmp_path, capsys):
    board_path = tmp_path / "storyboard.json"
    board_path.write_text(json.dumps(_board()))
    assert tv.main(["validate", str(board_path)]) == 0
    assert "storyboard OK" in capsys.readouterr().out


def test_cli_validate_rejects_bad_board(tmp_path):
    board_path = tmp_path / "storyboard.json"
    board_path.write_text(json.dumps({"title": "x", "scenes": [{"id": "a"}]}))
    with pytest.raises(SystemExit):
        tv.main(["validate", str(board_path)])


# --- engine resolution ------------------------------------------------------


def test_resolve_engine_passthrough_and_auto(monkeypatch):
    assert tv.resolve_engine("say") == "say"
    assert tv.resolve_engine("openai") == "openai"
    import keychain
    monkeypatch.setattr(keychain, "has_secret", lambda name: True)
    assert tv.resolve_engine("auto") == "elevenlabs"
    monkeypatch.setattr(keychain, "has_secret", lambda name: name == "OPENAI_API_KEY")
    assert tv.resolve_engine("auto") == "openai"
    monkeypatch.setattr(keychain, "has_secret", lambda name: False)
    monkeypatch.setattr(tv.shutil, "which", lambda name: "/usr/bin/say")
    assert tv.resolve_engine("auto") == "say"
    monkeypatch.setattr(tv.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        tv.resolve_engine("auto")


# --- pronunciations ---------------------------------------------------------


def test_apply_pronunciations_word_boundary_case_insensitive():
    mapping = {"Dogeared Coach": "dog eared coach", "Dogeared": "dog eared"}
    assert tv.apply_pronunciations("Welcome to Dogeared Coach.", mapping) == "Welcome to dog eared coach."
    # longest key wins; bare word also mapped; no substring mangling
    assert tv.apply_pronunciations("dogeared style, undogeared", mapping) == "dog eared style, undogeared"


def test_pronunciations_schema_validated():
    board = _board(pronunciations={"Dogeared": "dog eared"})
    assert tv.validate_storyboard(board) == []
    bad = _board(pronunciations={"Dogeared": 3})
    assert any("pronunciations" in e for e in tv.validate_storyboard(bad))

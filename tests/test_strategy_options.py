"""chief-wiggum#254: strategy-option divergence — convergent-labelling (not discard)
plus entropy-injection constraint prompts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import strategy_options as so  # noqa: E402

DOC = Path(__file__).resolve().parent.parent / "docs" / "business-factory.md"


# --- convergent-labelling (decision 2): kept, never discarded ------------------

def test_convergent_options_are_labelled_not_discarded():
    entries = so.classify_options({
        "codex": ["undercut on price", "target the neglected incumbent"],
        "opus": ["undercut on price"],
    })
    names = {e["name"] for e in entries}
    assert names == {"undercut on price", "target the neglected incumbent"}, (
        "both options must survive — #254 never discards, only labels"
    )
    by_name = {e["name"]: e for e in entries}
    assert by_name["undercut on price"]["convergent"] is True
    assert by_name["target the neglected incumbent"]["convergent"] is False


def test_cli_quorum_file_reports_convergent_count(tmp_path, capsys):
    qfile = tmp_path / "options.json"
    qfile.write_text(json.dumps({
        "codex": ["undercut on price"],
        "opus": ["undercut on price"],
        "gemini": ["jurisdiction quarantine"],
    }))
    argv = ["strategy_options.py", "--quorum-file", str(qfile), "--format", "json"]
    with patch.object(sys, "argv", argv):
        rc = so.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["options"]) == 2
    conv = next(o for o in out["options"] if o["name"] == "undercut on price")
    assert conv["convergent"] is True
    div = next(o for o in out["options"] if o["name"] == "jurisdiction quarantine")
    assert div["convergent"] is False


def test_cli_text_output_flags_convergent_and_names_the_requirement(tmp_path, capsys):
    qfile = tmp_path / "options.json"
    qfile.write_text(json.dumps({"a": ["undercut"], "b": ["undercut"]}))
    argv = ["strategy_options.py", "--quorum-file", str(qfile)]
    with patch.object(sys, "argv", argv):
        rc = so.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "CONVERGENT" in out
    assert "stated reason" in out.lower() or "state why" in out.lower()


# --- entropy-injection constraints (decision 3) --------------------------------

def test_constraint_categories_cover_the_documented_set():
    expected = {
        "segment", "cost-structure", "distribution-channel",
        "buyer-inversion", "adversarial-reframe", "historical-episode",
    }
    assert set(so.CONSTRAINT_CATEGORIES) == expected


def test_random_constraint_draw_is_reproducible_with_a_seed():
    a = so.draw_constraint("random", __import__("random").Random(42))
    b = so.draw_constraint("random", __import__("random").Random(42))
    assert a == b


def test_historical_episode_constraint_names_a_drawn_episode():
    import random
    c = so.draw_constraint("historical-episode", random.Random(1))
    assert c["episode"] in so.HISTORICAL_EPISODES
    assert c["episode"] in c["prompt"]


def test_unknown_constraint_category_exits():
    import random
    with pytest.raises(SystemExit):
        so.draw_constraint("not-a-real-category", random.Random(1))


def test_cli_constraint_flag_prints_a_prompt(capsys):
    argv = ["strategy_options.py", "--constraint", "segment", "--seed", "1"]
    with patch.object(sys, "argv", argv):
        rc = so.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "segment" in out.lower()


def test_historical_episodes_count_matches_doc_mined_corpus():
    """§9.4 mines nine episodes — keep the entropy-seeding list in sync so a future
    doc edit that adds/removes an episode is forced to touch this file too."""
    assert len(so.HISTORICAL_EPISODES) == 9
    text = DOC.read_text()
    m = re.search(r"Nine episodes mined", text)
    assert m, "docs/business-factory.md §9.4 must still describe nine mined episodes"

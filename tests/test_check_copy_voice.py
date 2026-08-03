"""Tests for scripts/check_copy_voice.py (chief-wiggum#255): the banned-construction
lint + specificity floor. Report-only always (docs/gate-rollout.md) — voice is
human-judged, the lint only surfaces candidates.

The seeded-defect positive case is an INVENTED, generic AI-default specimen (never a
real product's copy — CLAUDE.md forbids naming private products in this public repo),
carrying every named tell: em-dash triplets, antithesis, a tricolon, an abstract-virtue
header, and a low specificity floor. The negative case is a human-sourced-sounding
paragraph (sequential, plain, specific) that must trip nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_copy_voice as ccv  # noqa: E402

CCV = str(SCRIPTS / "check_copy_voice.py")

# Seeded-defect positive case: invented, generic AI-default marketing voice.
AI_DEFAULT_COPY = """# Seamless, Effortless Excellence

This is not complexity — it's clarity. It isn't just a tool, it's a movement.
It's not busywork, it's freedom. You review, you approve, you ship.

Our platform delivers frictionless, world-class results for every team, every
day, without compromise, without friction, without doubt.
"""

# Negative case: sequential, plain, specific — modelled on the doctrine's own
# worked example of real customer language (docs/business-factory.md, #255
# context), but written fresh here rather than quoting any real correspondence.
HUMAN_SOURCED_COPY = """Currently we use a rostering app, then take the hours and
enter them into a spreadsheet, then send them to accounts. Accounts enter them
into Xero and pay staff. That takes about 3 hours every fortnight. Our tool
cuts that to 15 minutes by reading the CSV export and posting directly to Xero.
"""


def _run(*argv: str):
    return subprocess.run([sys.executable, CCV, *argv], capture_output=True, text=True)


# ---- individual tells -----------------------------------------------------------

def test_em_dash_density_flagged_above_threshold():
    findings = ccv.em_dash_findings(AI_DEFAULT_COPY)
    assert findings and findings[0].tell == "em-dash-density"


def test_em_dash_density_silent_on_occasional_legitimate_use():
    # A single em-dash pair spread across ~120 words of otherwise plain prose
    # is well under the 1.5-per-100-words threshold — occasional legitimate
    # punctuation, not a tic.
    filler = "It reads the CSV export and posts the totals directly to Xero each fortnight. "
    text = "We built this after a long — but useful — conversation with a customer. "
    text += filler * 15
    findings = ccv.em_dash_findings(text)
    assert findings == []


def test_antithesis_not_x_emdash_y_detected():
    findings = ccv.antithesis_findings("This is not complexity — it's clarity.")
    assert len(findings) == 1
    assert findings[0].tell == "antithesis"


def test_antithesis_isnt_just_x_its_y_detected():
    findings = ccv.antithesis_findings("It isn't just a tool, it's a movement.")
    assert len(findings) == 1


def test_antithesis_its_not_x_its_y_detected():
    findings = ccv.antithesis_findings("It's not busywork, it's freedom.")
    assert len(findings) == 1


def test_antithesis_silent_on_plain_sentence():
    assert ccv.antithesis_findings("The tool reads the CSV export and posts to Xero.") == []


def test_tricolon_of_short_parallel_clauses_detected():
    findings = ccv.tricolon_findings("You review, you approve, you ship.")
    assert len(findings) == 1 and findings[0].tell == "tricolon"


def test_tricolon_silent_on_a_normal_longer_clause_list():
    text = "You review the schedule carefully, approve it once everything matches, and ship it to the team."
    assert ccv.tricolon_findings(text) == []


def test_abstract_virtue_header_detected():
    findings = ccv.virtue_header_findings("# Seamless, Effortless Excellence")
    assert len(findings) == 1 and findings[0].tell == "abstract-virtue-header"


def test_virtue_header_silent_when_a_number_is_present():
    assert ccv.virtue_header_findings("# 3 hours saved every fortnight") == []


def test_virtue_header_silent_on_a_concrete_header():
    assert ccv.virtue_header_findings("# Reads your CSV export and posts to Xero") == []


def test_specificity_floor_flags_low_concreteness_copy():
    findings = ccv.specificity_findings(AI_DEFAULT_COPY)
    assert findings and findings[0].tell == "specificity-floor"


def test_specificity_floor_passes_on_human_sourced_copy():
    assert ccv.specificity_findings(HUMAN_SOURCED_COPY) == []


# ---- end-to-end lint() -----------------------------------------------------------

def test_lint_flags_every_tell_on_the_seeded_ai_default_specimen():
    findings = ccv.lint(AI_DEFAULT_COPY)
    tells = {f.tell for f in findings}
    assert tells == {
        "em-dash-density", "antithesis", "tricolon",
        "abstract-virtue-header", "specificity-floor",
    }


def test_lint_is_clean_on_human_sourced_copy():
    assert ccv.lint(HUMAN_SOURCED_COPY) == []


# ---- CLI -------------------------------------------------------------------------

def test_cli_report_only_exits_zero_even_with_findings(tmp_path):
    p = tmp_path / "copy.md"
    p.write_text(AI_DEFAULT_COPY)
    r = _run(str(p))
    assert r.returncode == 0
    assert "finding" in r.stdout.lower()


def test_cli_gate_flag_exits_one_with_findings(tmp_path):
    p = tmp_path / "copy.md"
    p.write_text(AI_DEFAULT_COPY)
    r = _run(str(p), "--gate")
    assert r.returncode == 1


def test_cli_gate_flag_exits_zero_when_clean(tmp_path):
    p = tmp_path / "copy.md"
    p.write_text(HUMAN_SOURCED_COPY)
    r = _run(str(p), "--gate")
    assert r.returncode == 0


def test_cli_json_output_is_a_list_of_findings(tmp_path):
    p = tmp_path / "copy.md"
    p.write_text(AI_DEFAULT_COPY)
    r = _run(str(p), "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert isinstance(out, list) and len(out) >= 5
    assert all({"tell", "detail", "snippet"} <= set(f) for f in out)


def test_cli_missing_file_exits_nonzero():
    r = _run("/no/such/file.md")
    assert r.returncode != 0

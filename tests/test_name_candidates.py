"""chief-wiggum#253: entropy-injected name generation.

Covers the two mechanisms this ticket adds on top of the already-shipped
corpus/coinage generator + RDAP availability filter:

- multi-model divergence with INTERSECTION-DISCARD (decision 2): names
  proposed independently by >=2 quorum providers are discarded, not promoted
- availability-before-taste (decision 3): a known-registered domain, and its
  `get<name>.com` variant, are both correctly rejected by the RDAP filter
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import name_candidates as nc  # noqa: E402

# --- quorum intersection-discard (decision 2) ---------------------------------

def test_intersection_discard_removes_names_proposed_by_two_or_more_providers():
    by_provider = {
        "codex": ["wanderoo", "flintbase", "solelight"],
        "opus": ["wanderoo", "cinderpath"],
        "gemini": ["quillmark"],
    }
    survivors, discarded = nc.quorum_classify(by_provider)
    survivor_names = {c["name"] for c in survivors}
    discarded_names = {d["name"] for d in discarded}

    assert "wanderoo" not in survivor_names, "proposed by codex AND opus — must be discarded"
    assert "wanderoo" in discarded_names
    assert {"flintbase", "solelight", "cinderpath", "quillmark"} <= survivor_names


def test_intersection_discard_is_case_and_whitespace_normalized():
    by_provider = {
        "codex": [" Wanderoo "],
        "opus": ["wanderoo"],
    }
    survivors, discarded = nc.quorum_classify(by_provider)
    assert survivors == []
    assert discarded == [{"name": "wanderoo", "sources": ["codex", "opus"]}]


def test_intersection_discard_records_provenance_on_survivors():
    by_provider = {"codex": ["flintbase"], "opus": ["cinderpath"]}
    survivors, discarded = nc.quorum_classify(by_provider)
    assert discarded == []
    by_name = {c["name"]: c for c in survivors}
    assert by_name["flintbase"]["sources"] == ["codex"]
    assert by_name["flintbase"]["seed_words"] == "proposed-by:codex"
    assert by_name["flintbase"]["converged"] is False


def test_intersection_discard_three_way_agreement_still_discarded():
    by_provider = {
        "codex": ["quillmark"],
        "opus": ["quillmark"],
        "gemini": ["quillmark"],
    }
    survivors, discarded = nc.quorum_classify(by_provider)
    assert survivors == []
    assert discarded[0]["sources"] == ["codex", "gemini", "opus"]


def test_intersection_discard_empty_input_is_empty_output():
    survivors, discarded = nc.quorum_classify({})
    assert survivors == [] and discarded == []


def test_quorum_file_cli_path_discards_convergent_and_reports_them(tmp_path, capsys):
    qfile = tmp_path / "quorum.json"
    qfile.write_text(json.dumps({
        "codex": ["wanderoo", "flintbase"],
        "opus": ["wanderoo"],
    }))
    argv = ["name_candidates.py", "--quorum-file", str(qfile), "--format", "json"]
    with patch.object(sys, "argv", argv):
        rc = nc.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in out["candidates"]}
    assert names == {"flintbase"}
    assert out["discarded_convergent"] == [{"name": "wanderoo", "sources": ["codex", "opus"]}]


def test_quorum_file_survivors_still_run_through_brand_infringement_filter(tmp_path):
    qfile = tmp_path / "quorum.json"
    qfile.write_text(json.dumps({"codex": ["mycopilotapp"]}))
    argv = ["name_candidates.py", "--quorum-file", str(qfile), "--format", "json"]
    with patch.object(sys, "argv", argv):
        rc = nc.main()
    assert rc == 0


# --- availability-before-taste: RDAP filter (decision 3) ----------------------

class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen_registered(url, timeout=12):
    """Every domain in this fixture resolves as registered (HTTP 200) —
    simulates a squatted real word AND its get<name>.com variant."""
    return _FakeResponse(200)


def test_rdap_available_reports_registered_domain_as_unavailable():
    with patch.object(nc.urllib.request, "urlopen", _fake_urlopen_registered):
        assert nc.rdap_available("wanderoo.com") is False


def test_rdap_available_reports_get_prefixed_variant_as_unavailable_too():
    """A known-registered name's get<name>.com variant must ALSO be rejected —
    the naming-protocol funnel checks every prefix pattern, not just the bare
    name (chief-wiggum#249 item 1's checklist)."""
    with patch.object(nc.urllib.request, "urlopen", _fake_urlopen_registered):
        assert nc.rdap_available("getwanderoo.com") is False


def test_rdap_available_reports_404_as_available():
    def fake_urlopen(url, timeout=12):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch.object(nc.urllib.request, "urlopen", fake_urlopen):
        assert nc.rdap_available("totallyfreshname.com") is True


def test_rdap_available_reports_unresolved_on_error():
    def fake_urlopen(url, timeout=12):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    with patch.object(nc.urllib.request, "urlopen", fake_urlopen):
        assert nc.rdap_available("ambiguousname.com") is None


def test_check_filter_drops_candidate_registered_on_every_tld(tmp_path, capsys):
    """End-to-end: a corpus-generated candidate whose every checked TLD comes
    back registered must not survive --check (availability-before-taste)."""
    qfile = tmp_path / "quorum.json"
    qfile.write_text(json.dumps({"codex": ["wanderoo", "flintbase"]}))
    argv = ["name_candidates.py", "--quorum-file", str(qfile), "--check",
            "--tlds", "com", "--format", "json"]
    with patch.object(nc.urllib.request, "urlopen", _fake_urlopen_registered):
        with patch.object(sys, "argv", argv):
            rc = nc.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["candidates"] == []  # both names registered on every checked TLD


# --- --wordlist is a real, working flag (the docstring/error message promise) --

def test_wordlist_flag_is_respected(tmp_path):
    custom = tmp_path / "words.txt"
    custom.write_text("flintbase\ncinderpath\nquillmark\nreckon\n")
    pool = nc.load_pool(wordlist=custom)
    assert set(pool) <= {"flintbase", "cinderpath", "quillmark", "reckon"}


def test_missing_wordlist_exits_cleanly(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(SystemExit):
        nc.load_pool(wordlist=missing)

"""Tests for the per-file gate-findings cache (#327): chief_wiggum/findings_cache.py.

``tests/conftest.py``'s ``isolate_findings_cache`` autouse fixture points
``CW_FINDINGS_CACHE_DIR`` at a per-test tmp dir, so these tests never touch the
operator's real ``~/.chief-wiggum/cache/findings``.
"""

from __future__ import annotations

from chief_wiggum import findings_cache as fc


# --- load/store roundtrip ----------------------------------------------------


def test_store_then_load_roundtrips(tmp_path):
    repo = str(tmp_path / "some-repo")
    findings = [{"verb": "guards", "target": "CTR-order-001", "line": 3}]
    fc.store(repo, "check_traceability", "order.py", "abc123", "scanner-v1", findings)
    assert fc.load(repo, "check_traceability", "order.py", "abc123", "scanner-v1") == findings


def test_load_is_a_miss_when_nothing_stored(tmp_path):
    repo = str(tmp_path)
    assert fc.load(repo, "check_traceability", "order.py", "abc123", "scanner-v1") is None


def test_load_tolerates_corrupt_cache_file(tmp_path):
    repo = str(tmp_path / "r")
    path = fc._entry_path(repo, "check_traceability", "order.py", "abc123", "scanner-v1")
    path.write_text("{not json")
    assert fc.load(repo, "check_traceability", "order.py", "abc123", "scanner-v1") is None


def test_different_engines_do_not_collide(tmp_path):
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "abc123", "v1", [{"a": 1}])
    fc.store(repo, "check_single_writer", "order.py", "abc123", "v1", [{"a": 2}])
    assert fc.load(repo, "check_traceability", "order.py", "abc123", "v1") == [{"a": 1}]
    assert fc.load(repo, "check_single_writer", "order.py", "abc123", "v1") == [{"a": 2}]


def test_different_repos_do_not_collide(tmp_path):
    r1, r2 = str(tmp_path / "one"), str(tmp_path / "two")
    fc.store(r1, "check_traceability", "order.py", "abc123", "v1", [{"a": "one"}])
    fc.store(r2, "check_traceability", "order.py", "abc123", "v1", [{"a": "two"}])
    assert fc.load(r1, "check_traceability", "order.py", "abc123", "v1") == [{"a": "one"}]
    assert fc.load(r2, "check_traceability", "order.py", "abc123", "v1") == [{"a": "two"}]


# --- key components are each load-bearing ------------------------------------


def test_different_path_same_content_does_not_collide(tmp_path):
    """Two files with byte-identical content at different paths must never
    serve each other's cached findings — emission depends on path too (e.g.
    a `_test.go` path classifies differently than a plain `.go` one)."""
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "SAMEHASH", "v1", [{"path": "order.py"}])
    assert fc.load(repo, "check_traceability", "order_test.py", "SAMEHASH", "v1") is None


def test_different_blob_sha_does_not_collide(tmp_path):
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "sha-v1", "v1", [{"a": 1}])
    assert fc.load(repo, "check_traceability", "order.py", "sha-v2", "v1") is None


def test_different_scanner_hash_does_not_collide(tmp_path):
    """The exact bug this cache exists to prevent: editing the scanner
    (changing scanner_hash) with the file's content untouched must never
    serve a finding computed by the OLD scanner version."""
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "sha-v1", "scanner-old", [{"a": 1}])
    assert fc.load(repo, "check_traceability", "order.py", "sha-v1", "scanner-new") is None


# --- --no-cache escape hatch --------------------------------------------------


def test_no_cache_env_disables_both_read_and_write(tmp_path, monkeypatch):
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "sha1", "v1", [{"a": 1}])  # before the flag
    monkeypatch.setenv(fc.NO_CACHE_ENV, "1")
    assert fc.load(repo, "check_traceability", "order.py", "sha1", "v1") is None  # read suppressed
    fc.store(repo, "check_traceability", "order.py", "sha2", "v1", [{"a": 2}])
    monkeypatch.delenv(fc.NO_CACHE_ENV)
    assert fc.load(repo, "check_traceability", "order.py", "sha2", "v1") is None  # write was suppressed too


# --- malformed on-disk data never crashes ------------------------------------


def test_load_rejects_non_dict_json(tmp_path):
    repo = str(tmp_path / "r")
    path = fc._entry_path(repo, "check_traceability", "order.py", "sha1", "v1")
    path.write_text("[1, 2, 3]")
    assert fc.load(repo, "check_traceability", "order.py", "sha1", "v1") is None


def test_load_rejects_non_list_findings(tmp_path):
    import json
    repo = str(tmp_path / "r")
    path = fc._entry_path(repo, "check_traceability", "order.py", "sha1", "v1")
    path.write_text(json.dumps({
        "rel": "order.py", "blob_sha": "sha1", "scanner_hash": "v1", "findings": "not-a-list",
    }))
    assert fc.load(repo, "check_traceability", "order.py", "sha1", "v1") is None

"""The findings cache as ONE index per (repo, engine) — the properties the
per-file layout lacked: bounded by file count, self-garbage-collecting on a
scanner change, one read and one write per process, and safe under two
processes flushing the same index.
"""

from __future__ import annotations

import json

from chief_wiggum import findings_cache as fc


def _idx(repo, engine="check_traceability"):
    return json.loads(fc._index_path(repo, engine).read_text())


def test_one_index_file_per_repo_and_engine(tmp_path):
    repo = str(tmp_path / "r")
    for i in range(50):
        fc.store(repo, "check_traceability", f"m{i}.py", f"sha{i}", "v1", [{"i": i}])
    fc.flush()
    files = [p for p in fc._root().rglob("*") if p.is_file()]
    assert len(files) == 1 and files[0].name == "check_traceability.json"
    assert len(_idx(repo)["entries"]) == 50


def test_a_new_blob_replaces_the_old_so_the_index_is_bounded_by_file_count(tmp_path):
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "order.py", "sha-old", "v1", [{"a": 1}])
    fc.store(repo, "check_traceability", "order.py", "sha-new", "v1", [{"a": 2}])
    fc.flush()
    assert list(_idx(repo)["entries"]) == ["order.py"]
    assert fc.load(repo, "check_traceability", "order.py", "sha-old", "v1") is None
    assert fc.load(repo, "check_traceability", "order.py", "sha-new", "v1") == [{"a": 2}]


def test_a_scanner_change_drops_every_old_entry(tmp_path):
    """Nothing computed by the previous scanner may survive beside new entries —
    the old layout kept them forever (and could never serve them, so they were
    pure growth)."""
    repo = str(tmp_path / "r")
    fc.store(repo, "check_traceability", "a.py", "sha-a", "scanner-old", [{"a": 1}])
    fc.store(repo, "check_traceability", "b.py", "sha-b", "scanner-old", [{"b": 1}])
    fc.store(repo, "check_traceability", "a.py", "sha-a", "scanner-new", [{"a": 2}])
    fc.flush()
    idx = _idx(repo)
    assert idx["scanner_hash"] == "scanner-new"
    assert list(idx["entries"]) == ["a.py"]
    assert fc.load(repo, "check_traceability", "b.py", "sha-b", "scanner-new") is None


def test_flush_merges_with_what_another_process_wrote(tmp_path):
    """Two gate runs on the same repo flushing the same index: the later one
    folds the earlier one's entries in (theirs under ours) instead of
    clobbering them — a lost race costs a re-scan, never a wrong answer."""
    repo = str(tmp_path / "r")
    # "Other process": an index already on disk for the same scanner.
    path = fc._index_path(repo, "check_traceability")
    path.write_text(json.dumps({
        "scanner_hash": "v1",
        "entries": {"theirs.py": {"blob_sha": "t", "findings": [{"t": 1}]},
                    "shared.py": {"blob_sha": "old", "findings": [{"s": "theirs"}]}},
    }))
    fc._INDEXES.clear()  # this process has not read the index yet
    fc.store(repo, "check_traceability", "ours.py", "o", "v1", [{"o": 1}])
    fc.store(repo, "check_traceability", "shared.py", "new", "v1", [{"s": "ours"}])
    fc.flush()
    entries = _idx(repo)["entries"]
    assert entries["theirs.py"]["findings"] == [{"t": 1}]
    assert entries["ours.py"]["findings"] == [{"o": 1}]
    assert entries["shared.py"] == {"blob_sha": "new", "findings": [{"s": "ours"}]}


def test_flush_does_not_merge_across_scanner_versions(tmp_path):
    repo = str(tmp_path / "r")
    path = fc._index_path(repo, "check_traceability")
    path.write_text(json.dumps({"scanner_hash": "v-old", "entries": {"x.py": {"blob_sha": "x", "findings": []}}}))
    fc._INDEXES.clear()
    fc.store(repo, "check_traceability", "y.py", "y", "v-new", [{"y": 1}])
    fc.flush()
    assert list(_idx(repo)["entries"]) == ["y.py"]


def test_corrupt_or_wrong_shape_index_is_a_miss_never_a_crash(tmp_path):
    repo = str(tmp_path / "r")
    path = fc._index_path(repo, "check_traceability")
    for bad in ("{not json", "[1,2]", json.dumps({"entries": []}), json.dumps({"scanner_hash": 3, "entries": {}})):
        path.write_text(bad)
        fc._INDEXES.clear()
        assert fc.load(repo, "check_traceability", "a.py", "s", "v1") is None
    # and a store on top of a corrupt index recovers it
    fc.store(repo, "check_traceability", "a.py", "s", "v1", [{"a": 1}])
    fc.flush()
    assert _idx(repo)["entries"]["a.py"]["blob_sha"] == "s"


def test_no_cache_env_suppresses_the_flush_too(tmp_path, monkeypatch):
    repo = str(tmp_path / "r")
    monkeypatch.setenv(fc.NO_CACHE_ENV, "1")
    fc.store(repo, "check_traceability", "a.py", "s", "v1", [{"a": 1}])
    fc.flush()
    assert not fc._index_path(repo, "check_traceability").is_file()

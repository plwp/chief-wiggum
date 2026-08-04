"""Integration tests for the #327 per-file findings cache wired into
``check_traceability.py`` / ``check_single_writer.py``: the emission half of
each checker's per-file scan must be SKIPPED on a cache hit, and the cache
must INVALIDATE on either half of its key going stale — a content change, or
(the bug this cache exists to prevent) a scanner-source change with content
untouched. A performance win without both invalidation halves is worse than
none, because it looks correct while silently serving stale findings.

Every fixture is a real git repo (pinned ``--initial-branch=main`` per repo
convention) — the cache only activates when a manifest (git-native blob sha)
is available; a non-git ``--source`` degrades to the pre-cache live-scan
behavior, exercised elsewhere by the existing non-git-fixture test suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import check_single_writer as sw
import check_traceability as ct


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, message: str = "commit") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _spy_emit(monkeypatch, module):
    """Wrap ``module.emitters.emit`` to count invocations while still
    delegating to the real implementation — the thing a cache HIT must skip
    calling entirely."""
    calls = {"n": 0}
    original = module.emitters.emit

    def wrapper(path, content):
        calls["n"] += 1
        return original(path, content)

    monkeypatch.setattr(module.emitters, "emit", wrapper)
    return calls


# --- check_traceability.py: skip -------------------------------------------


def test_traceability_second_scan_skips_unchanged_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (repo / "billing.py").write_text("# @cw-trace guards CTR-bill-001\n")
    _commit(repo)

    calls = _spy_emit(monkeypatch, ct)
    first = ct.scan_source(repo)
    assert calls["n"] == 2  # cold: both files emitted

    calls["n"] = 0
    second = ct.scan_source(repo)
    assert calls["n"] == 0  # warm: emission skipped entirely for both files
    assert sorted(a.to_dict().items() for a in second) == sorted(a.to_dict().items() for a in first)


# --- check_traceability.py: invalidation (a) content change -----------------


def test_traceability_content_change_rescans_only_that_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (repo / "billing.py").write_text("# @cw-trace guards CTR-bill-001\n")
    _commit(repo)

    calls = _spy_emit(monkeypatch, ct)
    ct.scan_source(repo)  # warm the cache
    calls["n"] = 0

    (repo / "order.py").write_text("# @cw-trace guards CTR-order-002\n")  # dirty, uncommitted
    anns = ct.scan_source(repo)
    assert calls["n"] == 1  # only order.py re-emitted; billing.py served from cache
    assert any(a.target == "CTR-order-002" for a in anns)  # the new content was actually seen


# --- check_traceability.py: invalidation (b) scanner-source change ----------


def test_traceability_scanner_source_change_busts_whole_cache(tmp_path, monkeypatch):
    """The bug this key exists to prevent: a scanner-version bump (editing
    trace_emission.py's grammar, in practice) must invalidate EVERY cached
    file, not just the ones that happened to change content."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (repo / "billing.py").write_text("# @cw-trace guards CTR-bill-001\n")
    _commit(repo)

    monkeypatch.setattr(ct, "_scanner_version", lambda: "scanner-v1")
    calls = _spy_emit(monkeypatch, ct)
    ct.scan_source(repo)
    assert calls["n"] == 2
    calls["n"] = 0

    ct.scan_source(repo)  # same scanner version, content untouched -> cache hit
    assert calls["n"] == 0

    monkeypatch.setattr(ct, "_scanner_version", lambda: "scanner-v2")  # simulated scanner edit
    ct.scan_source(repo)
    assert calls["n"] == 2  # neither file's stale finding is served


# --- check_single_writer.py: skip -------------------------------------------


def test_single_writer_second_scan_skips_unchanged_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "internal" / "billing").mkdir(parents=True)
    (repo / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\nfunc ReconcileStripe(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    (repo / "internal" / "admin").mkdir(parents=True)
    (repo / "internal" / "admin" / "handlers.go").write_text(
        "package admin\nfunc ChangePlan(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    _commit(repo)

    inv = sw.SingleWriterInvariant(
        id="INV-bil-001", description="d",
        controls_field=["provider.stripe_plan"],
        sanctioned_writers=["ReconcileStripe"],
        source="invariants.md",
    )

    calls = _spy_emit(monkeypatch, sw)
    first = sw.scan_writers(repo, [inv])
    assert calls["n"] == 2  # cold

    calls["n"] = 0
    second = sw.scan_writers(repo, [inv])
    assert calls["n"] == 0  # warm: both files served from cache
    assert [w.to_dict() if hasattr(w, "to_dict") else w for w in second] == \
        [w.to_dict() if hasattr(w, "to_dict") else w for w in first]


# --- check_single_writer.py: invalidation (a) content change ----------------


def test_single_writer_content_change_rescans_only_that_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "internal" / "billing").mkdir(parents=True)
    (repo / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\nfunc ReconcileStripe(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    (repo / "internal" / "admin").mkdir(parents=True)
    (repo / "internal" / "admin" / "handlers.go").write_text(
        "package admin\nfunc Noop(p *Provider) { _ = p }\n"
    )
    _commit(repo)

    inv = sw.SingleWriterInvariant(
        id="INV-bil-001", description="d",
        controls_field=["provider.stripe_plan"],
        sanctioned_writers=["ReconcileStripe"],
        source="invariants.md",
    )

    calls = _spy_emit(monkeypatch, sw)
    sw.scan_writers(repo, [inv])
    calls["n"] = 0

    # A NEW unsanctioned writer lands in the previously-inert handlers.go.
    (repo / "internal" / "admin" / "handlers.go").write_text(
        "package admin\nfunc ChangePlan(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    writers = sw.scan_writers(repo, [inv])
    assert calls["n"] == 1  # only handlers.go re-emitted
    assert any(w.symbol == "ChangePlan" for w in writers)  # the new writer was actually seen


# --- check_single_writer.py: invalidation (b) scanner-source change --------


def test_single_writer_scanner_source_change_busts_whole_cache(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "internal" / "billing").mkdir(parents=True)
    (repo / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\nfunc ReconcileStripe(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    (repo / "internal" / "admin").mkdir(parents=True)
    (repo / "internal" / "admin" / "handlers.go").write_text(
        "package admin\nfunc ChangePlan(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    _commit(repo)

    inv = sw.SingleWriterInvariant(
        id="INV-bil-001", description="d",
        controls_field=["provider.stripe_plan"],
        sanctioned_writers=["ReconcileStripe"],
        source="invariants.md",
    )

    monkeypatch.setattr(sw, "_scanner_version", lambda: "scanner-v1")
    calls = _spy_emit(monkeypatch, sw)
    sw.scan_writers(repo, [inv])
    assert calls["n"] == 2
    calls["n"] = 0

    sw.scan_writers(repo, [inv])  # same version, content untouched -> cache hit
    assert calls["n"] == 0

    monkeypatch.setattr(sw, "_scanner_version", lambda: "scanner-v2")  # simulated scanner edit
    sw.scan_writers(repo, [inv])
    assert calls["n"] == 2  # every file re-scanned, not just the changed one


# --- completeness: the coverage denominator is unaffected by caching -------


def test_single_writer_gate_coverage_denominator_unchanged_on_cached_run(tmp_path):
    """``--gate coverage`` must still claim over EVERY manifest file, whether
    served from cache or freshly scanned — the whole point of the #327
    doctrine (full-coverage semantics, incremental cost). A 1-file content
    change re-scans exactly that file and the report's measured denominator
    (``source_files_scanned``) — and the full verdict — stay identical to an
    uncached run over the same repo state."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "internal" / "billing").mkdir(parents=True)
    (repo / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\nfunc ReconcileStripe(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    (repo / "internal" / "admin").mkdir(parents=True)
    (repo / "internal" / "admin" / "handlers.go").write_text(
        "package admin\nfunc ChangePlan(p *Provider) { p.StripePlan = \"pro\" }\n"
    )
    _commit(repo)

    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text(
        "**INV-bil-001**: single atomic Stripe→plan write\n"
        "<!-- @cw-writes INV-bil-001 controls_field=provider.stripe_plan "
        "sanctioned_writers=ReconcileStripe -->\n"
    )

    cold = sw.check(epic, repo)  # first run: nothing cached yet
    warm = sw.check(epic, repo)  # second run: fully warm cache

    assert cold.source_files_scanned == warm.source_files_scanned == 2
    assert cold.coverage_ok is False and warm.coverage_ok is False  # ChangePlan still caught
    assert cold.to_dict() == warm.to_dict()  # dual-run zero-diff — the validation gate itself

    # A 1-file change: the manifest still enumerates BOTH files; only the
    # changed one is re-emitted, and the denominator still counts both.
    (repo / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\nfunc ReconcileStripe(p *Provider) { p.StripePlan = \"pro\"; _ = 1 }\n"
    )
    changed = sw.check(epic, repo)
    assert changed.source_files_scanned == 2  # denominator still counts every manifest file
    assert changed.coverage_ok is False  # ChangePlan is still a violation

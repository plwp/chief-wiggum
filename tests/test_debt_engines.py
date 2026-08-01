"""Tests for the #214 debt engines: quality/{dead_code,clones,test_health,markers}.py.

Each engine is exercised against a small synthetic git repo. External-tool
tiers (vulture, staticcheck, knip, jscpd) are tested via their graceful-skip
paths plus availability-gated live runs — the same discipline as
tests/test_hotspots.py (CI has none of the tools; local runs do).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest
from quality import clones, dead_code, duplication, markers, population, test_health

HAS_VULTURE = importlib.util.find_spec("vulture") is not None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", "--no-verify")
    return repo


# --- population ---------------------------------------------------------------


def test_population_excludes_generated_and_vendored(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": "x = 1\n",
        "proto_pb2.py": "generated = True\n",
        "vendor/lib.go": "package lib\n",
        "bundle.min.js": "var x=1;\n",
        "README.md": "docs\n",
    })
    files = population.tracked_source(str(repo))
    assert files == ["app.py"]


def test_population_applies_path_filter(tmp_path):
    repo = _make_repo(tmp_path, {"a/x.py": "x = 1\n", "b/y.py": "y = 1\n"})
    files = population.tracked_source(str(repo), path_filter=lambda rel: rel.startswith("a/"))
    assert files == ["a/x.py"]


# --- markers ------------------------------------------------------------------


def test_markers_finds_all_four_kinds_with_trailing_text(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": (
            "# TODO: wire up the retry path\n"
            "x = 1  # FIXME broken on leap years\n"
            "# HACK(pat): temporary until #99\n"
            "y = 2  # XXX\n"
        ),
    })
    result = markers.analyze(str(repo))
    kinds = {f["kind"] for f in result["findings"]}
    assert kinds == {"TODO", "FIXME", "HACK", "XXX"}
    todo = next(f for f in result["findings"] if f["kind"] == "TODO")
    assert todo["file"] == "app.py"
    assert todo["line"] == 1
    assert todo["text"] == "wire up the retry path"
    hack = next(f for f in result["findings"] if f["kind"] == "HACK")
    assert hack["text"] == "temporary until #99"  # (author) prefix stripped


def test_markers_lowercase_todo_not_flagged(tmp_path):
    repo = _make_repo(tmp_path, {"app.py": "# our todo list lives elsewhere\n"})
    assert markers.analyze(str(repo))["findings"] == []


def test_markers_go_context_todo_call_not_flagged(tmp_path):
    """Live-caught FP class on a real Go corpus: Go's stdlib `context.TODO()`
    is an API call, not a deferred-work marker. `TODO(author):` attribution
    still counts."""
    repo = _make_repo(tmp_path, {
        "pkg/a.go": (
            "package pkg\n"
            "func f() { _ = seed(context.TODO(), conn) }\n"
            "// TODO(pat): replace context.TODO with a request context\n"
        ),
    })
    result = markers.analyze(str(repo))
    assert [f["line"] for f in result["findings"]] == [3]
    assert result["findings"][0]["text"].startswith("replace context.TODO")


def test_markers_skips_vendored_and_generated(tmp_path):
    repo = _make_repo(tmp_path, {
        "node_modules/x.js": "// TODO vendored\n",
        "gen_pb2.py": "# FIXME generated\n",
        "src/app.py": "# TODO real\n",
    })
    result = markers.analyze(str(repo))
    assert [f["file"] for f in result["findings"]] == ["src/app.py"]


def test_markers_trailing_text_capped_at_80(tmp_path):
    repo = _make_repo(tmp_path, {"app.py": "# TODO: " + "z" * 200 + "\n"})
    (finding,) = markers.analyze(str(repo))["findings"]
    assert len(finding["text"]) == 80


# --- test_health --------------------------------------------------------------


def test_orphaned_python_test_flagged(tmp_path):
    repo = _make_repo(tmp_path, {
        "orders.py": "def place():\n    return 1\n",
        "tests/test_orders.py": "def test_place():\n    assert True\n",
        "tests/test_billing.py": "def test_gone():\n    assert True\n",
    })
    result = test_health.analyze(str(repo))
    orphans = [f for f in result["findings"] if f["kind"] == "orphaned_test"]
    assert [o["file"] for o in orphans] == ["tests/test_billing.py"]
    assert orphans[0]["subject_stem"] == "billing"
    assert "mapping" in orphans[0]


def test_orphan_mapping_tolerates_plural_and_package_dirs(tmp_path):
    repo = _make_repo(tmp_path, {
        "order.py": "x = 1\n",
        "billing/__init__.py": "y = 1\n",
        "tests/test_orders.py": "def test_a():\n    assert True\n",
        "tests/test_billing.py": "def test_b():\n    assert True\n",
    })
    result = test_health.analyze(str(repo))
    assert [f for f in result["findings"] if f["kind"] == "orphaned_test"] == []


def test_generic_stems_never_orphaned(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": "x = 1\n",
        "tests/test_integration.py": "def test_x():\n    assert True\n",
        "tests/test_e2e.py": "def test_y():\n    assert True\n",
    })
    result = test_health.analyze(str(repo))
    assert [f for f in result["findings"] if f["kind"] == "orphaned_test"] == []


def test_go_package_tests_not_orphaned_while_package_has_code(tmp_path):
    repo = _make_repo(tmp_path, {
        "pkg/server.go": "package pkg\n",
        "pkg/handlers_test.go": (
            "package pkg\nimport \"testing\"\n"
            "func TestX(t *testing.T) {\n\tt.Fatal(\"no\")\n}\n"
        ),
        "dead/gone_test.go": (
            "package dead\nimport \"testing\"\n"
            "func TestGone(t *testing.T) {\n\tt.Fatal(\"no\")\n}\n"
        ),
    })
    result = test_health.analyze(str(repo))
    orphans = [f for f in result["findings"] if f["kind"] == "orphaned_test"]
    assert [o["file"] for o in orphans] == ["dead/gone_test.go"]


def test_assertion_free_python_test_flagged(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": "def f():\n    return 1\n",
        "tests/test_app.py": (
            "import pytest\n"
            "from app import f\n\n"
            "def test_real():\n    assert f() == 1\n\n"
            "def test_raises():\n"
            "    with pytest.raises(ValueError):\n        f()\n\n"
            "def test_helper_style():\n    check_output(f())\n\n"
            "def test_hollow():\n    f()\n"
        ),
    })
    result = test_health.analyze(str(repo))
    hollow = [f for f in result["findings"] if f["kind"] == "assertion_free_test"]
    assert [h["symbol"] for h in hollow] == ["test_hollow"]


def test_assertion_free_go_test_flagged(tmp_path):
    repo = _make_repo(tmp_path, {
        "pkg/a.go": "package pkg\n",
        "pkg/a_test.go": (
            "package pkg\n\nimport \"testing\"\n\n"
            "func TestReal(t *testing.T) {\n\tif 1 != 1 {\n\t\tt.Fatal(\"no\")\n\t}\n}\n\n"
            "func TestHollow(t *testing.T) {\n\t_ = compute()\n}\n"
        ),
    })
    result = test_health.analyze(str(repo))
    hollow = [f for f in result["findings"] if f["kind"] == "assertion_free_test"]
    assert [h["symbol"] for h in hollow] == ["TestHollow"]


def test_skipped_suites_all_languages(tmp_path):
    repo = _make_repo(tmp_path, {
        "a.py": "x = 1\n",
        "tests/test_a.py": (
            "import pytest\n\n"
            "@pytest.mark.skip(reason='quarantined')\n"
            "def test_q():\n    assert True\n"
        ),
        "pkg/a.go": "package pkg\n",
        "pkg/a_test.go": (
            "package pkg\nimport \"testing\"\n"
            "func TestS(t *testing.T) {\n\tt.Skip(\"flaky\")\n\tt.Fatal(\"x\")\n}\n"
        ),
        "web/app.ts": "export const x = 1;\n",
        "web/app.test.ts": "describe.skip('quarantined', () => { it('x', () => {}); });\n",
    })
    result = test_health.analyze(str(repo))
    skipped_files = {f["file"] for f in result["findings"] if f["kind"] == "skipped_test"}
    assert skipped_files == {"tests/test_a.py", "pkg/a_test.go", "web/app.test.ts"}


def test_ts_assertion_scan_reported_unscanned(tmp_path):
    repo = _make_repo(tmp_path, {
        "web/app.ts": "export const x = 1;\n",
        "web/app.test.ts": "it('x', () => {});\n",
    })
    result = test_health.analyze(str(repo))
    assert result["unscanned"]["assertion_scan"].get("typescript") == 1


# --- dead_code ----------------------------------------------------------------


def test_builtin_python_pass_flags_only_unreferenced(tmp_path, monkeypatch):
    monkeypatch.setattr(dead_code, "_vulture_pass", lambda *a, **k: None)
    repo = _make_repo(tmp_path, {
        "lib.py": (
            "def used():\n    return 1\n\n"
            "def dead_helper():\n    return 2\n\n"
            "class DeadClass:\n    pass\n\n"
            "def _private():\n    return 3\n"
        ),
        "app.py": "from lib import used\nprint(used())\n",
    })
    result = dead_code.analyze(str(repo))
    assert result["languages"]["python"]["tier"] == "builtin-ast"
    symbols = {f["symbol"] for f in result["findings"]}
    assert symbols == {"dead_helper", "DeadClass"}  # _private skipped (underscore)
    dh = next(f for f in result["findings"] if f["symbol"] == "dead_helper")
    assert dh["file"] == "lib.py" and dh["line"] == 4 and dh["tier"] == "builtin-ast"


def test_builtin_pass_counts_string_and_test_mentions_as_uses(tmp_path, monkeypatch):
    monkeypatch.setattr(dead_code, "_vulture_pass", lambda *a, **k: None)
    repo = _make_repo(tmp_path, {
        "lib.py": (
            "def dispatched():\n    return 1\n\n"
            "def test_covered():\n    return 2\n"
        ),
        "app.py": 'HANDLERS = {"dispatched": None}\n',
        "tests/test_lib.py": "from lib import test_covered\n\ndef test_x():\n    assert test_covered()\n",
    })
    result = dead_code.analyze(str(repo))
    assert result["findings"] == []  # string mention + test import both count


def test_builtin_pass_skips_decorated_defs(tmp_path, monkeypatch):
    monkeypatch.setattr(dead_code, "_vulture_pass", lambda *a, **k: None)
    repo = _make_repo(tmp_path, {
        "routes.py": "@app.route('/x')\ndef handler():\n    return 1\n",
    })
    result = dead_code.analyze(str(repo))
    assert result["findings"] == []


def test_go_skipped_and_unscanned_when_staticcheck_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(dead_code.shutil, "which", lambda name: None)
    repo = _make_repo(tmp_path, {"main.go": "package main\nfunc main() {}\n"})
    result = dead_code.analyze(str(repo))
    assert "skipped" in result["languages"]["go"]
    assert result["unscanned"] == {"go": 1}  # reported, never silently empty


def test_ts_skipped_and_unscanned_when_knip_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(dead_code.shutil, "which", lambda name: None)
    repo = _make_repo(tmp_path, {"web/app.ts": "export const x = 1;\n"})
    result = dead_code.analyze(str(repo))
    assert "skipped" in result["languages"]["typescript"]
    assert result["unscanned"] == {"typescript": 1}


def test_staticcheck_json_parsing(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, {"pkg/dead.go": "package pkg\nfunc unusedThing() {}\n"})
    fake_line = (
        '{"code":"U1000","severity":"error","location":'
        f'{{"file":"{repo}/pkg/dead.go","line":2,"column":6}},'
        '"message":"func unusedThing is unused"}'
    )

    class FakeProc:
        returncode = 1
        stdout = fake_line + "\n"
        stderr = ""

    monkeypatch.setattr(dead_code.shutil, "which",
                        lambda name: "/usr/bin/staticcheck" if name == "staticcheck" else None)
    monkeypatch.setattr(dead_code, "_go_module_roots", lambda repo: ["."])
    monkeypatch.setattr(dead_code.subprocess, "run", lambda *a, **k: FakeProc())
    findings, reason, warnings = dead_code._staticcheck_pass(str(repo))
    assert reason is None and warnings == []
    assert findings == [{
        "file": "pkg/dead.go", "line": 2, "symbol": "unusedThing",
        "kind": "unused", "message": "func unusedThing is unused", "tier": "staticcheck",
    }]


def test_staticcheck_compile_error_is_never_a_clean_scan(tmp_path, monkeypatch):
    """Live-caught on a real validation repo: a repo whose go.mod lives in a
    subdir makes root-level staticcheck emit only a 'compile' diagnostic —
    that must degrade to skipped/unscanned, NEVER an empty (clean) finding
    set."""
    repo = _make_repo(tmp_path, {"backend/main.go": "package main\nfunc main() {}\n"})

    class FakeProc:
        returncode = 0
        stdout = ('{"code":"compile","severity":"error","location":{"file":"","line":0},'
                  '"message":"pattern ./...: directory prefix . does not contain main module"}\n')
        stderr = ""

    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if "staticcheck" in str(cmd[0]):
            return FakeProc()
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(dead_code.shutil, "which",
                        lambda name: "/usr/bin/staticcheck" if name == "staticcheck" else None)
    monkeypatch.setattr(dead_code, "_go_module_roots", lambda repo: ["."])
    monkeypatch.setattr(dead_code.subprocess, "run", fake_run)
    findings, reason, _ = dead_code._staticcheck_pass(str(repo))
    assert findings is None
    assert "could not analyze" in reason or "analyzed no module" in reason
    result = dead_code.analyze(str(repo))
    assert "skipped" in result["languages"]["go"]
    assert result["unscanned"].get("go") == 1  # reported unscanned, not silently clean


def test_go_module_roots_found_in_subdirs(tmp_path):
    repo = _make_repo(tmp_path, {
        "backend/go.mod": "module example.com/b\n\ngo 1.21\n",
        "backend/main.go": "package main\nfunc main() {}\n",
        "vendor/x/go.mod": "module x\n",
    })
    assert dead_code._go_module_roots(str(repo)) == ["backend"]  # vendor excluded


@pytest.mark.skipif(shutil.which("staticcheck") is None, reason="staticcheck not on PATH")
def test_staticcheck_live_on_synthetic_module(tmp_path):
    # go.mod deliberately in a SUBDIR — the live regression that produced a
    # false-clean when staticcheck only ran from the repo root.
    repo = _make_repo(tmp_path, {
        "backend/go.mod": "module example.com/dead\n\ngo 1.21\n",
        "backend/main.go": (
            "package main\n\nfunc main() {}\n\nfunc deadFunc() int { return 1 }\n"
        ),
    })
    result = dead_code.analyze(str(repo))
    go = result["languages"]["go"]
    if "skipped" in go:  # toolchain present but module analysis failed — honest skip
        pytest.skip(f"staticcheck could not analyze: {go['skipped']}")
    assert any(f["symbol"] == "deadFunc" for f in result["findings"])


@pytest.mark.skipif(not HAS_VULTURE, reason="vulture not importable")
def test_vulture_tier_used_when_importable(tmp_path):
    repo = _make_repo(tmp_path, {
        "lib.py": "def dead_helper():\n    return 2\n",
    })
    result = dead_code.analyze(str(repo))
    assert result["languages"]["python"]["tier"] == "vulture"
    assert any(f["symbol"] == "dead_helper" for f in result["findings"])


# --- clones -------------------------------------------------------------------

FAKE_DUPLICATES = [
    {  # pair 1: a <-> b, same content
        "lines": 6, "tokens": 40, "fragment": "def f():\n    x = 1\n    return x\n",
        "firstFile": {"name": "src/a.py", "start": 10, "end": 15},
        "secondFile": {"name": "src/b.py", "start": 20, "end": 25},
    },
    {  # pair 2: a <-> c, SAME normalized content (extra blank lines/indent)
        "lines": 6, "tokens": 40, "fragment": "def f():\n\n    x = 1\n\n    return x\n",
        "firstFile": {"name": "src/a.py", "start": 10, "end": 15},
        "secondFile": {"name": "src/c.py", "start": 30, "end": 35},
    },
    {  # unrelated pair
        "lines": 4, "tokens": 20, "fragment": "y = 2\nz = 3\n",
        "firstFile": {"name": "src/d.py", "start": 1, "end": 4},
        "secondFile": {"name": "src/e.py", "start": 1, "end": 4},
    },
]


def test_cluster_merges_pairs_into_clone_classes():
    classes = clones.cluster("/repo", FAKE_DUPLICATES)
    assert len(classes) == 2
    big = classes[0]  # sorted size desc
    assert big["size"] == 3
    assert [m["file"] for m in big["members"]] == ["src/a.py", "src/b.py", "src/c.py"]
    assert all(len(c["content_hash"]) == clones.CONTENT_HASH_LEN for c in classes)


def test_cluster_scope_filter_drops_members_and_small_classes():
    classes = clones.cluster(
        "/repo", FAKE_DUPLICATES, path_filter=lambda rel: rel != "src/e.py"
    )
    # d<->e loses e -> 1 member -> class disappears; a/b/c class survives.
    assert [c["size"] for c in classes] == [3]


def test_cluster_scope_dropped_classes_land_in_boundary_out():
    """C2 (#216): a class with >= 2 total spans that falls below 2 in-scope
    members is boundary evidence, not silence — full pre-filter member list."""
    boundary: list[dict] = []
    classes = clones.cluster(
        "/repo", FAKE_DUPLICATES, path_filter=lambda rel: rel != "src/e.py",
        boundary_out=boundary,
    )
    assert [c["size"] for c in classes] == [3]
    assert len(boundary) == 1
    assert boundary[0]["size"] == 2
    assert [m["file"] for m in boundary[0]["members"]] == ["src/d.py", "src/e.py"]


def test_analyze_returns_boundary_classes(tmp_path, monkeypatch):
    fake_report = {"statistics": {}, "duplicates": FAKE_DUPLICATES}
    monkeypatch.setattr(duplication, "run_jscpd", lambda repo, workdir: (fake_report, None))
    result = clones.analyze("/repo", str(tmp_path),
                            path_filter=lambda rel: rel != "src/e.py")
    assert [c["size"] for c in result["clone_classes"]] == [3]
    assert [c["size"] for c in result["boundary_classes"]] == [2]
    # unfiltered: nothing is boundary
    result_all = clones.analyze("/repo", str(tmp_path))
    assert result_all["boundary_classes"] == []


def test_cluster_content_hash_is_content_stable():
    one = clones.cluster("/repo", FAKE_DUPLICATES)[0]["content_hash"]
    moved = [dict(d, firstFile=dict(d["firstFile"], start=d["firstFile"]["start"] + 100),
                  secondFile=dict(d["secondFile"], start=d["secondFile"]["start"] + 100))
             for d in FAKE_DUPLICATES]
    two = clones.cluster("/repo", moved)[0]["content_hash"]
    assert one == two  # line moves don't change the class identity


def test_cluster_empty_fragment_falls_back_to_file_read(tmp_path):
    """Live-caught: jscpd emits fragment as an EMPTY string on real repos —
    the span must be re-read from the file, not silently dropped."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    body = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
    (repo / "src" / "a.py").write_text(body)
    (repo / "src" / "b.py").write_text(body)
    dups = [{
        "lines": 5, "tokens": 30, "fragment": "",
        "firstFile": {"name": "src/a.py", "start": 3, "end": 7},
        "secondFile": {"name": "src/b.py", "start": 3, "end": 7},
    }]
    classes = clones.cluster(str(repo), dups)
    assert len(classes) == 1 and classes[0]["size"] == 2


def test_playwright_flow_specs_never_orphaned(tmp_path):
    """Live-caught FP class: e2e flow specs under a standalone tests/ dir are
    named for behaviors, not modules — no orphan mapping applies."""
    repo = _make_repo(tmp_path, {
        "ui/src/App.tsx": "export const App = 1;\n",
        "ui/tests/admin-cancellation.spec.ts": "it('x', () => { expect(1).toBe(1); });\n",
        "ui/src/gone.spec.ts": "it('y', () => { expect(1).toBe(1); });\n",  # colocated -> maps
    })
    result = test_health.analyze(str(repo))
    orphans = [f["file"] for f in result["findings"] if f["kind"] == "orphaned_test"]
    assert orphans == ["ui/src/gone.spec.ts"]


def test_clones_skipped_when_jscpd_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(duplication, "_jscpd_cmd", lambda: None)
    result = clones.analyze(str(_make_repo(tmp_path, {"a.py": "x=1\n"})), str(tmp_path / "wd"))
    assert "skipped" in result


def test_duplication_analyze_still_reports_aggregate_via_shared_runner(tmp_path, monkeypatch):
    """The minimal refactor must not change duplication.analyze's output shape."""
    fake_report = {
        "statistics": {"total": {
            "lines": 100, "tokens": 800, "sources": 4, "clones": 1,
            "duplicatedLines": 10, "duplicatedTokens": 60,
            "percentage": 10.0, "percentageTokens": 7.5,
        }},
        "duplicates": FAKE_DUPLICATES,
    }
    monkeypatch.setattr(duplication, "run_jscpd", lambda repo, workdir: (fake_report, None))
    result = duplication.analyze("/repo", workdir=str(tmp_path))
    assert result["duplication_pct_lines"] == 10.0
    assert result["clones"] == 1
    assert "baselines" in result
    clone_result = clones.analyze("/repo", str(tmp_path))
    assert clone_result["clone_classes"][0]["size"] == 3
    assert clone_result["clone_pairs_reported"] == 3


def test_duplication_skip_shape_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(duplication, "_jscpd_cmd", lambda: None)
    result = duplication.analyze(str(tmp_path), workdir=str(tmp_path / "wd"))
    assert result["skipped"] == "jscpd/node not found"
    assert "note" in result


# --- scope doctrine: detection repo-wide, authority in-scope (F2) -------------


def test_builtin_dead_use_from_scope_excluded_file_counts_as_use(tmp_path, monkeypatch):
    """A symbol whose ONLY use lives in a scope-excluded file is NOT dead —
    the USE corpus is the full pre-scope population."""
    monkeypatch.setattr(dead_code, "_vulture_pass", lambda *a, **k: None)
    repo = _make_repo(tmp_path, {
        "lib.py": "def used_from_outside():\n    return 1\n",
        "excluded/consumer.py": "from lib import used_from_outside\nused_from_outside()\n",
    })
    in_scope = lambda rel: not rel.startswith("excluded/")  # noqa: E731
    result = dead_code.analyze(str(repo), path_filter=in_scope)
    assert result["findings"] == []  # use-from-excluded kills the dead flag


def test_builtin_dead_findings_only_for_in_scope_files(tmp_path, monkeypatch):
    """Authority in-scope: a genuinely dead symbol in an EXCLUDED file is
    never a finding; the same symbol in scope still is."""
    monkeypatch.setattr(dead_code, "_vulture_pass", lambda *a, **k: None)
    repo = _make_repo(tmp_path, {
        "lib.py": "def dead_in_scope():\n    return 1\n",
        "excluded/old.py": "def dead_outside():\n    return 2\n",
    })
    in_scope = lambda rel: not rel.startswith("excluded/")  # noqa: E731
    result = dead_code.analyze(str(repo), path_filter=in_scope)
    assert {f["symbol"] for f in result["findings"]} == {"dead_in_scope"}


@pytest.mark.skipif(not HAS_VULTURE, reason="vulture not importable")
def test_vulture_dead_use_from_scope_excluded_file_counts_as_use(tmp_path):
    repo = _make_repo(tmp_path, {
        "lib.py": "def used_from_outside():\n    return 1\n",
        "excluded/consumer.py": (
            "from lib import used_from_outside\nprint(used_from_outside())\n"
        ),
    })
    in_scope = lambda rel: not rel.startswith("excluded/")  # noqa: E731
    result = dead_code.analyze(str(repo), path_filter=in_scope)
    assert result["languages"]["python"]["tier"] == "vulture"
    assert not any(f["symbol"] == "used_from_outside" for f in result["findings"])
    # authority: findings never name the excluded file either
    assert all(in_scope(f["file"]) for f in result["findings"])


def test_orphan_subject_in_scope_excluded_path_is_not_orphaned(tmp_path):
    """A test whose subject exists in a scope-excluded path is NOT orphaned —
    the EXISTENCE corpus is the full pre-scope population."""
    repo = _make_repo(tmp_path, {
        "excluded/billing.py": "x = 1\n",
        "tests/test_billing.py": "def test_b():\n    assert True\n",
        "tests/test_gone.py": "def test_g():\n    assert True\n",
    })
    in_scope = lambda rel: not rel.startswith("excluded/")  # noqa: E731
    result = test_health.analyze(str(repo), path_filter=in_scope)
    orphans = [f["file"] for f in result["findings"] if f["kind"] == "orphaned_test"]
    # subject-in-excluded kills the orphan flag; the truly-gone one stays
    assert orphans == ["tests/test_gone.py"]


def test_orphan_findings_never_emitted_for_out_of_scope_tests(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": "x = 1\n",
        "excluded/tests/test_vanished.py": "def test_v():\n    assert True\n",
    })
    in_scope = lambda rel: not rel.startswith("excluded/")  # noqa: E731
    result = test_health.analyze(str(repo), path_filter=in_scope)
    assert [f for f in result["findings"] if f["kind"] == "orphaned_test"] == []


# --- Go helper delegation (F5) ------------------------------------------------


def test_go_helper_delegated_test_not_flagged_assertion_free(tmp_path):
    """The dgrd FP class: a test whose only 'assertion' is a local helper
    receiving t (the helper calls require.* inside) is NOT assertion-free —
    it lands in the helper_delegated bucket, stated as unverified."""
    repo = _make_repo(tmp_path, {
        "pkg/a.go": "package pkg\n",
        "pkg/a_test.go": (
            "package pkg\n\nimport (\n\t\"testing\"\n\n"
            "\t\"github.com/stretchr/testify/require\"\n)\n\n"
            "func TestDelegated(t *testing.T) {\n\trunScenario(t, 42)\n}\n\n"
            "func TestHollow(t *testing.T) {\n\t_ = compute()\n}\n\n"
            "func runScenario(t *testing.T, x int) {\n\trequire.Equal(t, 42, x)\n}\n"
        ),
    })
    result = test_health.analyze(str(repo))
    hollow = [f for f in result["findings"] if f["kind"] == "assertion_free_test"]
    assert [h["symbol"] for h in hollow] == ["TestHollow"]
    assert result["helper_delegated"]["go"] == 1
    assert "UNVERIFIED" in result["helper_delegated"]["note"]


def test_go_helper_delegation_matches_receiver_field_t(tmp_path):
    repo = _make_repo(tmp_path, {
        "pkg/a.go": "package pkg\n",
        "pkg/a_test.go": (
            "package pkg\n\nimport \"testing\"\n\n"
            "func TestSuiteStyle(t *testing.T) {\n\ts := suite{t: t}\n\trunHelper(s.t, 1)\n}\n"
        ),
    })
    result = test_health.analyze(str(repo))
    assert [f for f in result["findings"] if f["kind"] == "assertion_free_test"] == []
    assert result["helper_delegated"]["go"] == 1


# --- assertion-scan gap line (F8) ---------------------------------------------


def test_assertion_scan_gap_line_states_the_unscanned_languages():
    engines = {"test_health": {"unscanned": {"assertion_scan": {"typescript": 3, "javascript": 1}}}}
    line = test_health.assertion_scan_gap(engines)
    assert "javascript: 1 file(s)" in line and "typescript: 3 file(s)" in line
    assert "not evidence of health" in line
    assert test_health.assertion_scan_gap({"test_health": {"unscanned": {"assertion_scan": {}}}}) is None
    assert test_health.assertion_scan_gap({}) is None

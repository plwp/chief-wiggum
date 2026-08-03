"""C#/.NET support across CW's detection and measurement layers (#259).

The bug this file guards: a 12,551-file .NET monolith adopted cleanly with an
EMPTY ratchet pass-set and a 0-item debt inventory, and every downstream
surface rendered that as a pass. Three things have to hold for that not to
recur:

- **.NET is DETECTED** — ``.sln``/``.csproj`` plan a real ``dotnet test``, so
  ``ratchet.py`` autodetects a suite instead of reporting none.
- **C# is MEASURED** — ``.cs`` is in the quality/debt population, in clone
  detection, and its test-file conventions are recognized.
- **The TRX parser is REAL** — it is exercised against genuine
  ``dotnet test --logger trx`` output captured from an actual .NET SDK run
  (``tests/fixtures/trx/``), not a hand-written XML sample that only proves
  the parser can read XML someone wrote to match it.

The fixtures are verbatim TRX from a real ``dotnet test`` over a two-project
solution (usernames/hostnames scrubbed), whose ground truth is known:

===========================================  =========  ==================
case                                         outcome    counts as passing
===========================================  =========  ==================
CalculatorTests.AddsTwoNumbers               Passed     yes
CalculatorTests.SubtractsTwoNumbers          Passed     yes
CalculatorTests.ParameterisedPasses(n: 1)    Passed     yes
CalculatorTests.ParameterisedPasses(n: 2)    Passed     yes
CalculatorTests.KnownFailure                 Failed     no
CalculatorTests.KnownSkip                    NotExecuted no
GreeterTests.Greets (second project)         Passed     yes
===========================================  =========  ==================
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ratchet  # noqa: E402
import status  # noqa: E402
from chief_wiggum import languages as cw_languages  # noqa: E402
from chief_wiggum import verification as v  # noqa: E402
from quality import complexity, population  # noqa: E402

TRX_FIXTURES = ROOT / "tests" / "fixtures" / "trx"

# Ground truth of the captured run — 5 passing across TWO test projects.
EXPECTED_PASSING = {
    "Other.Tests.GreeterTests::Greets",
    "Sample.Tests.CalculatorTests::AddsTwoNumbers",
    "Sample.Tests.CalculatorTests::ParameterisedPasses(n: 1)",
    "Sample.Tests.CalculatorTests::ParameterisedPasses(n: 2)",
    "Sample.Tests.CalculatorTests::SubtractsTwoNumbers",
}


def _trx_docs() -> list[str]:
    docs = [p.read_text() for p in sorted(TRX_FIXTURES.glob("*.trx"))]
    assert len(docs) == 2, "fixture set must keep BOTH projects' TRX files"
    return docs


# --- the TRX parser, against real dotnet output ---------------------------------


def test_parses_real_dotnet_trx_output():
    """The headline assertion: real TRX in, the exact known pass-set out."""
    assert ratchet.parse_trx(_trx_docs()) == EXPECTED_PASSING


def test_failing_and_skipped_cases_are_not_passing():
    """`Failed` and `NotExecuted` must not enter the pass-set — a parser that
    counted them would inflate the high-water mark and then never let a real
    regression through."""
    passing = ratchet.parse_trx(_trx_docs())
    assert not [c for c in passing if "KnownFailure" in c or "KnownSkip" in c]


def test_second_test_project_is_not_dropped():
    """`dotnet test` on a SOLUTION writes one TRX per test project. Parsing
    only one file is the exact shape of silent under-measurement #259 is
    about — so the pass-set must span both projects."""
    passing = ratchet.parse_trx(_trx_docs())
    assert any(c.startswith("Sample.Tests.") for c in passing)
    assert any(c.startswith("Other.Tests.") for c in passing)


def test_theory_data_cases_are_distinct_cases():
    """xunit `[Theory]` data rows are separately ratcheted: losing one is a
    real regression, so they must not collapse to one case ID."""
    passing = ratchet.parse_trx(_trx_docs())
    theory = {c for c in passing if "ParameterisedPasses" in c}
    assert len(theory) == 2, theory


def test_parsing_a_single_document_yields_only_its_cases():
    one = [(TRX_FIXTURES / "other-tests.trx").read_text()]
    assert ratchet.parse_trx(one) == {"Other.Tests.GreeterTests::Greets"}


def test_retry_that_ends_in_failure_does_not_count_as_passing():
    """A case recorded both Passed and Failed (a flaky retry) is NOT passing —
    same `passed - failed` discipline as the go/pass-fail parsers."""
    doc = f"""<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="{ratchet.TRX_NS[1:-1]}">
  <Results>
    <UnitTestResult testId="a" testName="N.C.Flaky" outcome="Passed" />
    <UnitTestResult testId="a" testName="N.C.Flaky" outcome="Failed" />
    <UnitTestResult testId="b" testName="N.C.Solid" outcome="Passed" />
  </Results>
  <TestDefinitions>
    <UnitTest name="N.C.Flaky" id="a"><TestMethod className="N.C" name="Flaky" /></UnitTest>
    <UnitTest name="N.C.Solid" id="b"><TestMethod className="N.C" name="Solid" /></UnitTest>
  </TestDefinitions>
</TestRun>"""
    assert ratchet.parse_trx([doc]) == {"N.C::Solid"}


def test_empty_trx_run_yields_no_cases():
    """A run that executed nothing measures nothing — it must not raise, and
    it must not invent cases."""
    doc = f'<TestRun xmlns="{ratchet.TRX_NS[1:-1]}"><Results /></TestRun>'
    assert ratchet.parse_trx([doc]) == set()


# --- TRX case -> source file mapping (#207) -------------------------------------


def _suite_cfg(tmp_path, report=".trx"):
    (tmp_path / "docs" / "quality").mkdir(parents=True)
    (tmp_path / "docs" / "quality" / "ratchet.json").write_text(json.dumps({
        "suites": [{"name": "dotnet", "cmd": "dotnet test", "cwd": ".",
                    "parser": "trx", "report": report}],
        "epic_docs": "docs/epics",
    }))
    return ratchet.load_config(tmp_path)


def test_trx_case_files_resolve_when_the_file_is_named_for_its_class(tmp_path):
    cfg = _suite_cfg(tmp_path)
    (tmp_path / "Sample.Tests").mkdir()
    (tmp_path / "Sample.Tests" / "CalculatorTests.cs").write_text("// tests")
    (tmp_path / "Other.Tests").mkdir()
    (tmp_path / "Other.Tests" / "GreeterTests.cs").write_text("// tests")

    files = ratchet.trx_case_files(cfg, cfg.suites[0], _trx_docs())

    assert files["dotnet::Sample.Tests.CalculatorTests::AddsTwoNumbers"] == (
        "Sample.Tests/CalculatorTests.cs")
    assert files["dotnet::Other.Tests.GreeterTests::Greets"] == (
        "Other.Tests/GreeterTests.cs")


def test_trx_case_files_keys_carry_the_suite_prefix(tmp_path):
    """run_suite filters the file map against already-namespaced case IDs, so
    an unprefixed key silently drops EVERY mapping (caught on a real run)."""
    cfg = _suite_cfg(tmp_path)
    (tmp_path / "CalculatorTests.cs").write_text("// tests")
    files = ratchet.trx_case_files(cfg, cfg.suites[0], _trx_docs())
    assert files
    assert all(k.startswith("dotnet::") for k in files)


def test_ambiguous_class_name_stays_unresolved(tmp_path):
    """Two files could hold the class — resolution must abstain, never guess."""
    cfg = _suite_cfg(tmp_path)
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "CalculatorTests.cs").write_text("// tests")
    files = ratchet.trx_case_files(cfg, cfg.suites[0], _trx_docs())
    assert not [k for k in files if "CalculatorTests" in k]


def test_build_output_is_not_mistaken_for_source(tmp_path):
    cfg = _suite_cfg(tmp_path)
    (tmp_path / "obj").mkdir()
    (tmp_path / "obj" / "GreeterTests.cs").write_text("// generated")
    files = ratchet.trx_case_files(cfg, cfg.suites[0], _trx_docs())
    assert "dotnet::Other.Tests.GreeterTests::Greets" not in files


# --- run_suite: a runner that writes nothing must not read as a clean run -------


def test_missing_trx_report_raises_rather_than_reporting_zero(tmp_path):
    """The #259 failure mode in miniature: a suite whose logger produced no
    TRX must ERROR, not return an empty (and therefore 'clean') pass-set."""
    cfg = _suite_cfg(tmp_path, report=".trx-nowhere")
    with pytest.raises(ratchet.RatchetError, match="no .trx written"):
        ratchet.run_suite(cfg, cfg.suites[0])


def test_stale_trx_from_an_earlier_run_is_cleared(tmp_path):
    """A TRX results DIRECTORY accumulates. Without a pre-run clear, a case
    deleted from the codebase would keep passing forever off a stale file."""
    cfg = _suite_cfg(tmp_path)
    results = tmp_path / ".trx"
    results.mkdir()
    (results / "stale.trx").write_text((TRX_FIXTURES / "other-tests.trx").read_text())
    # `true` writes no new TRX, so after the clear there is nothing to parse.
    cfg.suites[0].cmd = "true"
    with pytest.raises(ratchet.RatchetError, match="no .trx written"):
        ratchet.run_suite(cfg, cfg.suites[0])
    assert not list(results.rglob("*.trx"))


def test_trx_parser_requires_a_report_path(tmp_path):
    cfg = _suite_cfg(tmp_path)
    cfg.suites[0].report = None
    with pytest.raises(ratchet.RatchetError, match="needs `report`"):
        ratchet.run_suite(cfg, cfg.suites[0])


# --- .NET detection --------------------------------------------------------------


def _csproj(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<Project Sdk=\"Microsoft.NET.Sdk\" />")


def test_detect_solution_file(tmp_path):
    (tmp_path / "Roller.sln").write_text("Microsoft Visual Studio Solution File")
    det = v.detect_project(tmp_path)
    assert det.has_dotnet
    assert det.dotnet_solutions == ("Roller.sln",)


def test_detect_directory_build_props(tmp_path):
    (tmp_path / "Directory.Build.props").write_text("<Project />")
    assert v.detect_project(tmp_path).has_dotnet


def test_detect_nested_csproj(tmp_path):
    """Real .NET repos put each project one directory down, not at the root."""
    _csproj(tmp_path, "src/Roller.API/Roller.API.csproj")
    assert v.detect_project(tmp_path).has_dotnet


def test_csproj_inside_build_output_does_not_count(tmp_path):
    _csproj(tmp_path, "src/App/obj/Restore/Ghost.csproj")
    _csproj(tmp_path, "node_modules/pkg/Vendored.csproj")
    assert not v.detect_project(tmp_path).has_dotnet


def test_non_dotnet_repo_is_not_detected(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    det = v.detect_project(tmp_path)
    assert det.has_go and not det.has_dotnet


def test_plans_dotnet_commands_per_profile(tmp_path):
    _csproj(tmp_path, "src/App/App.csproj")
    det = v.detect_project(tmp_path)
    plans = {s.profile: s.command for s in v.plan_steps(tmp_path, ["test", "build", "lint"], det)}
    target = "src/App/App.csproj"
    assert plans["test"] == ["dotnet", "test", target]
    assert plans["build"] == ["dotnet", "build", target]
    assert plans["lint"][:2] == ["dotnet", "format"]


def test_multiple_solutions_are_each_named_explicitly(tmp_path):
    """A bare `dotnet test` in a root holding several solutions fails outright
    (MSB1011) — and the #259 target ships 8 .sln files, so an unnamed command
    would never have run there at all."""
    for name in ("Api.sln", "Legacy.sln"):
        (tmp_path / name).write_text("solution")
    det = v.detect_project(tmp_path)
    assert det.dotnet_solutions == ("Api.sln", "Legacy.sln")
    cmds = [s.command for s in v.plan_steps(tmp_path, ["test"], det)]
    assert cmds == [["dotnet", "test", "Api.sln"], ["dotnet", "test", "Legacy.sln"]]


def test_single_solution_is_named(tmp_path):
    _csproj(tmp_path, "src/App/App.csproj")
    (tmp_path / "App.sln").write_text("solution")
    named = v.plan_steps(tmp_path, ["test"], v.detect_project(tmp_path))
    assert [s.command for s in named] == [["dotnet", "test", "App.sln"]]


def test_projects_only_repo_targets_the_project_not_the_root(tmp_path):
    """`dotnet test` does NOT discover projects recursively: run bare at a root
    whose projects live under `src/`, it fails with MSB1003 and writes no
    results at all — an empty pass-set that reads like a clean one."""
    _csproj(tmp_path, "src/App.Tests/App.Tests.csproj")
    steps = v.plan_steps(tmp_path, ["test"], v.detect_project(tmp_path))
    assert [s.command for s in steps] == [
        ["dotnet", "test", "src/App.Tests/App.Tests.csproj"]]


def test_test_projects_are_preferred_over_library_projects(tmp_path):
    """A non-test project exits 0 writing NO results, so pointing a suite at
    one turns a library into a hard suite failure. With no solution to run,
    only test-named projects are targeted."""
    _csproj(tmp_path, "src/App/App.csproj")
    _csproj(tmp_path, "src/App.Core/App.Core.csproj")
    _csproj(tmp_path, "test/App.Tests/App.Tests.csproj")
    det = v.detect_project(tmp_path)
    assert [s.command[-1] for s in v.plan_steps(tmp_path, ["test"], det)] == [
        "test/App.Tests/App.Tests.csproj"]
    # build/lint are not test-runners: they cover every project.
    assert len(v.plan_steps(tmp_path, ["build"], det)) == 3


def test_no_runnable_test_target_plans_nothing_rather_than_a_failing_command(tmp_path):
    """Several library projects, no solution, nothing test-named: there is no
    command that would work. Planning one anyway would manufacture exactly the
    empty-but-clean-looking pass-set this ticket is about."""
    _csproj(tmp_path, "src/App/App.csproj")
    _csproj(tmp_path, "src/App.Core/App.Core.csproj")
    det = v.detect_project(tmp_path)
    assert det.has_dotnet
    assert v.dotnet_test_targets(det.dotnet_solutions, det.dotnet_projects) == ()
    assert v.plan_steps(tmp_path, ["test"], det) == []
    assert ratchet.detect_suites(tmp_path) == []


def test_repo_controlled_filenames_cannot_inject_shell_commands(tmp_path):
    """`run_suite` executes a suite's `cmd` through a shell, and adoption runs
    against third-party repos — so a solution filename is untrusted input."""
    hostile = 'bad"; touch pwned; #.sln'
    (tmp_path / hostile).write_text("solution")
    (tmp_path / "Safe.sln").write_text("solution")

    suites = ratchet.detect_suites(tmp_path)
    assert suites
    import subprocess
    for suite in suites:
        # `dotnet` may be absent; what matters is that the shell treats the
        # filename as ONE argument and runs no extra command.
        subprocess.run(suite["cmd"], shell=True, cwd=tmp_path,
                       capture_output=True, text=True)
    assert not (tmp_path / "pwned").exists(), "shell injection via a repo filename"


def test_results_directories_are_filesystem_safe(tmp_path):
    for name in ('we"ird one.sln', "Other.sln"):
        (tmp_path / name).write_text("solution")
    reports = [s["report"] for s in ratchet.detect_suites(tmp_path)]
    assert len(set(reports)) == len(reports)
    for report in reports:
        assert not (set(report) & set('"\'; |&$<>()')), report


def test_multiple_solutions_get_one_suite_each_with_separate_results_dirs(tmp_path):
    """Separate results directories: a shared one would let one solution's
    pre-run clear delete the other's results mid-flight."""
    for name in ("Api.sln", "Legacy.sln"):
        (tmp_path / name).write_text("solution")
    suites = ratchet.detect_suites(tmp_path)
    assert [s["name"] for s in suites] == ["dotnet-Api", "dotnet-Legacy"]
    assert len({s["report"] for s in suites}) == 2
    import shlex
    for s in suites:
        argv = shlex.split(s["cmd"])
        assert Path(argv[2]).suffix == ".sln"
        assert argv[argv.index("--results-directory") + 1] == s["report"]


def test_makefile_target_still_wins_over_dotnet(tmp_path):
    """The existing Makefile-first precedence must not be disturbed."""
    _csproj(tmp_path, "src/App/App.csproj")
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    det = v.detect_project(tmp_path)
    steps = v.plan_steps(tmp_path, ["test"], det)
    assert [s.tool for s in steps] == ["make"]


# --- ratchet suite autodetection --------------------------------------------------


def test_detect_suites_finds_a_dotnet_suite(tmp_path):
    _csproj(tmp_path, "src/App.Tests/App.Tests.csproj")
    suites = ratchet.detect_suites(tmp_path)
    assert [s["name"] for s in suites] == ["dotnet"]
    suite = suites[0]
    assert suite["parser"] == "trx"
    # A results DIRECTORY, never a fixed filename: with a fixed LogFileName
    # every test project overwrites the previous project's results.
    assert "--results-directory" in suite["cmd"]
    assert "LogFileName" not in suite["cmd"]
    assert suite["report"] in suite["cmd"]


def test_dotnet_and_node_coexist(tmp_path):
    """A .NET repo with a JS frontend gets the real .NET suite, not the
    fill-me-in `npm test` skeleton."""
    _csproj(tmp_path, "src/App/App.csproj")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    assert [s["name"] for s in ratchet.detect_suites(tmp_path)] == ["dotnet"]


# --- C# in the measurement population --------------------------------------------


def test_cs_maps_to_a_language_in_the_quality_population():
    assert complexity.EXT_LANG[".cs"] == "csharp"
    assert population.lang_of("src/Roller.API/OrderService.cs") == "csharp"


def test_cs_files_enter_the_scanned_population_not_the_unscanned_bucket(tmp_path):
    """#259's measurable symptom: 8,316 .cs files reported as `unscanned`."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "OrderService.cs").write_text("public class OrderService {}")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    assert population.tracked_source(str(tmp_path)) == ["src/OrderService.cs"]
    assert ".cs" not in population.unknown_language_files(str(tmp_path))


def test_dotnet_build_manifests_are_not_reported_as_unscanned_source(tmp_path):
    """`.csproj`/`.sln` are build config, like `.toml` — noise in the
    'unscanned source' signal that should stay reserved for real source."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "App.sln").write_text("solution")
    (tmp_path / "App.csproj").write_text("<Project />")
    (tmp_path / "Views").mkdir()
    (tmp_path / "Views" / "Index.cshtml").write_text("@model X")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    unknown = population.unknown_language_files(str(tmp_path))
    assert ".csproj" not in unknown and ".sln" not in unknown
    # Razor IS real, genuinely unscanned source — it must stay visible.
    assert unknown[".cshtml"] == 1


@pytest.mark.parametrize("path", [
    "Roller.API.Tests/OrderTests.cs",
    "test/UnitTests/OrderTests.cs",
    "src/OrderServiceTest.cs",
])
def test_csharp_test_conventions_are_recognized(path):
    assert population.is_test_file(path)
    assert complexity.TEST_RE.search(path)


@pytest.mark.parametrize("path", [
    "src/Latest.cs",          # ends in "test.cs" only by accident
    "src/Manifest.cs",
    "src/latest/Order.cs",    # lowercase dir, not a camel-case Tests/ project
])
def test_production_code_that_merely_looks_test_like_is_not_a_test(path):
    """Case-sensitivity matters: misclassifying production code as tests
    deflates the measured source population — the same under-measurement in a
    subtler form."""
    assert not population.is_test_file(path)
    assert not complexity.TEST_RE.search(path)


def test_csharp_is_in_clone_detection_formats():
    from quality import duplication

    assert "csharp" in duplication.FORMATS.split(",")


# --- C# enclosing-symbol resolution (func_regex) ------------------------------------

CS_SOURCE = """public class OrderService
{
    private readonly IDb _db;
    private string _status;

    public async Task<Order> UpdatePlan(Guid id, string plan)
    {
        order.PlanCode = plan;
        return order;
    }

    protected internal static IReadOnlyList<Order> Archive(Guid id)
    {
        _db.Update(id, new { status = "archived" });
    }

    public string Status
    {
        set { _status = value; }
    }

    // never write PlanCode = here
    public void Noop() { }
}
"""


def _symbols(source: str = CS_SOURCE, path: str = "src/OrderService.cs") -> dict[int, str | None]:
    from chief_wiggum.write_emission import emit_write_sites

    return {s.line: s.symbol for s in emit_write_sites(path, source)}


def test_csharp_write_sites_resolve_their_enclosing_method():
    """Without this, a `.cs` write has no enclosing symbol and the single-writer
    checker cannot tell a sanctioned writer from an unsanctioned one."""
    symbols = _symbols()

    def line_of(fragment: str) -> int:
        return next(i for i, ln in enumerate(CS_SOURCE.splitlines(), 1) if fragment in ln)

    assert symbols[line_of("order.PlanCode = plan")] == "UpdatePlan"
    # modifiers + generic return type
    assert symbols[line_of('status = "archived"')] == "Archive"
    # property setter whose brace sits on the following line
    assert symbols[line_of("_status = value")] == "Status"


def test_csharp_comments_are_stripped_before_write_detection():
    """`.cs` had no comment marker registered, so a field named in a `//`
    comment would have read as a write the moment C# entered the scan."""
    comment_line = next(
        i for i, ln in enumerate(CS_SOURCE.splitlines(), 1) if ln.strip().startswith("//"))
    assert comment_line not in _symbols()


def test_csharp_field_declaration_is_not_mistaken_for_a_method():
    source = "public class C\n{\n    private readonly IDb _db;\n    public void Go()\n    {\n        x.Plan = 1;\n    }\n}\n"
    assert _symbols(source, "src/C.cs")[6] == "Go"


def test_csharp_call_is_not_mistaken_for_a_declaration():
    """A bare call has no modifier or return type; treating it as a
    declaration would attribute the write to the wrong symbol — worse than
    reporting none."""
    source = "public class C\n{\n    public void Go()\n    {\n        Save(order);\n        x.Plan = 1;\n    }\n}\n"
    assert _symbols(source, "src/C.cs")[6] == "Go"


def test_csharp_regex_does_not_leak_into_other_languages():
    """The C# member pattern is permissive about return types on purpose; it
    must only be consulted for `.cs`."""
    from chief_wiggum.write_emission import _enclosing_symbol

    lines = ["public class C {", "    public void Go() {", "        x.Plan = 1;"]
    assert _enclosing_symbol(lines, 2, ".cs") == "Go"
    assert _enclosing_symbol(lines, 2, ".go") is None


def test_csharp_declares_func_regex_support():
    assert cw_languages.languages()["csharp"].func_regex is True


# --- the declared support matrix --------------------------------------------------


def test_csharp_is_declared_at_tier_2():
    lang = cw_languages.languages()["csharp"]
    assert lang.tier == "2"
    assert lang.extensions == (".cs",)
    assert lang.dep_profile == "dotnet"
    assert "trx" in (lang.test_parser or "")
    # Tier 2 is NOT tier 1: no dedicated emitter module exists, so `.cs` must
    # fall through to the generic regex tier rather than claim a built one.
    assert not lang.built
    assert lang.requires, "tier 2 must state what is still missing for tier 1"


def test_cs_is_scanned_by_the_generic_tier_not_reported_unsupported():
    import emitters

    assert ".cs" in cw_languages.generic_tier_extensions()
    assert ".cs" not in cw_languages.unsupported_extensions()
    assert emitters.tier_for_suffix(".cs") == "generic"


def test_dotnet_dependency_profile_exists():
    import check_deps

    assert check_deps.WORKFLOW_REQUIREMENTS["dotnet"]["cmds"] == {"dotnet"}


# --- the extension maps must stay in step ------------------------------------------


def test_every_measured_language_has_a_test_file_convention():
    """Parity guard across the extension maps. ``population.is_test_file``
    looks a language up in ``TEST_FILE_RES``; a language added to
    ``complexity.EXT_LANG`` without a matching entry made that a KeyError that
    took down every debt engine AND the adopt survey. This asserts the maps
    agree, so the NEXT language cannot reintroduce it."""
    measured = set(complexity.EXT_LANG.values())
    missing = measured - set(population.TEST_FILE_RES)
    assert not missing, f"languages measured but with no test-file convention: {missing}"


def test_every_measured_language_survives_is_test_file():
    """The KeyError above, asserted behaviorally rather than structurally."""
    for ext, lang in complexity.EXT_LANG.items():
        assert population.is_test_file(f"src/Thing{ext}") in (True, False), lang


def test_declared_tier_1_and_tier_2_languages_are_in_the_quality_population():
    """A language declared in the matrix but absent from ``EXT_LANG`` is
    declared-not-measured — precisely #259's failure. (Designed-but-unbuilt
    slots like Rust are exempt: they claim nothing yet.)"""
    for lang in cw_languages.languages().values():
        if "designed" in lang.status:
            continue
        for ext in lang.extensions:
            assert ext in complexity.EXT_LANG, f"{lang.name} declares {ext} but nothing measures it"


def test_scanned_extension_config_is_a_scanner_version_input():
    """`config/languages.json` decides which files the scanners walk. If it is
    not hashed, moving an extension between `generic_tier` and
    `unsupported_extensions` changes real coverage while every validation
    record still reads `passing` — a vacuous pass in the gate-validation layer
    itself (#259)."""
    import subprocess

    config = ROOT / "config" / "languages.json"
    original = config.read_text()

    def version(gate: str) -> str:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / f"{gate}.py"), "--scanner-version"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    gates = ("check_single_writer", "check_traceability")
    before = {g: version(g) for g in gates}
    try:
        doc = json.loads(original)
        doc["generic_tier"]["extensions"].append(".zzz")
        config.write_text(json.dumps(doc, indent=2) + "\n")
        after = {g: version(g) for g in gates}
    finally:
        config.write_text(original)

    for gate in gates:
        assert before[gate] != after[gate], (
            f"{gate}'s scanner_version ignored a change to the scanned-extension "
            "set — its validation record would stay 'passing' across a real "
            "coverage change")


# --- /status: NOT MEASURED ---------------------------------------------------------


def _quality_dir(tmp_path) -> Path:
    q = tmp_path / "docs" / "quality"
    q.mkdir(parents=True)
    return q


def _write_ratchet(q: Path, suites: list[dict], scorecard: dict | None) -> None:
    (q / ratchet.CONFIG_NAME).write_text(json.dumps({"suites": suites}))
    if scorecard is not None:
        (q / ratchet.SCORECARD_NAME).write_text(json.dumps(scorecard))


def test_zero_pass_set_with_no_suites_is_not_measured(tmp_path):
    q = _quality_dir(tmp_path)
    _write_ratchet(q, [], {"pass_set": [], "tests_run": False})
    rt = status.ratchet_status(q)
    reason = status.ratchet_not_measured(q, rt)
    assert reason and "no test runner detected" in reason


def test_zero_pass_set_with_suites_recorded_not_run_says_so(tmp_path):
    q = _quality_dir(tmp_path)
    _write_ratchet(q, [{"name": "dotnet"}], {"pass_set": [], "tests_run": False})
    reason = status.ratchet_not_measured(q, status.ratchet_status(q))
    assert reason and "tests_run: false" in reason and "dotnet" in reason


def test_suites_that_ran_but_produced_nothing_are_not_measured(tmp_path):
    q = _quality_dir(tmp_path)
    _write_ratchet(q, [{"name": "dotnet"}], {"pass_set": [], "tests_run": True})
    reason = status.ratchet_not_measured(q, status.ratchet_status(q))
    assert reason and "0 passing case(s)" in reason


def test_a_real_pass_set_is_never_marked_not_measured(tmp_path):
    """The critical negative: a healthy target must never carry the marker."""
    q = _quality_dir(tmp_path)
    _write_ratchet(q, [{"name": "dotnet"}], {"pass_set": ["dotnet::A::b"], "tests_run": True})
    assert status.ratchet_not_measured(q, status.ratchet_status(q)) is None


def test_unconfigured_ratchet_is_not_marked_not_measured(tmp_path):
    """`no ratchet config` already says it plainly — no double-reporting."""
    q = _quality_dir(tmp_path)
    assert status.ratchet_not_measured(q, status.ratchet_status(q)) is None


def test_empty_inventory_over_an_empty_population_is_not_measured(tmp_path):
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({
        "items": [],
        "engines": {"dead_code": {"files_in_population": 0}},
        "unscanned_languages": {"unknown-language (.cs)": 8316,
                                "unknown-language (.sql)": 2715},
    }))
    reason = status.debt_not_measured(q, status.debt_counts(q))
    assert reason and "no known-language source files" in reason
    assert ".cs" in reason and "8316" in reason


def test_empty_inventory_over_a_real_population_is_a_genuine_clean_result(tmp_path):
    """Zero findings across 400 scanned files is HEALTH, not a gap — marking
    it NOT MEASURED would cry wolf and train the operator to ignore it."""
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({
        "items": [],
        "engines": {"dead_code": {"files_in_population": 400}},
        "unscanned_languages": {},
    }))
    assert status.debt_not_measured(q, status.debt_counts(q)) is None


def test_inventory_with_findings_is_never_marked_not_measured(tmp_path):
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({
        "items": [{"severity": "high"}],
        "engines": {"dead_code": {"files_in_population": 0}},
    }))
    assert status.debt_not_measured(q, status.debt_counts(q)) is None


def test_absent_inventory_is_not_marked_not_measured(tmp_path):
    q = _quality_dir(tmp_path)
    assert status.debt_not_measured(q, status.debt_counts(q)) is None


def test_older_inventory_without_a_population_count_does_not_claim(tmp_path):
    """Without proof of a zero population, /status must stay silent rather
    than assert a gap it cannot demonstrate."""
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({"items": []}))
    assert status.debt_not_measured(q, status.debt_counts(q)) is None


def test_language_in_the_population_with_no_dead_code_tier_is_reported_unscanned(tmp_path):
    """C# enters the population but no dead-code tier handles it. Counted in
    `files_in_population`, absent from `languages` AND absent from `unscanned`,
    it would produce a zero-finding inventory over a non-zero population — a
    clean-looking result that also defeats the NOT MEASURED marker (#259's own
    failure mode, one layer down)."""
    import subprocess

    from quality import dead_code

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "OrderService.cs").write_text("public class OrderService {}")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    result = dead_code.analyze(str(tmp_path))
    assert result["files_in_population"] == 1
    assert result["unscanned"].get("csharp") == 1
    assert "no dead-code tier" in result["languages"]["csharp"]["skipped"]


def test_zero_items_over_a_partly_unscanned_population_is_flagged_partial(tmp_path):
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({
        "items": [],
        "engines": {"dead_code": {"files_in_population": 8316}},
        "unscanned_languages": {"csharp": 8316},
    }))
    counts = status.debt_counts(q)
    # Something WAS scanned, so this is not "not measured"...
    assert status.debt_not_measured(q, counts) is None
    # ...but it is not a clean bill of health either.
    partial = status.debt_partial_coverage(q, counts)
    assert partial and "csharp" in partial


def test_fully_scanned_empty_inventory_is_not_flagged_partial(tmp_path):
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(json.dumps({
        "items": [],
        "engines": {"dead_code": {"files_in_population": 400}},
        "unscanned_languages": {},
    }))
    assert status.debt_partial_coverage(q, status.debt_counts(q)) is None


def test_rendered_status_shows_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    target = tmp_path / "target"
    q = _quality_dir(target)
    _write_ratchet(q, [], {"pass_set": [], "tests_run": False})
    (q / "debt.json").write_text(json.dumps({
        "items": [],
        "engines": {"dead_code": {"files_in_population": 0}},
        "unscanned_languages": {"unknown-language (.cs)": 8316},
    }))

    st = status.gather(target)
    assert set(st["not_measured"]) == {"ratchet", "debt"}

    text = status.render_text(st)
    assert text.count("NOT MEASURED") == 2
    assert "absence of findings is NOT health" in text
    # The counts still render — the marker adds context, never hides data.
    assert "pass-set: 0 case(s)" in text


def test_rendered_status_stays_quiet_when_everything_was_measured(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    target = tmp_path / "target"
    q = _quality_dir(target)
    _write_ratchet(q, [{"name": "dotnet"}], {"pass_set": ["dotnet::A::b"], "tests_run": True})
    (q / "debt.json").write_text(json.dumps({
        "items": [], "engines": {"dead_code": {"files_in_population": 400}}}))

    st = status.gather(target)
    assert st["not_measured"] == {}
    assert "NOT MEASURED" not in status.render_text(st)

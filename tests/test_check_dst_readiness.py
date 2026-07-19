"""Tests for the DST-readiness scanner (#167)."""

from __future__ import annotations

import json

import check_dst_readiness as dst


def _rule_files(report, rule):
    return sorted(f["file"] for f in report.findings if f["rule"] == rule)


# --- wall-clock rule, per language -------------------------------------------


def test_go_wall_clock_detected(tmp_path):
    (tmp_path / "svc.go").write_text(
        "package svc\n\nfunc Now() time.Time {\n\treturn time.Now()\n}\n"
    )
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.go"]
    finding = report.findings[0]
    assert finding["line"] == 4
    assert "time.Now(" in finding["match"]


def test_python_datetime_now_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import datetime\n\nx = datetime.now()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]


def test_python_datetime_utcnow_detected(tmp_path):
    (tmp_path / "svc.py").write_text("x = datetime.utcnow()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]


def test_python_time_time_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import time\nstart = time.time()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]


def test_js_date_now_detected(tmp_path):
    (tmp_path / "svc.js").write_text("const t = Date.now();\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.js"]


def test_ts_new_date_no_args_detected(tmp_path):
    (tmp_path / "svc.ts").write_text("const t = new Date();\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.ts"]


def test_ts_new_date_with_arg_not_flagged(tmp_path):
    # A reconstructed/parsed date is not a live wall-clock read.
    (tmp_path / "svc.ts").write_text("const t = new Date(payload.timestamp);\n")
    report = dst.check(tmp_path)
    assert report.findings == []


# --- unseeded-random rule, per language ---------------------------------------


def test_go_rand_intn_detected(tmp_path):
    (tmp_path / "svc.go").write_text(
        "package svc\n\nfunc Roll() int {\n\treturn rand.Intn(6)\n}\n"
    )
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.go"]


def test_go_rand_float64_detected(tmp_path):
    (tmp_path / "svc.go").write_text("x := rand.Float64()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.go"]


def test_python_random_module_call_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import random\nx = random.random()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.py"]


def test_python_random_randint_detected(tmp_path):
    (tmp_path / "svc.py").write_text("x = random.randint(1, 6)\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.py"]


def test_python_random_seed_not_flagged(tmp_path):
    # Seeding explicitly IS the seam pattern, not a violation of it.
    (tmp_path / "svc.py").write_text("random.seed(42)\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_js_math_random_detected(tmp_path):
    (tmp_path / "svc.js").write_text("const x = Math.random();\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.js"]


# --- comment stripping --------------------------------------------------------


def test_call_in_comment_not_flagged(tmp_path):
    (tmp_path / "svc.go").write_text("// example: time.Now() returns wall clock\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_call_in_python_comment_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("# x = random.random()\n")
    report = dst.check(tmp_path)
    assert report.findings == []


# --- allowlist: seam globs -----------------------------------------------------


def test_default_clock_seam_file_exempt(tmp_path):
    (tmp_path / "clock.go").write_text("func Now() time.Time { return time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.findings == []
    assert any(e["file"] == "clock.go" and e["reason"] == "seam-glob" for e in report.exempted_files)


def test_default_clock_seam_directory_exempt(tmp_path):
    d = tmp_path / "internal" / "clock"
    d.mkdir(parents=True)
    (d / "impl.go").write_text("func Now() time.Time { return time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_default_rand_seam_exempt(tmp_path):
    (tmp_path / "randsource.go").write_text("func Roll() int { return rand.Intn(6) }\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_default_telemetry_seam_exempt(tmp_path):
    d = tmp_path / "telemetry"
    d.mkdir()
    (d / "emit.go").write_text("func Emit() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_non_seam_file_still_flagged(tmp_path):
    (tmp_path / "handler.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["handler.go"]


def test_config_adds_extra_seam(tmp_path):
    (tmp_path / "widgets.go").write_text("func H() { _ = time.Now() }\n")
    config = tmp_path / "dst-config.json"
    config.write_text(json.dumps({"seams": ["**/widgets*"]}))
    report = dst.check(tmp_path, config)
    assert report.findings == []
    # Built-in defaults still apply alongside the config additions.
    (tmp_path / "clock.go").write_text("func Now() time.Time { return time.Now() }\n")
    report2 = dst.check(tmp_path, config)
    assert report2.findings == []


def test_config_bad_seams_type_warns_but_keeps_defaults(tmp_path):
    (tmp_path / "clock.go").write_text("func Now() time.Time { return time.Now() }\n")
    config = tmp_path / "dst-config.json"
    config.write_text(json.dumps({"seams": "not-a-list"}))
    report = dst.check(tmp_path, config)
    assert report.findings == []  # default clock seam still exempts it
    assert any("must be a list" in w for w in report.warnings)


def test_missing_config_file_warns(tmp_path):
    (tmp_path / "handler.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path, tmp_path / "does-not-exist.json")
    assert any("could not read --config" in w for w in report.warnings)
    # Scan still proceeds with default seams.
    assert _rule_files(report, "wall-clock") == ["handler.go"]


# --- allowlist: cw:dst-exempt marker -------------------------------------------


def test_hash_marker_exempts_python_file(tmp_path):
    (tmp_path / "legacy.py").write_text("# cw:dst-exempt\nx = datetime.now()\n")
    report = dst.check(tmp_path)
    assert report.findings == []
    assert any(e["file"] == "legacy.py" for e in report.exempted_files)


def test_slash_marker_exempts_go_file(tmp_path):
    (tmp_path / "legacy.go").write_text("// cw:dst-exempt\nfunc H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_marker_beyond_top_lines_does_not_exempt(tmp_path):
    body = "\n".join(f"// line {i}" for i in range(25))
    content = body + "\n// cw:dst-exempt\nfunc H() { _ = time.Now() }\n"
    (tmp_path / "legacy.go").write_text(content)
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["legacy.go"]


# --- allowlist: test paths ------------------------------------------------------


def test_test_file_exempt_by_filename(tmp_path):
    (tmp_path / "svc_test.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.findings == []
    assert any(e["reason"] == "test-path" for e in report.exempted_files)


def test_spec_file_exempt(tmp_path):
    (tmp_path / "svc.spec.ts").write_text("const t = Date.now();\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_e2e_directory_exempt(tmp_path):
    d = tmp_path / "e2e"
    d.mkdir()
    (d / "helpers.ts").write_text("const t = Date.now();\n")
    report = dst.check(tmp_path)
    assert report.findings == []


# --- report shape / rendering --------------------------------------------------


def test_ok_true_when_no_findings(tmp_path):
    (tmp_path / "svc.go").write_text("package svc\n")
    report = dst.check(tmp_path)
    assert report.ok is True
    assert report.counts["total"] == 0


def test_ok_false_when_findings(tmp_path):
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert report.ok is False
    assert report.counts["total"] == 1
    assert report.counts["wall_clock"] == 1
    assert report.counts["unseeded_random"] == 0


def test_authority_string_present_in_dict_and_text():
    report = dst.DSTReport()
    d = report.to_dict()
    assert "does not prove" in d["authority"]
    text = dst.render_text(report)
    assert "Authority:" in text
    assert "does not prove" in text


def test_render_text_lists_findings(tmp_path):
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    text = dst.render_text(report)
    assert "svc.go:1" in text
    assert "wall-clock" in text


def test_missing_source_root_warns_and_returns_empty(tmp_path):
    missing = tmp_path / "nope"
    report = dst.check(missing)
    assert report.findings == []
    assert any("source root not found" in w for w in report.warnings)


# --- CLI / gate exit codes ------------------------------------------------------


def test_main_default_report_only_exit_zero_even_with_findings(tmp_path, capsys):
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    rc = dst.main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "svc.go" in out


def test_main_gate_exit_one_on_findings(tmp_path, capsys):
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    rc = dst.main([str(tmp_path), "--gate"])
    assert rc == 1


def test_main_gate_exit_zero_when_clean(tmp_path):
    (tmp_path / "svc.go").write_text("package svc\n")
    rc = dst.main([str(tmp_path), "--gate"])
    assert rc == 0


def test_main_usage_error_on_missing_root(tmp_path, capsys):
    rc = dst.main([str(tmp_path / "does-not-exist")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_main_json_format(tmp_path, capsys):
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    rc = dst.main([str(tmp_path), "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["counts"]["total"] == 1
    assert data["ok"] is False


# --- glob translation internals -------------------------------------------------


def test_glob_to_regex_matches_root_and_nested():
    regex = dst._glob_to_regex("**/clock*")
    assert regex.match("clock.go")
    assert regex.match("internal/clock")
    assert regex.match("internal/clock/impl.go") is None  # only the dir itself, not a
    # non-clock-prefixed leaf inside it — but the directory-ancestor check in
    # _is_seam is what actually exempts files under the dir (tested above).


def test_glob_to_regex_star_does_not_cross_slash():
    regex = dst._glob_to_regex("*.go")
    assert regex.match("svc.go")
    assert regex.match("pkg/svc.go") is None


# --- skip-dirs are relative to the scanned root (P1 regression) -----------------


def test_checkout_under_skip_named_ancestor_is_scanned(tmp_path):
    # A checkout living under .../build/... must still be scanned in full — only
    # skip-dirs INSIDE the root count.
    root = tmp_path / "build" / "checkout"
    root.mkdir(parents=True)
    (root / "svc.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(root)
    assert _rule_files(report, "wall-clock") == ["svc.go"]


def test_skip_dir_inside_root_still_skipped(tmp_path):
    d = tmp_path / "vendor" / "lib"
    d.mkdir(parents=True)
    (d / "dep.go").write_text("func H() { _ = time.Now() }\n")
    (tmp_path / "svc.go").write_text("func H() { _ = time.Now() }\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.go"]


# --- Python idioms: qualified datetime, from-imports, aliases (P1 regression) ----


def test_python_datetime_datetime_now_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import datetime\nx = datetime.datetime.now()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]
    assert len(report.findings) == 1  # deduped: one finding per rule per line


def test_python_datetime_datetime_utcnow_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import datetime\nx = datetime.datetime.utcnow()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]


def test_python_import_datetime_as_alias_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import datetime as dt\na = dt.datetime.now()\nb = dt.now()\n")
    report = dst.check(tmp_path)
    assert [f["line"] for f in report.findings if f["rule"] == "wall-clock"] == [2, 3]


def test_python_from_time_import_time_bare_call_detected(tmp_path):
    (tmp_path / "svc.py").write_text("from time import time\nstart = time()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]
    assert report.findings[0]["line"] == 2


def test_python_from_time_import_time_aliased_detected(tmp_path):
    (tmp_path / "svc.py").write_text("from time import time as wall\nstart = wall()\n")
    report = dst.check(tmp_path)
    assert report.findings and report.findings[0]["line"] == 2


def test_python_bare_time_without_from_import_not_flagged(tmp_path):
    # A bare time() call in a file that never does `from time import time` could be
    # any local helper — not flagged (precision over recall).
    (tmp_path / "svc.py").write_text("start = time()\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_import_time_as_alias_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import time as tm\nstart = tm.time()\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.py"]


def test_python_import_random_as_alias_detected(tmp_path):
    (tmp_path / "svc.py").write_text("import random as rr\nx = rr.randint(1, 6)\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.py"]


def test_python_from_random_import_bare_call_detected(tmp_path):
    (tmp_path / "svc.py").write_text(
        "from random import randint, choice\nx = randint(1, 6)\ny = choice([1, 2])\n"
    )
    report = dst.check(tmp_path)
    lines = [f["line"] for f in report.findings if f["rule"] == "unseeded-random"]
    assert lines == [2, 3]


def test_python_from_random_import_aliased_detected(tmp_path):
    (tmp_path / "svc.py").write_text("from random import choice as pick\nx = pick([1, 2])\n")
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.py"]


def test_python_from_random_import_seed_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("from random import seed\nseed(42)\n")
    report = dst.check(tmp_path)
    assert report.findings == []


# --- Go import aliasing (P1 regression) -----------------------------------------


def test_go_aliased_math_rand_import_detected(tmp_path):
    (tmp_path / "svc.go").write_text(
        'package svc\n\nimport mrand "math/rand"\n\nfunc Roll() int {\n\treturn mrand.Intn(6)\n}\n'
    )
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.go"]
    assert report.findings[0]["line"] == 6


def test_go_aliased_import_in_block_detected(tmp_path):
    (tmp_path / "svc.go").write_text(
        'package svc\n\nimport (\n\t"fmt"\n\tmrand "math/rand/v2"\n)\n\n'
        "func Roll() int {\n\treturn mrand.IntN(6)\n}\n"
    )
    report = dst.check(tmp_path)
    assert _rule_files(report, "unseeded-random") == ["svc.go"]


def test_go_crypto_rand_not_misflagged(tmp_path):
    # crypto/rand imports under the default name `rand` — its calls are NOT the
    # unseeded math/rand default source.
    (tmp_path / "svc.go").write_text(
        'package svc\n\nimport "crypto/rand"\n\n'
        "func Key() {\n\tn, _ := rand.Int(rand.Reader, max)\n\t_ = n\n}\n"
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_go_aliased_import_suppresses_default_rand_name(tmp_path):
    # With math/rand imported under an alias, a symbol literally named `rand.` is
    # something else entirely (a local var, another package) — not flagged.
    (tmp_path / "svc.go").write_text(
        'package svc\n\nimport mrand "math/rand"\n\nfunc Roll() int {\n\treturn rand.Intn(6)\n}\n'
    )
    report = dst.check(tmp_path)
    assert report.findings == []


# --- seed-awareness: seeded constructors are NOT flagged (P2 regression) ---------


def test_go_seeded_source_not_flagged(tmp_path):
    (tmp_path / "svc.go").write_text(
        'package svc\n\nimport "math/rand"\n\nfunc New() *rand.Rand {\n'
        "\treturn rand.New(rand.NewSource(42))\n}\n"
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_go_rand_seed_call_not_flagged(tmp_path):
    (tmp_path / "svc.go").write_text('import "math/rand"\n\nfunc init() { rand.Seed(42) }\n')
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_random_random_constructor_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("import random\nrng = random.Random(42)\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_system_random_constructor_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("import random\nrng = random.SystemRandom()\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_aliased_random_constructor_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("import random as rr\nrng = rr.Random(42)\nrr.seed(1)\n")
    report = dst.check(tmp_path)
    assert report.findings == []


# --- string literals / docstrings / block comments (P2 regression) ---------------


def test_python_docstring_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text(
        '"""Module doc.\n\nNever call datetime.now() or random.random() directly.\n"""\n\nx = 1\n'
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_string_literal_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text('msg = "do not use time.time() here"\n')
    report = dst.check(tmp_path)
    assert report.findings == []


def test_python_code_after_docstring_still_flagged(tmp_path):
    (tmp_path / "svc.py").write_text(
        '"""Doc mentioning datetime.now()."""\n\nimport datetime\nx = datetime.now()\n'
    )
    report = dst.check(tmp_path)
    assert [f["line"] for f in report.findings] == [4]


def test_go_block_comment_not_flagged(tmp_path):
    (tmp_path / "svc.go").write_text(
        "package svc\n\n/*\nExample:\n\tt := time.Now()\n*/\n\nfunc H() {}\n"
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_go_raw_string_across_lines_not_flagged(tmp_path):
    (tmp_path / "svc.go").write_text(
        "package svc\n\nvar doc = `\nusage: never call time.Now() inline\n`\n"
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_ts_block_comment_not_flagged(tmp_path):
    (tmp_path / "svc.ts").write_text(
        "/**\n * Do not use Date.now() or Math.random() directly.\n */\nexport const x = 1;\n"
    )
    report = dst.check(tmp_path)
    assert report.findings == []


def test_ts_string_literal_not_flagged(tmp_path):
    (tmp_path / "svc.ts").write_text("const msg = 'avoid Date.now() calls';\n")
    report = dst.check(tmp_path)
    assert report.findings == []


def test_code_on_same_line_as_string_still_flagged(tmp_path):
    (tmp_path / "svc.ts").write_text('log("stamp", Date.now());\n')
    report = dst.check(tmp_path)
    assert _rule_files(report, "wall-clock") == ["svc.ts"]

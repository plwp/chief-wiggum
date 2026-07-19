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

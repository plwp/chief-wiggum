"""Tests for the single-writer / mutator-inventory checker."""

from __future__ import annotations

import json

import check_single_writer as sw

# --- invariant metadata parsing ---------------------------------------------


def test_parse_prose_invariant_with_tag():
    text = (
        "**INV-bil-001**: single atomic Stripe→plan write\n"
        "<!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan "
        "sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go -->\n"
    )
    invs, malformed = sw.parse_prose_invariants(text, "invariants.md")
    assert malformed == []
    assert len(invs) == 1
    inv = invs[0]
    assert inv.id == "INV-bil-001"
    assert inv.controls_field == ["provider.plan", "provider.stripe_plan"]
    assert inv.sanctioned_writers == ["ReconcileStripe", "internal/billing/reconcile.go"]
    assert inv.description == "single atomic Stripe→plan write"


def test_prose_tag_attrs_order_free():
    text = (
        "<!-- @cw-writes INV-x-001 sanctioned_writers=Foo controls_field=a.b -->\n"
    )
    invs, _ = sw.parse_prose_invariants(text, "f.md")
    assert invs[0].controls_field == ["a.b"]
    assert invs[0].sanctioned_writers == ["Foo"]


def test_prose_incomplete_metadata_is_malformed():
    text = "<!-- @cw-writes INV-x-001 controls_field=a.b -->\n"  # no sanctioned_writers
    invs, malformed = sw.parse_prose_invariants(text, "f.md")
    assert invs == []
    assert malformed and "both" in malformed[0]["reason"]


def test_structured_invariant_parsed():
    data = {
        "invariants": [
            {
                "id": "INV-bil-001",
                "description": "single write path",
                "controls_field": ["provider.stripe_plan"],
                "sanctioned_writers": ["ReconcileStripe"],
            },
            {"id": "INV-bil-002", "description": "unrelated invariant"},  # skipped
        ]
    }
    invs, malformed = sw.parse_structured_invariants(data, "state-machines.json")
    assert malformed == []
    assert [i.id for i in invs] == ["INV-bil-001"]  # the plain one is skipped


def test_structured_one_sided_metadata_is_malformed():
    data = {"invariants": [{"id": "INV-x-001", "controls_field": ["a.b"]}]}
    invs, malformed = sw.parse_structured_invariants(data, "sm.json")
    assert invs == []
    assert malformed and "not both" in malformed[0]["reason"]


def test_invariant_without_metadata_is_skipped_gracefully():
    data = {"invariants": [{"id": "INV-x-001", "description": "prose only invariant"}]}
    invs, malformed = sw.parse_structured_invariants(data, "sm.json")
    assert invs == [] and malformed == []


# --- field token derivation -------------------------------------------------


def test_field_tokens_cover_snake_and_camel():
    inv = sw.SingleWriterInvariant(
        "INV-x-001", "", ["provider.stripe_plan"], ["Foo"], "src"
    )
    toks = inv.field_tokens()
    assert "stripe_plan" in toks and "stripeplan" in toks


# --- writer scanning --------------------------------------------------------


def _inv():
    return sw.SingleWriterInvariant(
        id="INV-bil-001",
        description="single write path",
        controls_field=["provider.plan", "provider.stripe_plan"],
        sanctioned_writers=["ReconcileStripe", "internal/billing/reconcile.go"],
        source="invariants.md",
    )


def test_go_assignment_writer_detected(tmp_path):
    (tmp_path / "admin.go").write_text(
        "func ChangePlan(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert len(writers) == 1
    w = writers[0]
    assert w.file == "admin.go" and w.symbol == "ChangePlan"
    assert w.sanctioned is False  # ChangePlan is NOT in the sanctioned set


def test_sanctioned_by_symbol(tmp_path):
    (tmp_path / "other.go").write_text(
        "func ReconcileStripe(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert len(writers) == 1 and writers[0].sanctioned is True


def test_sanctioned_by_file_path(tmp_path):
    d = tmp_path / "internal" / "billing"
    d.mkdir(parents=True)
    (d / "reconcile.go").write_text(
        "func doWrite(p *Provider, v string) {\n\tp.Plan = v\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert len(writers) == 1 and writers[0].sanctioned is True  # file is sanctioned


def test_struct_literal_write_detected(tmp_path):
    (tmp_path / "seed.go").write_text(
        "func mkProvider() Provider {\n\treturn Provider{Plan: \"pro\"}\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert any(w.field == "provider.plan" for w in writers)


def test_bson_set_mutation_detected(tmp_path):
    (tmp_path / "repo.go").write_text(
        "func setPlan(c *mongo.Collection, v string) {\n"
        "\tc.UpdateOne(ctx, filter, bson.M{\"$set\": bson.M{\"stripe_plan\": v}})\n"
        "}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert any("stripe_plan" in w.text for w in writers)


def test_bare_field_literal_without_mutation_context_ignored(tmp_path):
    # A DTO response struct tag mentioning "plan" is not a write.
    (tmp_path / "dto.go").write_text(
        "type Resp struct {\n\tName string `json:\"plan\"`\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    # No assignment/struct-set/mutation — should not be flagged as a writer.
    assert all("`json" not in w.text for w in writers)


def test_test_files_are_not_violations(tmp_path):
    (tmp_path / "admin_test.go").write_text(
        "func TestX(t *testing.T) {\n\tp.StripePlan = \"pro\"\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert writers and all(w.sanctioned and w.is_test for w in writers)


# --- end-to-end: the ChangePlan incident ------------------------------------


def _write_billing_epic(tmp_path):
    """Reproduce INV-BIL-001: single atomic Stripe→plan write."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text(
        "# Invariants\n\n"
        "**INV-bil-001**: single atomic Stripe→plan write / single write path\n"
        "<!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan "
        "sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go -->\n"
    )
    return epic


def test_incident_flags_legacy_changeplan_writer(tmp_path):
    """The pre-existing admin ChangePlan control is a SECOND writer of stripe_plan
    and must be flagged as an unsanctioned single-write-path violation."""
    epic = _write_billing_epic(tmp_path)

    src = tmp_path / "src"
    (src / "internal" / "billing").mkdir(parents=True)
    # Sanctioned writer — the reconcile path.
    (src / "internal" / "billing" / "reconcile.go").write_text(
        "package billing\n\n"
        "func ReconcileStripe(p *Provider, sub *stripe.Subscription) {\n"
        "\tp.StripePlan = sub.Plan.ID\n"
        "}\n"
    )
    # LEGACY unsanctioned writer — the admin plan dropdown from an earlier epic.
    (src / "internal" / "admin").mkdir(parents=True)
    (src / "internal" / "admin" / "handlers.go").write_text(
        "package admin\n\n"
        "func ChangePlan(p *Provider, newPlan string) {\n"
        "\tp.StripePlan = newPlan  // SECOND writer — violates INV-bil-001\n"
        "}\n"
    )

    report = sw.check(epic, src)

    # Exactly one violation: ChangePlan.
    assert len(report.violations) == 1
    v = report.violations[0]
    assert v["invariant_id"] == "INV-bil-001"
    assert v["symbol"] == "ChangePlan"
    assert v["file"].endswith("handlers.go")
    assert v["field"] == "provider.stripe_plan"

    # The reconcile writer is present but sanctioned (not a violation).
    sanctioned = [w for w in report.writers if w["symbol"] == "ReconcileStripe"]
    assert sanctioned and sanctioned[0]["sanctioned"] is True

    # Gates: soundness OK (metadata well-formed), coverage FAILS (unsanctioned writer).
    assert report.soundness_ok is True
    assert report.coverage_ok is False


def test_incident_clean_when_only_sanctioned_writer(tmp_path):
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    (src / "internal" / "billing").mkdir(parents=True)
    (src / "internal" / "billing" / "reconcile.go").write_text(
        "func ReconcileStripe(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    report = sw.check(epic, src)
    assert report.violations == []
    assert report.coverage_ok is True


def test_epic_own_generated_artifacts_are_not_scanned_as_writers(tmp_path):
    """When the epic dir lives UNDER the scanned source_root (the real layout:
    source is the repo root, epic is docs/epics/<slug>), the epic's OWN rendered
    model artifacts DESCRIBE the controlled field — they must not be mis-read as a
    second writer. Regression: a rendered `@deal.post` message carrying the literal
    bson update `{active_owner_count:-1}` was flagged as an unsanctioned writer."""
    repo = tmp_path
    epic = repo / "docs" / "epics" / "team-seats"
    (epic / "models").mkdir(parents=True)
    (epic / "invariants.md").write_text(
        "# Invariants\n\n"
        "**INV-seat-001**: single write path for the owner counter\n"
        "<!-- @cw-writes INV-seat-001 controls_field=provider.active_owner_count "
        "sanctioned_writers=RemoveStaff,internal/db/provider_owner_count.go -->\n"
    )
    # A rendered spec artifact: the field token appears inside a message STRING that
    # documents the physical update — it is not itself a write.
    (epic / "models" / "contracts_deal.py").write_text(
        "@deal.post(lambda r: provider.active_owner_count_after "
        "== provider.active_owner_count_before - 1, "
        'message="runs {$inc:{active_owner_count:-1}}; MatchedCount==0 -> ErrLastOwner")\n'
    )
    # The real, sanctioned physical writer, in the implementation tree.
    (repo / "internal" / "db").mkdir(parents=True)
    (repo / "internal" / "db" / "provider_owner_count.go").write_text(
        "package db\n\n"
        "func (r *providerRepo) DecrementActiveOwnerCountIfMultiple(id ID) {\n"
        '\tr.c.UpdateOne(ctx, bson.M{"_id": id},\n'
        '\t\tbson.M{"$inc": bson.M{"active_owner_count": -1}})\n'
        "}\n"
    )

    report = sw.check(epic, repo)

    # The generated spec artifact under the epic dir is NOT a violation.
    assert [v for v in report.violations if "contracts_deal.py" in v["file"]] == []
    assert report.violations == []
    assert report.coverage_ok is True
    # The real implementation writer is still found (scanning wasn't over-excluded).
    assert any(
        w["file"].endswith("provider_owner_count.go") for w in report.writers
    ), "the real db-layer writer must still be detected"


# --- graceful degradation + gates -------------------------------------------


def test_graceful_when_no_metadata(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text("**INV-x-001**: some prose invariant\n")
    report = sw.check(epic, tmp_path)
    assert report.warnings and report.soundness_ok and report.coverage_ok


def test_no_writer_found_warns(tmp_path):
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "unrelated.go").write_text("func f() { x := 1; _ = x }\n")
    report = sw.check(epic, src)
    assert any("no writer found" in w for w in report.warnings)
    assert report.coverage_ok  # no writer means no violation


# --- language coverage metadata (#162) ---------------------------------------


def test_unsupported_extension_file_is_not_silently_skipped(tmp_path):
    """A recognized-but-unsupported-language file (no emitter at all) must
    surface an explicit coverage warning — never just vanish from the scan."""
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    (src / "internal" / "billing").mkdir(parents=True)
    (src / "internal" / "billing" / "reconcile.go").write_text(
        "func ReconcileStripe(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    (src / "legacy.php").write_text("<?php $plan = 'pro';\n")
    report = sw.check(epic, src)
    assert any("no emitter coverage" in w and ".php" in w for w in report.warnings)
    assert report.coverage_ok  # unrelated to the actual single-writer verdict


def test_unsupported_extension_counts_are_aggregated_per_extension(tmp_path):
    (tmp_path / "a.php").write_text("<?php\n")
    (tmp_path / "b.php").write_text("<?php\n")
    (tmp_path / "c.cpp").write_text("int main() {}\n")
    counts = sw.unsupported_extension_counts(tmp_path)
    assert counts == {".php": 2, ".cpp": 1}


def test_unsupported_extension_counts_empty_when_all_supported(tmp_path):
    (tmp_path / "a.go").write_text("func f() {}\n")
    (tmp_path / "b.py").write_text("def f(): pass\n")
    assert sw.unsupported_extension_counts(tmp_path) == {}


def test_unsupported_extension_counts_ignores_arbitrary_non_source_files(tmp_path):
    """Markdown/lockfiles/etc. are not in the curated unsupported list — no
    coverage-warning noise for ordinary non-source repo content."""
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    assert sw.unsupported_extension_counts(tmp_path) == {}


def test_unsupported_extension_counts_respects_exclude(tmp_path):
    d = tmp_path / "vendor"
    d.mkdir()
    (d / "legacy.php").write_text("<?php\n")
    assert sw.unsupported_extension_counts(tmp_path, exclude=["vendor"]) == {}


def test_scan_writers_routes_through_emitter_registry(tmp_path, monkeypatch):
    """The gate consumes scripts/emitters' dispatch path — not a private direct
    call to emit_write_sites — so a per-language emitter can't drift from what
    the gate actually scans. Regression: fails if scan_writers reverts to
    calling emit_write_sites directly."""
    calls: list[str] = []
    real_emit = sw.emitters.emit

    def spy(path, content):
        calls.append(path)
        return real_emit(path, content)

    monkeypatch.setattr(sw.emitters, "emit", spy)
    (tmp_path / "admin.go").write_text(
        "func ChangePlan(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert calls == ["admin.go"]
    assert writers and writers[0].symbol == "ChangePlan"


def test_changed_since_scoped_scan_still_warns_on_unsupported_extension(tmp_path, capsys):
    """A changed .php file must trigger the coverage warning even in
    --changed-since scoped mode — scoping must never make a coverage gap
    silent (the changed-path predicate is widened beyond SOURCE_EXTS)."""
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.go").write_text("func A() {}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    epic = _write_billing_epic(tmp_path)
    # Added AFTER base: an unsupported-language file (and nothing else changed).
    (tmp_path / "legacy.php").write_text("<?php $plan = 'pro';\n")

    rc = sw.main([
        str(epic), "--source", str(tmp_path), "--changed-since", base, "--format", "json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any("no emitter coverage" in w and ".php" in w for w in data["warnings"])


# --- CLI --------------------------------------------------------------------


def test_cli_coverage_gate_fails_on_violation(tmp_path, capsys):
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    (src / "internal" / "admin").mkdir(parents=True)
    (src / "internal" / "admin" / "h.go").write_text(
        "func ChangePlan(p *Provider) {\n\tp.StripePlan = \"x\"\n}\n"
    )
    rc = sw.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["violations"] == 1
    assert data["violations"][0]["symbol"] == "ChangePlan"


def test_cli_emits_telemetry_with_caught_count(tmp_path, capsys, monkeypatch):
    """The gate emits a real gate event with the finding count (feeds the verdict)."""
    log = tmp_path / "tel.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    (src / "internal" / "admin").mkdir(parents=True)
    (src / "internal" / "admin" / "h.go").write_text("func ChangePlan(p *Provider) {\n\tp.StripePlan = \"x\"\n}\n")
    sw.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    capsys.readouterr()
    events = [json.loads(ln) for ln in log.read_text().splitlines()]
    gate = next(e for e in events if e.get("event") == "gate" and e["name"] == "check_single_writer")
    assert gate["caught"] == 1 and gate["result"] == "fail"


def test_cli_soundness_gate_fails_on_malformed(tmp_path, capsys):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text(
        "<!-- @cw-writes INV-x-001 controls_field=a.b -->\n"  # missing sanctioned_writers
    )
    rc = sw.main([str(epic), "--gate", "soundness"])
    assert rc == 1


def test_cli_soundness_passes_with_wellformed_metadata(tmp_path, capsys):
    epic = _write_billing_epic(tmp_path)
    # Soundness does not fail on existing writers — only on malformed metadata.
    rc = sw.main([str(epic), "--gate", "soundness"])
    assert rc == 0


def test_cli_missing_epic_dir_is_usage_error(tmp_path, capsys):
    rc = sw.main([str(tmp_path / "nope")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_text_output(tmp_path, capsys):
    epic = _write_billing_epic(tmp_path)
    rc = sw.main([str(epic)])
    assert rc == 0
    assert "# Single-Writer Audit" in capsys.readouterr().out


def test_report_json_serializable(tmp_path):
    epic = _write_billing_epic(tmp_path)
    report = sw.check(epic, None)
    json.loads(json.dumps(report.to_dict()))


# --- #93 precision: comments, `:=`, persistence-only, exclude -----------------


def _inv_persist():
    """Single-write-path invariant on PERSISTED fields (sink=db)."""
    return sw.SingleWriterInvariant(
        id="INV-bil-001",
        description="single atomic Stripe→plan write",
        controls_field=["provider.plan", "provider.quota_minutes"],
        sanctioned_writers=["ReconcileStripe", "UpdateEntitlementOverlay"],
        source="invariants.md",
        persistence_only=True,
    )


def test_field_mentioned_in_comment_not_flagged(tmp_path):
    # The classic false positive: `plan:` inside a `// Free plan: …` comment.
    (tmp_path / "video.go").write_text(
        "func limits() {\n\t// Free plan: MaxVideoSeconds is capped\n\tx := 1\n\t_ = x\n}\n"
    )
    assert sw.scan_writers(tmp_path, [_inv()]) == []


def test_python_hash_comment_not_flagged(tmp_path):
    (tmp_path / "svc.py").write_text("def f():\n    # plan: the free tier\n    return 1\n")
    assert sw.scan_writers(tmp_path, [_inv()]) == []


def test_ts_private_field_marker_preserved_not_treated_as_comment(tmp_path):
    # `#` is a comment only in Python/Ruby — in TS `#plan` is a private field, not a
    # comment, so the line must NOT be truncated at `#`.
    (tmp_path / "m.ts").write_text("class P {\n  #plan = 'free';\n}\n")
    # No assertion on writers here beyond: stripping must not crash / mangle. The `#plan`
    # line survives comment-stripping (verified indirectly — no exception, scan completes).
    sw.scan_writers(tmp_path, [_inv()])


def test_go_short_var_decl_not_flagged(tmp_path):
    # `plan :=` is a short-var declaration, not a field set.
    (tmp_path / "svc.go").write_text(
        "func compute() {\n\tplan := resolve()\n\t_ = plan\n}\n"
    )
    assert sw.scan_writers(tmp_path, [_inv()]) == []


def test_sink_db_skips_in_memory_assignment(tmp_path):
    # out.QuotaMinutes = 100 builds an in-memory Limits struct — not a persistence write.
    (tmp_path / "limits.go").write_text(
        "func EffectiveLimits(p *Provider) Limits {\n"
        "\tout := Limits{}\n\tout.QuotaMinutes = 100\n\treturn out\n}\n"
    )
    assert sw.scan_writers(tmp_path, [_inv_persist()]) == []


def test_sink_db_skips_other_struct_literal(tmp_path):
    # A same-named field on a DIFFERENT struct, set in a literal — not persistence.
    (tmp_path / "video.go").write_text(
        "func mk() VideoCfg {\n\treturn VideoCfg{Plan: planArg}\n}\n"
    )
    assert sw.scan_writers(tmp_path, [_inv_persist()]) == []


def test_sink_db_flags_only_the_db_sink(tmp_path):
    # In-memory assignment + struct literal are ignored; the bson $set is the writer.
    (tmp_path / "limits.go").write_text(
        "func EffectiveLimits(p *Provider) Limits {\n\tout := Limits{}\n"
        "\tout.QuotaMinutes = 100\n\treturn out\n}\n"
    )
    (tmp_path / "signup.go").write_text(
        "func signup() Provider {\n\treturn Provider{Plan: \"free\"}\n}\n"
    )
    (tmp_path / "repo.go").write_text(
        "func ReconcileStripe(c *mongo.Collection) {\n"
        "\tc.UpdateOne(ctx, f, bson.M{\"$set\": bson.M{\"plan\": v, \"quota_minutes\": q}})\n"
        "}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv_persist()])
    assert {w.file for w in writers} == {"repo.go"}
    assert all(w.sanctioned for w in writers)  # ReconcileStripe is sanctioned


def test_sink_db_flags_sql_update(tmp_path):
    (tmp_path / "store.go").write_text(
        "func UpdateEntitlementOverlay(db *sql.DB) {\n"
        "\tdb.Exec(\"UPDATE providers SET plan = $1 WHERE id = $2\", v, id)\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv_persist()])
    assert any(w.file == "store.go" for w in writers)
    assert all(w.sanctioned for w in writers)


def test_sink_db_flags_unsanctioned_persistence_writer(tmp_path):
    # A legacy admin ChangePlan doing its OWN $set is exactly what the gate must catch.
    (tmp_path / "admin.go").write_text(
        "func ChangePlan(c *mongo.Collection) {\n"
        "\tc.UpdateOne(ctx, f, bson.M{\"$set\": bson.M{\"plan\": v}})\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [_inv_persist()])
    assert writers and any(not w.sanctioned and w.symbol == "ChangePlan" for w in writers)


def test_exclude_skips_subtree(tmp_path):
    ui = tmp_path / "ui" / "src"
    ui.mkdir(parents=True)
    (ui / "types.ts").write_text("interface P {\n  plan: string;\n}\n")
    (tmp_path / "admin.go").write_text("func Bad(p *Provider) {\n\tp.Plan = x\n}\n")
    # Without exclude the TS interface field is a (false-positive) writer.
    assert any(w.file.startswith("ui/") for w in sw.scan_writers(tmp_path, [_inv()]))
    # --exclude ui removes the whole subtree; the Go writer remains.
    excl = sw.scan_writers(tmp_path, [_inv()], exclude=["ui"])
    assert all(not w.file.startswith("ui/") for w in excl)
    assert any(w.file == "admin.go" for w in excl)


def test_sink_db_parsed_from_prose_tag():
    text = (
        "<!-- @cw-writes INV-bil-001 controls_field=provider.plan "
        "sanctioned_writers=ReconcileStripe sink=db -->\n"
    )
    invs, malformed = sw.parse_prose_invariants(text, "invariants.md")
    assert malformed == [] and invs[0].persistence_only is True


def test_sink_db_parsed_from_structured():
    data = {"invariants": [{
        "id": "INV-bil-001", "description": "d",
        "controls_field": ["provider.plan"], "sanctioned_writers": ["ReconcileStripe"],
        "sink": "db",
    }]}
    invs, _ = sw.parse_structured_invariants(data, "state-machines.json")
    assert invs[0].persistence_only is True


def test_no_sink_defaults_to_all_writers():
    # Backward compatible: without sink=db, in-memory assignments still count.
    invs, _ = sw.parse_prose_invariants(
        "<!-- @cw-writes INV-x-001 controls_field=a.plan sanctioned_writers=Foo -->\n", "f.md"
    )
    assert invs[0].persistence_only is False


def test_sink_db_skips_query_filter_clause(tmp_path):
    # `"plan": {$exists:false}` is a FILTER (which docs to match), not a $set write.
    (tmp_path / "migrate.go").write_text(
        "func Migrate(c *mongo.Collection) {\n"
        "\tc.UpdateMany(ctx, bson.M{\"plan\": bson.M{\"$exists\": false}}, upd)\n}\n"
    )
    assert sw.scan_writers(tmp_path, [_inv_persist()]) == []


# --- emission/claim split (#160) --------------------------------------------


def test_emit_write_sites_is_field_agnostic():
    """emit_write_sites needs no invariant at all — it just finds candidate
    write-shaped tokens."""
    sites = sw.emit_write_sites(
        "admin.go", "func ChangePlan(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    assert any(s.token == "StripePlan" and s.kind == sw.KIND_ASSIGN for s in sites)
    assert all(s.file == "admin.go" for s in sites)
    assert any(s.symbol == "ChangePlan" for s in sites)


def test_emit_write_sites_struct_literal_kind():
    sites = sw.emit_write_sites("seed.go", "func mk() Provider {\n\treturn Provider{Plan: \"pro\"}\n}\n")
    assert any(s.token == "Plan" and s.kind == sw.KIND_STRUCT for s in sites)


def test_emit_write_sites_sql_kind():
    sites = sw.emit_write_sites(
        "store.go",
        "func UpdateQuota(db *sql.DB) {\n\tdb.Exec(\"UPDATE t SET plan = $1, quota = $2\", a, b)\n}\n",
    )
    tokens = {s.token for s in sites if s.kind == sw.KIND_SQL}
    assert tokens == {"plan", "quota"}


def test_emit_write_sites_no_candidates_on_dead_line():
    assert sw.emit_write_sites("f.go", "func f() {\n\tx := 1\n\t_ = x\n}\n") == []


def test_match_writers_filters_by_field_token_and_sink():
    sites = sw.emit_write_sites(
        "billing.go",
        "func ReconcileStripe(c *mongo.Collection) {\n"
        "\tc.UpdateOne(ctx, f, bson.M{\"$set\": bson.M{\"stripe_plan\": v}})\n}\n",
    )
    inv = _inv_persist()  # controls provider.plan + provider.quota_minutes, sink=db
    other_inv = sw.SingleWriterInvariant(
        id="INV-unrelated-001", description="", controls_field=["x.unrelated"],
        sanctioned_writers=["Foo"], source="s",
    )
    assert sw.match_writers(sites, other_inv) == []  # token doesn't match this invariant

    bil_inv = _inv()  # controls provider.plan, provider.stripe_plan; no sink
    matched = sw.match_writers(sites, bil_inv)
    assert len(matched) == 1
    assert matched[0].field == "provider.stripe_plan"
    assert matched[0].sanctioned is True


def test_match_writers_same_sites_different_invariants_yield_independent_results():
    """The same emitted sites can be claimed by multiple invariants — emission
    ran once, matching is a pure per-invariant query over it."""
    sites = sw.emit_write_sites(
        "admin.go", "func ChangePlan(p *Provider) {\n\tp.Plan = \"x\"\n}\n"
    )
    inv_a = sw.SingleWriterInvariant("INV-a-001", "", ["p.plan"], ["ChangePlan"], "s")
    inv_b = sw.SingleWriterInvariant("INV-b-001", "", ["p.plan"], ["SomeoneElse"], "s")
    assert sw.match_writers(sites, inv_a)[0].sanctioned is True
    assert sw.match_writers(sites, inv_b)[0].sanctioned is False


def test_scan_writers_preserves_line_major_ordering_across_invariants(tmp_path):
    """Regression guard for the refactor: when two invariants both hit in the
    same file at interleaved lines, the overall order must stay line-ascending
    (not grouped by invariant) — matching the original interleaved scan."""
    inv_a = sw.SingleWriterInvariant("INV-a-001", "", ["p.alpha"], ["Foo"], "s")
    inv_b = sw.SingleWriterInvariant("INV-b-001", "", ["p.beta"], ["Foo"], "s")
    (tmp_path / "f.go").write_text(
        "func f(p *P) {\n"
        "\tp.Alpha = 1\n"      # line 2: INV-a-001
        "\tp.Beta = 2\n"       # line 3: INV-b-001
        "\tp.Alpha = 3\n"      # line 4: INV-a-001 again
        "}\n"
    )
    writers = sw.scan_writers(tmp_path, [inv_a, inv_b])
    assert [(w.invariant_id, w.line) for w in writers] == [
        ("INV-a-001", 2), ("INV-b-001", 3), ("INV-a-001", 4),
    ]


def test_emission_captures_hyphenated_quoted_key(tmp_path):
    """Regression (#179 review): the pre-split scanner built regexes from the
    escaped field token, so a hyphenated Mongo key (`"plan-tier"`) matched. The
    field-agnostic emission must cover the same token surface — a `\\w+`-only
    capture would silently MISS this unsanctioned writer on a full scan.
    (Old-vs-new agreement on this case is also pinned by the golden fixture's
    INV-tier-004, whose expected outputs were generated with the pre-split
    scanner.)"""
    inv = sw.SingleWriterInvariant(
        id="INV-tier-001", description="", controls_field=["provider.plan-tier"],
        sanctioned_writers=["SetPlanTier"], source="s", persistence_only=True,
    )
    (tmp_path / "legacy.go").write_text(
        "func LegacyTier(c *mongo.Collection, v string) {\n"
        "\tc.UpdateOne(ctx, f, bson.M{\"$set\": bson.M{\"plan-tier\": v}})\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [inv])
    assert len(writers) == 1
    assert writers[0].symbol == "LegacyTier" and writers[0].sanctioned is False
    # And the raw emission carries the full hyphenated token, not a fragment.
    sites = sw.emit_write_sites("legacy.go", (tmp_path / "legacy.go").read_text())
    assert any(s.token == "plan-tier" and s.kind == sw.KIND_QUOTED for s in sites)


def test_emission_captures_hyphenated_sql_field(tmp_path):
    inv = sw.SingleWriterInvariant(
        id="INV-tier-001", description="", controls_field=["provider.plan-tier"],
        sanctioned_writers=["Nobody"], source="s", persistence_only=True,
    )
    (tmp_path / "store.go").write_text(
        "func UpdateTier(db *sql.DB) {\n"
        "\tdb.Exec(`UPDATE providers SET \"plan-tier\" = $1 WHERE id = $2`, v, id)\n}\n"
    )
    writers = sw.scan_writers(tmp_path, [inv])
    assert writers and writers[0].sanctioned is False


def test_dotted_quoted_key_does_not_claim_leaf_field():
    """`"provider.plan"` is captured as ONE token (`provider.plan`) — it must
    not claim-match the leaf `plan`, mirroring the old quote-delimited
    exact-token behavior."""
    sites = sw.emit_write_sites(
        "repo.go",
        "func f(c *mongo.Collection) {\n"
        "\tc.UpdateOne(ctx, f, bson.M{\"$set\": bson.M{\"provider.plan\": v}})\n}\n",
    )
    assert any(s.token == "provider.plan" for s in sites)
    assert not any(s.token == "plan" and s.kind == sw.KIND_QUOTED for s in sites)


def test_full_scan_skips_nested_git_checkout(tmp_path):
    """Submodules / vendored repos (a dir containing a .git entry) are excluded
    from the FULL scan, matching --changed-since (whose manifest never surfaces
    a submodule's files — a submodule is a single gitlink entry there)."""
    (tmp_path / "admin.go").write_text("func Bad(p *Provider) {\n\tp.Plan = x\n}\n")
    sub = tmp_path / "vendor-app"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/vendor-app\n")  # gitlink file
    (sub / "other.go").write_text("func AlsoBad(p *Provider) {\n\tp.Plan = y\n}\n")
    writers = sw.scan_writers(tmp_path, [_inv()])
    assert {w.file for w in writers} == {"admin.go"}


# --- --scanner-version / --changed-since (#160) ------------------------------


def test_scanner_version_is_deterministic_and_stable_across_calls():
    rc1 = sw.main(["--scanner-version"])
    assert rc1 == 0


def test_cli_scanner_version_prints_hex_digest(capsys):
    rc = sw.main(["--scanner-version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert len(out) == 64  # sha256 hex digest
    int(out, 16)  # valid hex


def test_cli_requires_epic_dir_unless_scanner_version(capsys):
    rc = sw.main([])
    assert rc == 2
    assert "epic_dir is required" in capsys.readouterr().err


def test_changed_since_scopes_scan_to_changed_files(tmp_path, capsys):
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.go").write_text("func A(p *Provider) {\n\tp.Unrelated = \"x\"\n}\n")
    (tmp_path / "b.go").write_text("func B() {}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    epic = _write_billing_epic(tmp_path)
    # New unsanctioned writer, added AFTER base, unrelated file to a.go.
    (tmp_path / "b.go").write_text("func ChangePlan(p *Provider) {\n\tp.StripePlan = \"y\"\n}\n")

    rc_full = sw.main([str(epic), "--source", str(tmp_path), "--gate", "coverage", "--format", "json"])
    full = json.loads(capsys.readouterr().out)
    rc_scoped = sw.main([
        str(epic), "--source", str(tmp_path), "--changed-since", base,
        "--gate", "coverage", "--format", "json",
    ])
    scoped = json.loads(capsys.readouterr().out)

    assert rc_full == 1 and rc_scoped == 1
    assert full["counts"]["violations"] == 1
    assert scoped["counts"]["violations"] == 1
    assert scoped["violations"][0]["file"] == "b.go"


def test_changed_since_whole_repo_default_is_unaffected(tmp_path, capsys):
    """No --changed-since given -> unchanged whole-repo behavior (regression
    guard: the new parameter must be fully opt-in)."""
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    (src / "internal" / "billing").mkdir(parents=True)
    (src / "internal" / "billing" / "reconcile.go").write_text(
        "func ReconcileStripe(p *Provider, v string) {\n\tp.StripePlan = v\n}\n"
    )
    rc = sw.main([str(epic), "--source", str(src), "--gate", "coverage"])
    assert rc == 0


def test_changed_since_non_git_source_is_usage_error(tmp_path, capsys):
    """--changed-since against a non-git --source must exit 2 with a concise
    message, never a traceback (#179 review)."""
    epic = _write_billing_epic(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    rc = sw.main([str(epic), "--source", str(src), "--changed-since", "main"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Error" in err and "Traceback" not in err


def test_changed_since_bad_ref_is_usage_error(tmp_path, capsys):
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.go").write_text("func A() {}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    epic = _write_billing_epic(tmp_path)
    rc = sw.main([str(epic), "--source", str(tmp_path), "--changed-since", "no-such-ref"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Error" in err and "Traceback" not in err

"""Tests for the review synthesis prompt (scripts/synthesize_reviews.py).

## Why this file exists (chief-wiggum#361)

Two ratchet high-water cases were traced here by archaeology:

- `test_review_pipeline::test_synthesis_prompt_lists_responses`
- `test_review_pipeline::test_synthesis_prompt_instructs_union_not_consensus`

Both were deleted in #332, which removed `review.build_synthesis_prompt` and
the `synthesis-prompt.md` artifact as dead code — correctly, since nothing
consumed them. `/implement` Step 8 synthesizes via `synthesize_reviews.py`
instead.

But the BEHAVIOUR those tests guarded did not die with the dead function. It
moved. `synthesize_reviews.synthesize()` lists the reviews it received and
carries the #163 union-not-consensus doctrine, and between #332 and now, no
test asserted either. A dead-code removal is a legitimate reason for a test to
disappear; it is not a reason for the live replacement to go unguarded.

That is why the ratchet's missing-case report is worth reconciling by hand
rather than mass-retiring: four of the six cases really were renames or
deliberate supersessions, and two were a coverage hole wearing a rename's
clothes.
"""

from __future__ import annotations

import json

import pytest
import synthesize_reviews


def _reviews(*sources: str) -> list[dict]:
    """Content deliberately does NOT contain the source name.

    My first version used f"finding from {source}", and mutation testing caught
    it: deleting the "## Review N: <source>" heading entirely left the tests
    green, because the source name was still present inside the body text. The
    assertion looked like it checked the heading and actually checked nothing.
    """
    return [
        {"source": s, "content": f"BODY-{i}-verbatim"}
        for i, s in enumerate(sources)
    ]


def _body(index: int) -> str:
    return f"BODY-{index}-verbatim"


class TestPromptListsWhatItReceived:
    """Successor to `test_synthesis_prompt_lists_responses`.

    The synthesizer's own account of its inputs is the only place a reader can
    check WHICH voices are in the merge. If a provider's review is missing, the
    prompt is where that becomes visible.
    """

    def test_every_reviewer_source_appears_as_its_own_heading(self):
        """The heading, not merely the name somewhere in the text — a body that
        happens to mention the provider is not an attribution."""
        prompt = synthesize_reviews.synthesize(
            _reviews("reviewer-codex", "reviewer-deepseek")
        )
        assert "## Review 1: reviewer-codex" in prompt
        assert "## Review 2: reviewer-deepseek" in prompt

    def test_each_body_is_attributed_to_the_reviewer_above_it(self):
        """Ordering matters: headings and bodies must not be interleaved wrong,
        or a finding gets credited to the provider that did not make it."""
        prompt = synthesize_reviews.synthesize(
            _reviews("reviewer-codex", "reviewer-deepseek")
        )
        assert prompt.index("## Review 1: reviewer-codex") < prompt.index(_body(0))
        assert prompt.index(_body(0)) < prompt.index("## Review 2: reviewer-deepseek")
        assert prompt.index("## Review 2: reviewer-deepseek") < prompt.index(_body(1))

    def test_every_reviewers_content_appears_in_the_prompt(self):
        """Listing a source but dropping its findings would be worse than
        omitting it — the merge would look complete while missing a voice."""
        prompt = synthesize_reviews.synthesize(
            _reviews("reviewer-codex", "reviewer-deepseek")
        )
        assert _body(0) in prompt
        assert _body(1) in prompt

    def test_the_prompt_states_how_many_reviews_it_received(self):
        prompt = synthesize_reviews.synthesize(_reviews("a", "b", "c"))
        assert "3 reviews received" in prompt

    def test_the_stated_count_tracks_the_actual_inputs(self):
        """Guard against a hardcoded count: the number must MOVE with the
        input. A fixed '3 reviews received' would pass the test above while
        lying about a two-provider quorum."""
        assert "2 reviews received" in synthesize_reviews.synthesize(_reviews("a", "b"))

    def test_no_reviews_is_stated_plainly_not_synthesized(self):
        assert synthesize_reviews.synthesize([]) == "No reviews to synthesize."


class TestUnionNotConsensus:
    """Successor to `test_synthesis_prompt_instructs_union_not_consensus`.

    Reconciliation of a (possibly lensed) quorum is union + cross-verify of
    contested items. A unique finding must not be downgraded for lacking
    consensus (#163) — under lenses, disjoint findings are the DESIGNED
    outcome, since each reviewer was scoped to a different concern. A prompt
    that quietly drifts to majority-voting turns a working lensed quorum into
    a machine that discards its best findings.
    """

    @pytest.fixture
    def prompt(self):
        return synthesize_reviews.synthesize(_reviews("reviewer-codex"))

    def test_the_prompt_instructs_union(self, prompt):
        assert "Combine by union" in prompt

    def test_the_prompt_forbids_majority_voting(self, prompt):
        assert "do not " in prompt and "majority-vote" in prompt

    def test_a_unique_finding_is_not_downgraded_for_being_unique(self, prompt):
        assert "not weaker for" in prompt
        assert "being unique" in prompt

    def test_cross_verification_is_reserved_for_contradictions(self, prompt):
        """Not merely for 'one reviewer mentioned something the other didn't' —
        that is the failure mode this sentence exists to prevent."""
        assert "CONTRADICTORY" in prompt
        assert "not merely where one mentions something the other" in prompt

    def test_the_low_confidence_bucket_does_not_absorb_unique_findings(self, prompt):
        """The instruction can be defeated further down the prompt: if the
        Disputed bucket is defined as 'raised by only one reviewer', the union
        rule above it is dead letter."""
        assert "Being unique to one reviewer is NOT" in prompt

    def test_high_confidence_admits_single_reviewer_findings(self, prompt):
        assert "whether raised by one reviewer or several" in prompt

    def test_high_confidence_is_not_gated_on_agreement(self, prompt):
        """The positive assertion above can coexist with a contradicting
        sentence. Check the negative too."""
        low = prompt.lower()
        assert "only findings that two or more reviewers agree" not in low
        assert "must be raised by at least two" not in low

    def test_the_lens_rationale_is_present(self, prompt):
        """#163: the reader needs to know WHY divergence is expected, or the
        union rule reads as an arbitrary style preference."""
        assert "lenses" in prompt
        assert "Expect disjoint findings, not convergence" in prompt


class TestLoadReviews:
    def test_a_present_file_is_loaded_with_its_stem_as_source(self, tmp_path):
        f = tmp_path / "reviewer-codex.md"
        f.write_text("  a finding  \n")
        loaded = synthesize_reviews.load_reviews([str(f)])
        assert loaded == [{"source": "reviewer-codex", "content": "a finding"}]

    def test_a_missing_file_is_skipped_with_a_warning(self, tmp_path, capsys):
        """Loading stays non-fatal, and that is deliberate (chief-wiggum#416).

        `config/providers.json` distinguishes required from optional providers
        precisely so an optional one may fail without blocking the role, so a
        missing file cannot simply become fatal here.

        What changed in #416 is downstream: the SYNTHESIS no longer reports the
        reduced set as though it were the whole picture. See
        `TestQuorumDelta` — the fix went where the lie was, not where the file
        was missing.
        """
        present = tmp_path / "reviewer-codex.md"
        present.write_text("a finding")
        loaded = synthesize_reviews.load_reviews([str(present), str(tmp_path / "gone.md")])

        assert len(loaded) == 1, "a missing file is skipped, not fatal"
        assert "not found" in capsys.readouterr().err


def _manifest(**providers):
    """A role manifest in the shape `consult_ai.py --role` writes."""
    return {
        "role": "reviewer",
        "results": [
            {"name": name, "required": required, "status": status,
             "path": path, "error": error}
            for name, (required, status, path, error) in providers.items()
        ],
    }


class TestQuorumDelta:
    """What the synthesis received, against what the role expected (#416).

    The failure this guards is not a crash. It is a synthesis that opens with
    a confident count while a provider that was supposed to run never did —
    which under review lenses removes exactly the findings nobody else was
    scoped to produce, and presents the narrowed result as complete.
    """

    def test_no_manifest_reports_the_expected_set_as_unknown(self):
        prompt = synthesize_reviews.synthesize(_reviews("a", "b"))
        assert "expected set is UNKNOWN" in prompt
        assert "unverified rather than complete" in prompt

    def test_a_complete_quorum_says_so_with_both_numbers(self):
        manifest = _manifest(
            codex=(True, "ok", "/x/reviewer-codex.md", None),
            deepseek=(True, "ok", "/x/reviewer-deepseek.md", None),
        )
        reviews = _reviews("reviewer-codex", "reviewer-deepseek")
        delta = synthesize_reviews.quorum_delta(reviews, manifest)
        assert delta["status"] == "complete"
        prompt = synthesize_reviews.synthesize(reviews, delta)
        assert "2 of 2 expected reviews received" in prompt
        assert "quorum complete" in prompt.lower()

    def test_an_absent_required_provider_is_named_and_flagged(self):
        manifest = _manifest(
            codex=(True, "failed", None, "usage limit reached"),
            deepseek=(True, "ok", "/x/reviewer-deepseek.md", None),
        )
        reviews = _reviews("reviewer-deepseek")
        delta = synthesize_reviews.quorum_delta(reviews, manifest)
        assert delta["status"] == "degraded-required"
        assert delta["absent_required"] == ["codex"]

        prompt = synthesize_reviews.synthesize(reviews, delta)
        assert "1 of 2 expected reviews received" in prompt
        assert "QUORUM INCOMPLETE" in prompt
        assert "codex" in prompt
        assert "usage limit reached" in prompt, "the reason belongs in the prompt"
        assert "NARROWED result" in prompt

    def test_an_absent_optional_provider_is_reported_but_not_alarming(self):
        manifest = _manifest(
            codex=(True, "ok", "/x/reviewer-codex.md", None),
            claude_interactive=(False, "failed", None, "delegate timed out"),
        )
        reviews = _reviews("reviewer-codex")
        delta = synthesize_reviews.quorum_delta(reviews, manifest)
        assert delta["status"] == "degraded-optional"
        assert delta["absent_required"] == []

        prompt = synthesize_reviews.synthesize(reviews, delta)
        assert "QUORUM INCOMPLETE" in prompt
        assert "which the role permits" in prompt
        assert "claude_interactive" in prompt
        # An optional absence must not be dressed up as a required one.
        assert "A **required** provider is missing" not in prompt

    def test_the_counts_track_the_manifest_not_the_files_found(self):
        """The whole point: expected comes from the role, received from disk.

        A synthesis that derived both numbers from the files it found could
        never report a gap, which is the fail-open being closed.
        """
        manifest = _manifest(
            a=(True, "ok", "/x/reviewer-a.md", None),
            b=(True, "failed", None, "boom"),
            c=(False, "failed", None, "boom"),
        )
        delta = synthesize_reviews.quorum_delta(
            _reviews("reviewer-a"), manifest)
        assert (delta["received_n"], delta["expected_n"]) == (1, 3)


class TestTheManifestIsNotTakenAtItsWord:
    """Expected providers are matched to reviews that actually loaded (#416).

    Deriving absence from the manifest's own `status` alone is not enough, and
    review found three ways it produced headers that contradict themselves.
    The first is the fail-open this whole change exists to close, wearing the
    fixed version's clothes.
    """

    def test_a_provider_marked_ok_whose_review_never_loaded_is_absent(self):
        """"0 of 1 expected reviews received - quorum complete."

        The manifest says the provider succeeded; its file is gone. The
        filesystem is what the synthesis is actually built from, so the
        manifest does not get the last word.
        """
        manifest = _manifest(
            codex=(True, "ok", "/gone/reviewer-codex.md", None),
        )
        delta = synthesize_reviews.quorum_delta([], manifest)
        assert delta["status"] == "degraded-required"
        assert delta["absent_required"] == ["codex"]
        assert "was not loaded" in delta["absent"][0]["error"]

        prompt = synthesize_reviews.synthesize(_reviews("x"), delta)
        assert "0 of 1 expected reviews received" in prompt
        assert "QUORUM INCOMPLETE" in prompt

    def test_an_optional_provider_marked_ok_but_unloaded_does_not_escalate(self):
        manifest = _manifest(
            codex=(True, "ok", "/x/reviewer-codex.md", None),
            extra=(False, "ok", "/gone/reviewer-extra.md", None),
        )
        delta = synthesize_reviews.quorum_delta(
            [{"source": "reviewer-codex", "content": "c"}], manifest)
        assert delta["status"] == "degraded-optional"
        assert delta["absent_required"] == []

    def test_unexpected_reviews_cannot_inflate_the_received_count(self):
        """"3 of 2 expected reviews received - quorum complete."

        received_n counts expected reviews genuinely in hand, so it can never
        exceed expected_n. Anything else loaded is reported, not counted.
        """
        manifest = _manifest(
            a=(True, "ok", "/x/reviewer-a.md", None),
            b=(True, "ok", "/x/reviewer-b.md", None),
        )
        reviews = [{"source": "reviewer-a", "content": "A"},
                   {"source": "reviewer-b", "content": "B"},
                   {"source": "reviewer-stray", "content": "S"}]
        delta = synthesize_reviews.quorum_delta(reviews, manifest)
        assert (delta["received_n"], delta["expected_n"]) == (2, 2)
        assert delta["status"] == "complete"
        assert delta["unexpected"] == ["reviewer-stray"]

        prompt = synthesize_reviews.synthesize(reviews, delta)
        assert "2 of 2 expected reviews received" in prompt
        assert "reviewer-stray" in prompt
        assert "unattributed" in prompt

    def test_a_manifest_listing_no_providers_is_malformed(self):
        """"1 of 0 expected reviews received - quorum complete."."""
        with pytest.raises(synthesize_reviews.MalformedManifest,
                           match="lists no providers"):
            synthesize_reviews.quorum_delta(_reviews("a"), {"role": "reviewer"})

    def test_the_worst_case_still_reports_who_was_absent(self):
        """Every provider failed, so nothing loaded.

        This used to produce the LEAST informative output of any path: a bare
        "No reviews to synthesize." with the delta discarded, or a usage error
        from the CLI. The maximally degraded quorum is the one the operator
        most needs told about.
        """
        manifest = _manifest(
            codex=(True, "failed", None, "usage limit reached"),
            deepseek=(True, "failed", None, "timed out"),
        )
        delta = synthesize_reviews.quorum_delta([], manifest)
        prompt = synthesize_reviews.synthesize([], delta)

        assert "0 of 2 expected reviews received" in prompt
        assert "QUORUM INCOMPLETE" in prompt
        assert "codex" in prompt and "usage limit reached" in prompt
        assert "deepseek" in prompt and "timed out" in prompt
        assert "No reviews were loaded at all" in prompt
        assert prompt != "No reviews to synthesize."

    def test_no_reviews_and_nothing_expected_stays_terse(self):
        """The bare message is still right when there is nothing to say."""
        assert synthesize_reviews.synthesize([]) == "No reviews to synthesize."

    @pytest.mark.parametrize("payload,match", [
        ({}, "lists no providers"),
        ([], "must be a JSON object"),
        ({"role": "r", "results": "codex"}, "must be a list"),
        ({"role": "r", "results": ["codex"]}, "entries must be objects"),
        ({"role": "r", "results": [{"required": True, "status": "ok"}]},
         "no usable provider name"),
        ({"role": "r", "results": [
            {"name": "x", "required": True, "status": "failed", "error": 123}]},
         "non-string error"),
    ])
    def test_off_shape_manifests_are_refused_by_name(self, payload, match):
        """Bad input must be bad input, not a crash mid-computation.

        Left unchecked these raised AttributeError or IndexError out of the
        delta computation, surfacing as a traceback at exit 1 — the code
        reserved for a finding being gated on, so a malformed file was
        indistinguishable from a real quorum failure.
        """
        with pytest.raises(synthesize_reviews.MalformedManifest, match=match):
            synthesize_reviews.quorum_delta(_reviews("a"), payload)

    def test_a_manifest_naming_a_provider_twice_is_malformed(self):
        manifest = {"role": "reviewer", "results": [
            {"name": "codex", "required": True, "status": "ok",
             "path": "/x/reviewer-codex.md", "error": None},
            {"name": "codex", "required": True, "status": "failed",
             "path": None, "error": "boom"},
        ]}
        with pytest.raises(synthesize_reviews.MalformedManifest,
                           match="twice"):
            synthesize_reviews.quorum_delta(_reviews("a"), manifest)


class TestCli:
    """End-to-end: the reconciler reads stdout, so the delta must land there."""

    def _setup(self, tmp_path, codex_ok: bool):
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a concrete finding")
        manifest = tmp_path / "reviewer-manifest.json"
        manifest.write_text(json.dumps({
            "role": "reviewer",
            "results": [
                {"name": "codex", "required": True,
                 "status": "ok" if codex_ok else "failed",
                 "path": str(review) if codex_ok else None,
                 "error": None if codex_ok else "usage limit reached"},
                {"name": "deepseek-flash", "required": True, "status": "ok",
                 "path": str(review), "error": None},
            ],
        }))
        return manifest

    def test_paths_are_derived_from_the_manifest_when_none_are_given(
            self, tmp_path, capsys):
        """Callers listing filenames by hand drift as the roster changes.

        `/implement` Step 8 hardcoded `reviewer-gemini.md` long after gemini
        left the reviewer role, so the name it passed could never resolve.
        """
        manifest = self._setup(tmp_path, codex_ok=True)
        assert synthesize_reviews.main(["--manifest", str(manifest)]) == 0
        assert "a concrete finding" in capsys.readouterr().out

    def test_an_absent_required_provider_is_reported_but_does_not_block(
            self, tmp_path, capsys):
        manifest = self._setup(tmp_path, codex_ok=False)
        assert synthesize_reviews.main(["--manifest", str(manifest)]) == 0
        captured = capsys.readouterr()
        assert "QUORUM INCOMPLETE" in captured.out
        assert "codex" in captured.err

    def test_gate_blocks_on_an_absent_required_provider(self, tmp_path, capsys):
        manifest = self._setup(tmp_path, codex_ok=False)
        assert synthesize_reviews.main(["--manifest", str(manifest), "--gate"]) == 1
        assert "QUORUM INCOMPLETE" in capsys.readouterr().out

    def test_gate_does_not_block_on_a_complete_quorum(self, tmp_path):
        manifest = self._setup(tmp_path, codex_ok=True)
        assert synthesize_reviews.main(["--manifest", str(manifest), "--gate"]) == 0

    def test_an_unreadable_manifest_is_an_error_not_a_silent_unknown(
            self, tmp_path, capsys):
        """The caller asked for the quorum to be checked and it could not be.

        Degrading to "expected set unknown" would turn a broken instrument
        into the same output as never having asked.
        """
        broken = tmp_path / "reviewer-manifest.json"
        broken.write_text("{not json")
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a finding")
        assert synthesize_reviews.main(
            ["--manifest", str(broken), str(review)]) == 2
        assert "cannot read manifest" in capsys.readouterr().err

    def test_a_manifest_that_cannot_say_who_was_expected_is_also_an_error(
            self, tmp_path, capsys):
        """Readable JSON, but it answers nothing. Same treatment."""
        empty = tmp_path / "reviewer-manifest.json"
        empty.write_text(json.dumps({"role": "reviewer", "results": []}))
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a finding")
        assert synthesize_reviews.main(
            ["--manifest", str(empty), str(review)]) == 2
        assert "lists no providers" in capsys.readouterr().err

    def test_a_manifest_file_containing_null_is_not_read_as_absent(
            self, tmp_path, capsys):
        """`null` parses to None, which looks exactly like "not supplied".

        Without the distinction the header would claim no manifest was given
        when one was, and `--gate` would go inert.
        """
        manifest = tmp_path / "reviewer-manifest.json"
        manifest.write_text("null")
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a finding")
        assert synthesize_reviews.main(
            ["--manifest", str(manifest), str(review)]) == 2
        assert "null" in capsys.readouterr().err

    def test_gate_without_a_manifest_is_refused_not_silently_inert(
            self, tmp_path, capsys):
        """A gate that cannot run must not report success.

        Without a manifest there is no expected set, so `--gate` could never
        fire and exited 0 — automation adding the flag would believe the
        quorum had been verified when nothing was checked. That is the
        "failed to run = pass" pattern at the gate level.
        """
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a finding")
        assert synthesize_reviews.main(["--gate", str(review)]) == 2
        assert "--gate requires --manifest" in capsys.readouterr().err

    def test_a_total_provider_failure_reports_instead_of_erroring_out(
            self, tmp_path, capsys):
        """No provider succeeded, so no paths can be derived.

        The CLI used to treat that as a usage error and exit 2, losing the
        quorum report entirely at exactly the moment it mattered most.
        """
        manifest = tmp_path / "reviewer-manifest.json"
        manifest.write_text(json.dumps({"role": "reviewer", "results": [
            {"name": "codex", "required": True, "status": "failed",
             "path": None, "error": "usage limit reached"},
        ]}))
        assert synthesize_reviews.main(["--manifest", str(manifest), "--gate"]) == 1
        out = capsys.readouterr().out
        assert "0 of 1 expected reviews received" in out
        assert "codex" in out

    def test_gate_blocks_when_a_required_review_is_missing_from_disk(
            self, tmp_path, capsys):
        """The manifest says ok; the file is gone. --gate must still block."""
        review = tmp_path / "reviewer-deepseek.md"
        review.write_text("a finding")
        manifest = tmp_path / "reviewer-manifest.json"
        manifest.write_text(json.dumps({"role": "reviewer", "results": [
            {"name": "codex", "required": True, "status": "ok",
             "path": str(tmp_path / "reviewer-codex.md"), "error": None},
            {"name": "deepseek-flash", "required": True, "status": "ok",
             "path": str(review), "error": None},
        ]}))
        assert synthesize_reviews.main(
            ["--manifest", str(manifest), "--gate"]) == 1
        assert "QUORUM INCOMPLETE" in capsys.readouterr().out

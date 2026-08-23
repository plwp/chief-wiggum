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
        delta = synthesize_reviews.quorum_delta(_reviews("a", "b"), manifest)
        assert delta["status"] == "complete"
        prompt = synthesize_reviews.synthesize(_reviews("a", "b"), delta)
        assert "2 of 2 expected reviews received" in prompt
        assert "quorum complete" in prompt.lower()

    def test_an_absent_required_provider_is_named_and_flagged(self):
        manifest = _manifest(
            codex=(True, "failed", None, "usage limit reached"),
            deepseek=(True, "ok", "/x/reviewer-deepseek.md", None),
        )
        delta = synthesize_reviews.quorum_delta(_reviews("a"), manifest)
        assert delta["status"] == "degraded-required"
        assert delta["absent_required"] == ["codex"]

        prompt = synthesize_reviews.synthesize(_reviews("a"), delta)
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
        delta = synthesize_reviews.quorum_delta(_reviews("a"), manifest)
        assert delta["status"] == "degraded-optional"
        assert delta["absent_required"] == []

        prompt = synthesize_reviews.synthesize(_reviews("a"), delta)
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
            a=(True, "ok", "/x/a.md", None),
            b=(True, "failed", None, "boom"),
            c=(False, "failed", None, "boom"),
        )
        delta = synthesize_reviews.quorum_delta(_reviews("a"), manifest)
        assert (delta["received_n"], delta["expected_n"]) == (1, 3)


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

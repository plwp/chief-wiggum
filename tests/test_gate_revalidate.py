"""The `revalidate` verb (chief-wiggum#410).

docs/gate-validation.md: bumping scanner_version alone does not revalidate a
gate; the trials must be re-run and the record re-authored. Doing that by hand
three times in one session is what motivated this, and the risk of automating
it is obvious — a tool that re-authors a record whose trials regressed would
launder a broken gate into a green one. So the refusals matter more than the
happy path here.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate_validation_designer as designer  # noqa: E402
import quality_slop_gate as slop_gate  # noqa: E402

VALIDATION_DIR = ROOT / "docs" / "quality" / "validation"
DESIGNER = ROOT / "scripts" / "gate_validation_designer.py"


@pytest.fixture()
def sandbox(tmp_path):
    """A copy of the real validation dir, so tests never write the shipped record."""
    target = tmp_path / "validation"
    (target / "seeds").mkdir(parents=True)
    for name in ("quality_slop_gate.json",):
        (target / name).write_text((VALIDATION_DIR / name).read_text())
    seed_file = VALIDATION_DIR / "seeds" / "quality_slop_gate.seeds.json"
    (target / "seeds" / seed_file.name).write_text(seed_file.read_text())
    return target


class TestHappyPath:
    def test_every_trial_is_replayed_and_passes(self, sandbox):
        result = designer.revalidate("quality_slop_gate", sandbox, write=False)
        assert result["status"] == "passed", result["failures"]
        assert len(result["trials"]) == len(slop_gate.SEED_FIXTURES)
        assert all(trial["passed"] for trial in result["trials"])

    def test_the_record_is_re_authored_with_the_live_scanner_version(self, sandbox):
        """Deliberately stale first: a record that already carried the live
        hash could not tell 'read live' from 'copied forward'."""
        record_path = sandbox / "quality_slop_gate.json"
        stale = json.loads(record_path.read_text())
        stale["scanner_version"] = "stale" + "0" * 59
        record_path.write_text(json.dumps(stale, indent=2))

        designer.revalidate("quality_slop_gate", sandbox)
        record = json.loads(record_path.read_text())
        assert record["scanner_version"] == slop_gate._scanner_version()
        assert record["scanner_version"] != stale["scanner_version"]
        assert record["status"] == "passed"

    def test_check_mode_never_writes(self, sandbox):
        before = (sandbox / "quality_slop_gate.json").read_text()
        result = designer.revalidate("quality_slop_gate", sandbox, write=False)
        assert result["written"] is False
        assert (sandbox / "quality_slop_gate.json").read_text() == before

    def test_coverage_is_derived_from_the_live_run(self, sandbox):
        """A copied coverage block is the 'unexercised no-op wearing a green
        checkmark' the protocol warns about."""
        result = designer.revalidate("quality_slop_gate", sandbox, write=False)
        coverage = result["clean_corpus"]["coverage"]
        assert coverage["signals_evaluated"] == 2
        assert coverage["measured_signals"] == 2
        assert coverage["bands_classified"] == 2

    def test_the_corpus_digest_comes_from_the_checker(self, sandbox):
        """Not an ad-hoc hash — I got that wrong by hand and tests caught it."""
        from check_gate_validation import corpus_digest

        result = designer.revalidate("quality_slop_gate", sandbox, write=False)
        assert result["clean_corpus"]["sha"] == corpus_digest(Path(slop_gate.GV_CORPUS))


class TestRefusals:
    def test_a_failing_trial_refuses_to_write(self, sandbox, monkeypatch):
        """The whole risk of automating this. A regressed gate must not be
        laundered into a green record."""
        before = (sandbox / "quality_slop_gate.json").read_text()
        monkeypatch.setattr(slop_gate, "replay_seeded_trial", lambda seed: "not-fired")
        result = designer.revalidate("quality_slop_gate", sandbox)
        assert result["status"] == "failed"
        assert result["written"] is False
        assert result["failures"]
        assert (sandbox / "quality_slop_gate.json").read_text() == before

    def test_a_replay_that_raises_is_a_failure_not_a_pass(self, sandbox, monkeypatch):
        def explode(seed):
            raise RuntimeError("fixture missing")

        monkeypatch.setattr(slop_gate, "replay_seeded_trial", explode)
        result = designer.revalidate("quality_slop_gate", sandbox, write=False)
        assert result["status"] == "failed"
        assert any("replay-error" in trial["result"] for trial in result["trials"])

    def test_findings_on_the_clean_corpus_refuse_to_write(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            slop_gate, "replay_clean_corpus",
            lambda: {"repo": "x", "findings": 3,
                     "coverage": {"signals_evaluated": 2}, "passed": False},
        )
        result = designer.revalidate("quality_slop_gate", sandbox)
        assert result["status"] == "failed"
        assert result["written"] is False

    def test_all_zero_coverage_is_refused(self, sandbox, monkeypatch):
        """A clean run that exercised nothing is not a passing clean run."""
        monkeypatch.setattr(
            slop_gate, "replay_clean_corpus",
            lambda: {"repo": "x", "findings": 0,
                     "coverage": {"signals_evaluated": 0, "measured_signals": 0},
                     "passed": True},
        )
        result = designer.revalidate("quality_slop_gate", sandbox)
        assert result["status"] == "failed"
        assert any("coverage is all zero" in failure for failure in result["failures"])
        assert result["written"] is False

    def test_a_gate_without_the_replay_protocol_is_refused_by_name(self, sandbox):
        """Scope boundary: a gate needing a live repo or human judgement must be
        refused, not half-automated."""
        (sandbox / "ratchet.json").write_text(
            (VALIDATION_DIR / "ratchet.json").read_text()
        )
        with pytest.raises(designer.NotReplayable, match="not mechanically replayable"):
            designer.revalidate("ratchet", sandbox, write=False)

    def test_an_unknown_gate_is_refused(self, sandbox):
        with pytest.raises(designer.NotReplayable, match="no validation record"):
            designer.revalidate("no_such_gate", sandbox, write=False)


class TestReportOnlyContract:
    """The designer is report-only by contract: exit 0 always."""

    def _run(self, *args):
        return subprocess.run([sys.executable, str(DESIGNER), *args],
                              capture_output=True, text=True)

    def test_a_passing_revalidate_exits_zero(self):
        result = self._run("revalidate", "quality_slop_gate", "--check")
        assert result.returncode == 0
        assert "status: passed" in result.stdout

    def test_a_failing_revalidate_still_exits_zero(self, tmp_path):
        """The invariant that matters: even a FAILED revalidate must not block.
        A passing run returns 0 either way, so it cannot test this."""
        sandbox = tmp_path / "validation"
        (sandbox / "seeds").mkdir(parents=True)
        (sandbox / "quality_slop_gate.json").write_text(
            (VALIDATION_DIR / "quality_slop_gate.json").read_text()
        )
        seeds_path = VALIDATION_DIR / "seeds" / "quality_slop_gate.seeds.json"
        seeds = json.loads(seeds_path.read_text())
        # Invert one expectation so the replay genuinely disagrees with it.
        seeds["seeds"][0]["expected"] = (
            "no-fire" if seeds["seeds"][0]["expected"] == "fire" else "fire"
        )
        (sandbox / "seeds" / seeds_path.name).write_text(json.dumps(seeds, indent=2))

        before = (sandbox / "quality_slop_gate.json").read_text()
        result = self._run("revalidate", "quality_slop_gate",
                           "--validation-dir", str(sandbox))
        assert result.returncode == 0, "report-only: never blocks, even on failure"
        assert "status: failed" in result.stdout
        assert "record NOT written" in result.stdout
        assert (sandbox / "quality_slop_gate.json").read_text() == before

    def test_an_unreplayable_gate_still_exits_zero(self):
        """Blocking is check_gate_validation's job, not this tool's."""
        result = self._run("revalidate", "ratchet", "--check")
        assert result.returncode == 0
        assert "not mechanically replayable" in result.stdout

    def test_json_output_is_marked_report_only(self):
        result = self._run("revalidate", "quality_slop_gate", "--check", "--format", "json")
        assert result.returncode == 0
        assert json.loads(result.stdout)["report_only"] is True

    def test_a_stale_record_still_blocks_via_the_real_checker(self):
        """The safety net: refusing to write leaves the record stale, and the
        BLOCKING checker fails on stale. Automation does not weaken the gate."""
        import check_gate_validation as checker

        report = checker.check("quality_slop_gate", VALIDATION_DIR)
        assert report.record_found and report.passing, report.to_dict()


class TestGateReplayProtocol:
    """The gate side of the protocol, which owns its own fixtures."""

    def test_every_seed_in_the_seeds_file_has_a_fixture(self):
        seeds = json.loads(
            (VALIDATION_DIR / "seeds" / "quality_slop_gate.seeds.json").read_text()
        )["seeds"]
        missing = [s["seed_id"] for s in seeds if s["seed_id"] not in slop_gate.SEED_FIXTURES]
        assert missing == [], f"seeds with no replay fixture: {missing}"

    def test_an_unknown_seed_id_raises_rather_than_guessing(self):
        with pytest.raises(KeyError, match="no fixture mapping"):
            slop_gate.replay_seeded_trial({"seed_id": "slop-invented-99"})

    def test_the_instrument_broken_seed_fires_on_error_not_findings(self):
        """A crashed engine produces NO finding, so counting findings alone
        would record not-fired while the gate is correctly erroring —
        reproducing #289 inside the machinery that certifies against it."""
        assert slop_gate.replay_seeded_trial({"seed_id": "slop-instrument-broken-01"}) == "fired"

    def test_the_integer_key_seed_differs_from_the_string_key_seed(self):
        """The direct/config-indirection pair exists to pin that difference, so
        they must not be fed the same dict."""
        direct = slop_gate.SEED_FIXTURES["slop-direct-01"]
        indirect = slop_gate.SEED_FIXTURES["slop-config-indirection-01"]
        assert direct[0] == indirect[0], "same fixture"
        assert direct[1] != indirect[1], "different key representation"

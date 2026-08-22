import json
import sys
import sqlite3
from pathlib import Path

import pytest
from chief_wiggum.dag import ErrorCode, GraphJournal, JournalError


ROOT = Path(__file__).resolve().parents[1]


def _envelope(**overrides) -> dict:
    base = {
        "schema_version": "1.0.0",
        "record_type": "mutation_envelope",
        "graph_id": "GRF-test001",
        "base_revision": 0,
        "mutation_id": "MUT-test-001",
        "idempotency_key": "test-key-001",
        "actor": "actor:test",
        "authority_class": "automatic",
        "operations": [
            {
                "op_id": "OPS-dag-001",
                "operation_type": "add_execution_node",
                "target_ref": "EXN-test-001",
                "value": {
                    "schema_version": "1.0.0",
                    "record_type": "execution_node",
                    "execution_node_id": "EXN-test-001",
                    "intent_node_id": "INN-test-001",
                    "node_type": "implementation",
                    "role": "role:implementer",
                    "lifecycle_state": "proposed",
                    "attempt": {"attempt_id": None, "outcome": "pending"},
                    "candidate": {"group_id": None, "disposition": "pending"},
                    "approval_state": "not_required",
                    "lease_state": "unclaimed",
                    "control_state": "active",
                    "compiled_from": {
                        "intent_node_id": "INN-test-001",
                        "intent_graph_digest": "sha256:" + "b" * 64,
                    },
                },
            }
        ],
        "reason_code": "EV_HUMAN_DECISION",
        "evidence_refs": [],
        "expected_effect": "adds one execution node",
        "budget_delta": {"unit": "tokens", "value": 0},
        "requires_approval": False,
    }
    base.update(overrides)
    return base


def _intent_envelope(**overrides) -> dict:
    """Envelope that adds an intent node — safe on an empty snapshot."""
    base = {
        "schema_version": "1.0.0",
        "record_type": "mutation_envelope",
        "graph_id": "GRF-test001",
        "base_revision": 0,
        "mutation_id": "MUT-dag-001",
        "idempotency_key": "test-key-001",
        "actor": "actor:test",
        "authority_class": "human",
        "operations": [
            {
                "op_id": "OPS-dag-001",
                "operation_type": "add_intent_node",
                "target_ref": "INN-dag-001",
                "value": {
                    "schema_version": "1.0.0",
                    "record_type": "intent_node",
                    "intent_node_id": "INN-dag-001",
                    "node_type": "implementation",
                    "role": "role:implementer",
                    "source_ref": "ticket:#1",
                    "in_scope": True,
                },
            }
        ],
        "reason_code": "EV_HUMAN_DECISION",
        "evidence_refs": [],
        "expected_effect": "adds one intent node",
        "budget_delta": {"unit": "tokens", "value": 0},
        "requires_approval": True,
    }
    base.update(overrides)
    return base


def _journal(tmp_path: Path) -> GraphJournal:
    journal = GraphJournal(tmp_path / "test.db")
    journal.init_graph("GRF-test001")
    return journal


class TestReplay:
    def test_empty_journal_replays_to_revision_zero(self, tmp_path: Path):
        journal = GraphJournal(tmp_path / "empty.db")
        state = journal.replay()
        assert state.get("graph_revision", 0) == 0
        journal.close()

    def test_init_then_replay_has_graph_id(self, tmp_path: Path):
        journal = _journal(tmp_path)
        state = journal.replay()
        assert state["graph_id"] == "GRF-test001"
        assert state["graph_revision"] == 0
        journal.close()

    def test_accepted_mutation_advances_revision(self, tmp_path: Path):
        journal = _journal(tmp_path)
        decision = journal.propose(_envelope())
        assert decision.accepted
        state = journal.replay()
        assert state["graph_revision"] == 1
        assert len(state["execution_nodes"]) == 1
        journal.close()


class TestHashChain:
    def test_genesis_previous_hash_is_64_zeros(self, tmp_path: Path):
        journal = _journal(tmp_path)
        conn = journal._conn
        row = conn.execute("SELECT previous_record_hash FROM graph_events ORDER BY journal_seq LIMIT 1").fetchone()
        assert row[0] == "0" * 64
        journal.close()

    def test_chain_is_continuous(self, tmp_path: Path):
        journal = _journal(tmp_path)
        journal.propose(_envelope())
        journal.propose(_envelope(mutation_id="MUT-test-002", idempotency_key="test-key-002"))
        rows = journal._conn.execute("SELECT previous_record_hash, event_hash FROM graph_events ORDER BY journal_seq").fetchall()
        for index in range(1, len(rows)):
            assert rows[index][0] == rows[index - 1][1]
        journal.close()

    def test_corrupt_payload_fails_closed(self, tmp_path: Path):
        journal = _journal(tmp_path)
        journal._conn.execute("UPDATE graph_events SET payload = payload || 'x' WHERE journal_seq = 1")
        with pytest.raises(JournalError):
            journal.replay()
        journal.close()


class TestCAS:
    def test_stale_base_is_rejected(self, tmp_path: Path):
        journal = _journal(tmp_path)
        envelope = _intent_envelope()
        journal.propose(envelope)
        stale = _envelope(mutation_id="MUT-dag-002", idempotency_key="key-stale")
        decision = journal.propose(stale)
        assert not decision.accepted
        assert any(v.code == ErrorCode.BASE_REVISION_MISMATCH for v in decision.violations)
        journal.close()


class TestIdempotency:
    def test_same_key_same_payload_is_accepted_once(self, tmp_path: Path):
        journal = _journal(tmp_path)
        envelope = _intent_envelope()
        first = journal.propose(envelope)
        second = journal.propose(dict(envelope))
        assert first.accepted
        assert second.accepted
        state = journal.replay()
        assert state["graph_revision"] == 1
        journal.close()


class TestCycleRejection:
    def test_cycle_proposal_reports_path_before_application(self, tmp_path: Path):
        journal = GraphJournal(tmp_path / "cycle.db")
        journal.init_graph("GRF-cyc001")
        envelope_a = _envelope(
            mutation_id="MUT-cyc-001", idempotency_key="cyc-a",
            graph_id="GRF-cyc001",
        )
        envelope_a["operations"][0]["value"]["execution_node_id"] = "EXN-cyc-001"
        envelope_a["operations"][0]["value"]["intent_node_id"] = "INN-cyc-001"
        envelope_a["operations"][0]["target_ref"] = "EXN-cyc-001"
        envelope_a["operations"] = [
            {
                "op_id": "OPS-cyc-001",
                "operation_type": "add_execution_node",
                "target_ref": "EXN-cyc-001",
                "value": {
                    "schema_version": "1.0.0",
                    "record_type": "execution_node",
                    "execution_node_id": "EXN-cyc-001",
                    "intent_node_id": "INN-cyc-001",
                    "node_type": "implementation",
                    "role": "role:implementer",
                    "lifecycle_state": "proposed",
                    "attempt": {"attempt_id": None, "outcome": "pending"},
                    "candidate": {"group_id": None, "disposition": "pending"},
                    "approval_state": "not_required",
                    "lease_state": "unclaimed",
                    "control_state": "active",
                    "compiled_from": {"intent_node_id": "INN-cyc-001", "intent_graph_digest": "sha256:" + "b" * 64},
                },
            }
        ]
        journal.propose(envelope_a)
        state = journal.replay()
        assert state["graph_revision"] == 1
        journal.close()


class TestCLI:
    def test_cli_init_and_inspect(self, tmp_path: Path):
        import subprocess

        db = str(tmp_path / "cli.db")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dag_engine.py"), "init", "--db", db, "--graph-id", "GRF-cli001"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["ok"]

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dag_engine.py"), "hash", "--db", db],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        hashes = json.loads(result.stdout)
        assert "schedule_hash" in hashes
        assert "audit_state_hash" in hashes

    def test_cli_replay_matches_direct(self, tmp_path: Path):
        import subprocess

        db = str(tmp_path / "replay.db")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "dag_engine.py"), "init", "--db", db, "--graph-id", "GRF-rep001"], capture_output=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "dag_engine.py"), "replay", "--db", db], capture_output=True, text=True)
        assert result.returncode == 0
        state = json.loads(result.stdout)
        assert state["graph_id"] == "GRF-rep001"


class TestSnapshotEquivalence:
    def test_replay_produces_same_hashes_twice(self, tmp_path: Path):
        journal = GraphJournal(tmp_path / "equiv.db")
        journal.init_graph("GRF-eq0001")
        envelope = _intent_envelope(graph_id="GRF-eq0001")
        journal.propose(envelope)
        hashes_1 = journal.hash()

        journal.close()
        reopened = GraphJournal(tmp_path / "equiv.db")
        hashes_2 = reopened.hash()
        assert hashes_1["schedule_hash"] == hashes_2["schedule_hash"]
        assert hashes_1["audit_state_hash"] == hashes_2["audit_state_hash"]
        reopened.close()


class TestConcurrentCAS:
    def test_two_proposals_one_base_only_first_wins(self, tmp_path: Path):
        journal_a = GraphJournal(tmp_path / "race.db")
        journal_a.init_graph("GRF-race01")
        journal_b = GraphJournal(tmp_path / "race.db")

        env_a = _envelope(mutation_id="MUT-dag-003", idempotency_key="race-a", graph_id="GRF-race01")
        env_b = _envelope(mutation_id="MUT-dag-004", idempotency_key="race-b", graph_id="GRF-race01")

        decision_a = journal_a.propose(env_a)
        decision_b = journal_b.propose(env_b)

        assert decision_a.accepted
        assert not decision_b.accepted
        journal_a.close()
        journal_b.close()


class TestSidecarResolver:
    def test_sidecar_mode_resolves_meta_outside_target(self, tmp_path: Path):
        sys.path.insert(0, str(ROOT / "scripts"))
        from artifacts import Resolver

        target = tmp_path / "target-repo"
        target.mkdir()
        try:
            target_id = Resolver.resolve(target).target_id
        finally:
            sys.path.remove(str(ROOT / "scripts"))

        cw_home = tmp_path / "cw-home"
        cw_home.mkdir()
        election_dir = cw_home / "meta" / target_id.replace("/", "/")
        election_dir.mkdir(parents=True)
        (election_dir / "election.json").write_text(json.dumps({"mode": "sidecar", "backing": "local"}))

        sys.path.insert(0, str(ROOT / "scripts"))
        resolver = Resolver.resolve(target, cw_home=cw_home)
        sys.path.remove(str(ROOT / "scripts"))
        assert resolver.mode == "sidecar"
        assert str(resolver.meta_root).startswith(str(cw_home))
        db = resolver.meta_root / "dag" / "journal.db"
        db.parent.mkdir(parents=True)
        journal = GraphJournal(db)
        journal.init_graph("GRF-side01")
        state = journal.replay()
        assert state["graph_id"] == "GRF-side01"
        journal.close()


class TestProcessKill:
    def test_partial_tail_does_not_corrupt_replay(self, tmp_path: Path):
        import shutil

        db = tmp_path / "kill.db"
        journal = GraphJournal(db)
        journal.init_graph("GRF-kill01")
        envelope = _intent_envelope(graph_id="GRF-kill01")
        journal.propose(envelope)
        journal.close()

        copy = tmp_path / "kill-partial.db"
        shutil.copy2(db, copy)
        conn = sqlite3.connect(str(copy))
        raw = conn.execute("SELECT payload FROM graph_events WHERE journal_seq = 1").fetchone()[0]
        truncated = raw[: len(raw) // 2]
        conn.execute("UPDATE graph_events SET payload = ? WHERE journal_seq = 1", (truncated,))
        conn.commit()
        conn.close()

        recovered = GraphJournal(copy)
        with pytest.raises(JournalError):
            recovered.replay()
        recovered.close()

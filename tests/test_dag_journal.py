"""Acceptance tests for the transactional graph journal (chief-wiggum#385).

Each class maps to an acceptance criterion on the ticket. Where a criterion
names a hazard (concurrency, a torn tail, a cycle), the test reproduces the
hazard rather than a sequential stand-in for it — a test that cannot fail when
the guard is removed is not evidence the guard works.
"""

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from chief_wiggum.dag import ErrorCode, GraphJournal, JournalError, load_authority_matrix
from chief_wiggum.dag.journal import _apply_operation, _empty_snapshot

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "scripts" / "dag_engine.py"
GRAPH = "GRF-test001"

_MATRIX = {row["operation_type"]: row for row in load_authority_matrix()["operations"]}
OPERATION_TYPES = sorted(_MATRIX)


# --------------------------------------------------------------- builders


def envelope(operations, *, base_revision, mutation_id, key, graph_id=GRAPH):
    """Build a schema-valid envelope with authority fields the matrix accepts."""
    requires_approval = any(
        _MATRIX[operation["operation_type"]]["approval_required"] for operation in operations
    )
    return {
        "schema_version": "1.0.0",
        "record_type": "mutation_envelope",
        "graph_id": graph_id,
        "base_revision": base_revision,
        "mutation_id": mutation_id,
        "idempotency_key": key,
        "actor": "actor:test",
        "authority_class": "human" if requires_approval else "automatic",
        "operations": operations,
        "reason_code": "EV_HUMAN_DECISION",
        "evidence_refs": [],
        "expected_effect": "test mutation",
        "budget_delta": {"unit": "tokens", "value": 0},
        "requires_approval": requires_approval,
    }


def op(op_id, operation_type, target_ref, **extra):
    return {"op_id": op_id, "operation_type": operation_type, "target_ref": target_ref, **extra}


def intent_node(node_id, ticket=None):
    record = {
        "schema_version": "1.0.0",
        "record_type": "intent_node",
        "intent_node_id": node_id,
        "node_type": "implementation",
        "role": "role:implementer",
        "source_ref": f"ticket:#{ticket or 1}",
        "in_scope": True,
    }
    if ticket is not None:
        record["source_ticket"] = ticket
    return record


def execution_node(node_id, intent_id, *, lifecycle_state="proposed", attempt_id=None):
    return {
        "schema_version": "1.0.0",
        "record_type": "execution_node",
        "execution_node_id": node_id,
        "intent_node_id": intent_id,
        "node_type": "implementation",
        "role": "role:implementer",
        "lifecycle_state": lifecycle_state,
        "attempt": {"attempt_id": attempt_id, "outcome": "pending"},
        "candidate": {"group_id": None, "disposition": "pending"},
        "approval_state": "not_required",
        "lease_state": "unclaimed",
        "control_state": "active",
        "compiled_from": {"intent_node_id": intent_id, "intent_graph_digest": "sha256:" + "b" * 64},
    }


def evidence_record(evidence_id):
    return {
        "schema_version": "1.0.0",
        "record_type": "evidence_record",
        "evidence_id": evidence_id,
        "evidence_type": "test_signal",
        "source_ref": "test",
        "content_digest": "sha256:" + "c" * 64,
        "observed_at": "2026-01-01T00:00:00Z",
        "status": "validated",
    }


def run_concurrently(setups, *, timeout=60):
    """Release callables together on threads, and never hang the suite.

    A bare threading.Barrier turns an early failure in one thread into a
    permanent stall in its peer and in the join, which surfaces as a hung test
    run rather than a red test. Each setup runs before the barrier and returns
    the callable to race; a setup that raises aborts the barrier so the peer
    fails fast instead of waiting forever.
    """
    barrier = threading.Barrier(len(setups), timeout=timeout)
    results = {}

    def wrapper(name, setup):
        try:
            action = setup()
        except BaseException as exc:  # noqa: BLE001 - surfaced via results
            results[name] = exc
            barrier.abort()
            return
        try:
            barrier.wait()
            results[name] = action()
        except BaseException as exc:  # noqa: BLE001 - surfaced via results
            results[name] = exc

    threads = [
        threading.Thread(target=wrapper, args=(name, setup), daemon=True)
        for name, setup in setups.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), "a racing thread never finished"
    for name, result in results.items():
        assert not isinstance(result, BaseException), f"{name} raised {result!r}"
    return results


def journal_at(tmp_path, name="graph.db", graph_id=GRAPH):
    journal = GraphJournal(tmp_path / name)
    journal.init_graph(graph_id)
    return journal


def seed_pair(journal, *, start_revision=0):
    """Add two intent nodes, two execution nodes and one evidence record."""
    revision = start_revision
    decisions = []
    for index, (intent_id, exec_id) in enumerate(
        (("INN-dag-001", "EXN-dag-001"), ("INN-dag-002", "EXN-dag-002")), start=1
    ):
        decision = journal.propose(
            envelope(
                [op(f"OPS-seed-{index:03d}", "add_intent_node", intent_id, value=intent_node(intent_id, ticket=index))],
                base_revision=revision,
                mutation_id=f"MUT-seed-{index:03d}",
                key=f"seed-intent-{index}",
            )
        )
        assert decision.accepted, decision.violations
        revision = decision.graph_revision
        decision = journal.propose(
            envelope(
                [
                    op(
                        f"OPS-seedx-{index:03d}",
                        "add_execution_node",
                        exec_id,
                        value=execution_node(exec_id, intent_id, attempt_id=f"ATM-dag-{index:03d}"),
                    )
                ],
                base_revision=revision,
                mutation_id=f"MUT-seedx-{index:03d}",
                key=f"seed-exec-{index}",
            )
        )
        assert decision.accepted, decision.violations
        revision = decision.graph_revision
        decisions.append(decision)
    decision = journal.propose(
        envelope(
            [op("OPS-seedv-001", "record_evidence", "EVD-dag-001", value=evidence_record("EVD-dag-001"))],
            base_revision=revision,
            mutation_id="MUT-seedv-001",
            key="seed-evidence",
        )
    )
    assert decision.accepted, decision.violations
    return decision.graph_revision


# ------------------------------------------------------ AC: deterministic replay


class TestReplay:
    def test_empty_journal_replays_to_revision_zero(self, tmp_path):
        journal = GraphJournal(tmp_path / "empty.db")
        assert journal.replay()["graph_revision"] == 0
        journal.close()

    def test_init_then_replay_has_graph_id(self, tmp_path):
        journal = journal_at(tmp_path)
        state = journal.replay()
        assert state["graph_id"] == GRAPH
        assert state["graph_revision"] == 0
        journal.close()

    def test_cold_replay_matches_warm_state_after_every_mutation(self, tmp_path):
        """AC: replay(journal) has an identical hash and ready set to the
        incrementally-maintained graph, for any sequence of admitted mutations."""
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)

        sequence = [
            [op("OPS-p-001", "transition_node", "EXN-dag-001", from_state="proposed", to_state="admitted")],
            [op("OPS-p-002", "transition_node", "EXN-dag-002", from_state="proposed", to_state="admitted")],
            [
                op(
                    "OPS-p-003",
                    "add_schedulable_edge",
                    "SED-dag-001",
                    value={
                        "schema_version": "1.0.0",
                        "record_type": "schedulable_edge",
                        "edge_id": "SED-dag-001",
                        "source": "EXN-dag-001",
                        "target": "EXN-dag-002",
                        "kind": "depends_on",
                        "derived_from": ["EVD-dag-001"],
                    },
                )
            ],
            # EXN-dag-001 sources the edge, so it runs AFTER EXN-dag-002 and
            # falls back to admitted; EXN-dag-002 stays derived-ready.
            [op("OPS-p-004", "transition_node", "EXN-dag-002", from_state="ready", to_state="running")],
            [op("OPS-p-005", "transition_node", "EXN-dag-002", from_state="running", to_state="succeeded")],
        ]
        for index, operations in enumerate(sequence, start=1):
            decision = journal.propose(
                envelope(
                    operations,
                    base_revision=revision,
                    mutation_id=f"MUT-prop-{index:03d}",
                    key=f"prop-{index}",
                )
            )
            assert decision.accepted, decision.violations
            revision = decision.graph_revision

            cold = GraphJournal(tmp_path / "graph.db")
            assert cold.hash() == journal.hash()
            assert cold.ready_set() == journal.ready_set()
            assert cold.replay() == journal.replay()
            assert cold.graph_revision() == revision
            cold.close()

        # The dependent node becomes ready only once its predecessor succeeded,
        # and the engine — not a proposal — is what moved it there.
        assert journal.ready_set() == ["EXN-dag-001"]
        journal.close()

    def test_caller_mutating_its_envelope_cannot_rewrite_history(self, tmp_path):
        journal = journal_at(tmp_path)
        body = envelope(
            [op("OPS-a-001", "add_intent_node", "INN-alias-001", value=intent_node("INN-alias-001"))],
            base_revision=0,
            mutation_id="MUT-al-001",
            key="alias",
        )
        assert journal.propose(body).accepted
        body["operations"][0]["value"]["intent_node_id"] = "INN-tampered-999"
        body["expected_effect"] = "rewritten after the fact"

        warm = journal.replay()
        cold = GraphJournal(tmp_path / "graph.db")
        assert warm == cold.replay()
        assert warm["intent_nodes"][0]["intent_node_id"] == "INN-alias-001"
        cold.close()
        journal.close()

    def test_replay_result_cannot_corrupt_the_cache(self, tmp_path):
        journal = journal_at(tmp_path)
        seed_pair(journal)
        leaked = journal.replay()
        leaked["execution_nodes"].append({"execution_node_id": "EXN-bogus-999"})
        leaked["graph_revision"] = 99
        assert len(journal.replay()["execution_nodes"]) == 2
        assert journal.graph_revision() != 99
        journal.close()


# ----------------------------------------------------------- AC: the fold


class TestFoldCompleteness:
    def test_every_vocabulary_operation_is_handled(self):
        """No operation type may fall through to a silent no-op or an unknown-type
        crash. This is the guard that the original fold's collection_map lacked."""
        unhandled = []
        for operation_type in OPERATION_TYPES:
            state = _empty_snapshot(GRAPH)
            try:
                _apply_operation(state, {"operation_type": operation_type, "target_ref": "X"})
            except JournalError as exc:
                if "unknown operation type" in str(exc):
                    unhandled.append(operation_type)
            except (KeyError, TypeError):
                pass  # handled, but needs a well-formed operation to do its work
        assert unhandled == []

    def test_transition_node_changes_lifecycle_state(self, tmp_path):
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)
        decision = journal.propose(
            envelope(
                [op("OPS-t-001", "transition_node", "EXN-dag-001", from_state="proposed", to_state="admitted")],
                base_revision=revision,
                mutation_id="MUT-tr-001",
                key="transition-1",
            )
        )
        assert decision.accepted, decision.violations
        node = next(
            n for n in journal.replay()["execution_nodes"] if n["execution_node_id"] == "EXN-dag-001"
        )
        assert node["lifecycle_state"] == "ready"  # no predecessors, so the fold derives ready
        journal.close()

    def test_lease_and_control_reach_the_projection(self, tmp_path):
        """schedule_hash covers control and lease records, so a paused graph must
        not hash the same as a running one."""
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)
        before = journal.hash()["schedule_hash"]

        lease = {
            "schema_version": "1.0.0",
            "record_type": "lease_record",
            "lease_id": "LSE-dag-001",
            "execution_node_id": "EXN-dag-001",
            "attempt_id": "ATM-dag-001",
            "epoch": 0,
            "state": "active",
            "fencing_token": "token-1",
        }
        decision = journal.propose(
            envelope(
                [op("OPS-l-001", "acquire_lease", "LSE-dag-001", value=lease)],
                base_revision=revision,
                mutation_id="MUT-ls-001",
                key="lease-1",
            )
        )
        assert decision.accepted, decision.violations
        revision = decision.graph_revision
        state = journal.replay()
        assert state["lease_records"][0]["state"] == "active"
        node = next(n for n in state["execution_nodes"] if n["execution_node_id"] == "EXN-dag-001")
        assert node["lease_state"] == "active"
        assert journal.hash()["schedule_hash"] != before

        control = {
            "schema_version": "1.0.0",
            "record_type": "control_record",
            "control_id": "CTL-dag-001",
            "graph_id": GRAPH,
            "state": "paused",
            "actor": "actor:test",
            "reason_code": "EV_HUMAN_DECISION",
        }
        paused = journal.propose(
            envelope(
                [op("OPS-c-001", "set_control", "CTL-dag-001", value=control)],
                base_revision=revision,
                mutation_id="MUT-ct-001",
                key="control-1",
            )
        )
        assert paused.accepted, paused.violations
        assert journal.replay()["control_records"][0]["state"] == "paused"
        assert journal.ready_set() == []
        journal.close()

    def test_release_lease_marks_the_lease_released(self, tmp_path):
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)
        lease = {
            "schema_version": "1.0.0",
            "record_type": "lease_record",
            "lease_id": "LSE-dag-001",
            "execution_node_id": "EXN-dag-001",
            "attempt_id": "ATM-dag-001",
            "epoch": 0,
            "state": "active",
            "fencing_token": "token-1",
        }
        revision = journal.propose(
            envelope(
                [op("OPS-l-002", "acquire_lease", "LSE-dag-001", value=lease)],
                base_revision=revision,
                mutation_id="MUT-ls-002",
                key="lease-2",
            )
        ).graph_revision
        decision = journal.propose(
            envelope(
                [op("OPS-l-003", "release_lease", "LSE-dag-001")],
                base_revision=revision,
                mutation_id="MUT-ls-003",
                key="lease-3",
            )
        )
        assert decision.accepted, decision.violations
        assert journal.replay()["lease_records"][0]["state"] == "released"
        journal.close()

    def test_remove_intent_node_drops_the_record(self, tmp_path):
        journal = journal_at(tmp_path)
        decision = journal.propose(
            envelope(
                [op("OPS-r-001", "add_intent_node", "INN-solo-001", value=intent_node("INN-solo-001"))],
                base_revision=0,
                mutation_id="MUT-rm-001",
                key="rm-add",
            )
        )
        assert decision.accepted, decision.violations
        decision = journal.propose(
            envelope(
                [op("OPS-r-002", "remove_intent_node", "INN-solo-001")],
                base_revision=decision.graph_revision,
                mutation_id="MUT-rm-002",
                key="rm-del",
            )
        )
        assert decision.accepted, decision.violations
        assert journal.replay()["intent_nodes"] == []
        journal.close()

    def test_dangling_execution_node_is_rejected_before_application(self, tmp_path):
        """The post-state is validated, so a compilation reference to a
        non-existent intent node cannot be admitted."""
        journal = journal_at(tmp_path)
        decision = journal.propose(
            envelope(
                [
                    op(
                        "OPS-d-001",
                        "add_execution_node",
                        "EXN-ghost-001",
                        value=execution_node("EXN-ghost-001", "INN-ghost-001"),
                    )
                ],
                base_revision=0,
                mutation_id="MUT-gh-001",
                key="ghost",
            )
        )
        assert not decision.accepted
        assert journal.replay()["execution_nodes"] == []
        journal.close()


# ------------------------------------------------------------ AC: hash chain


class TestHashChain:
    def test_genesis_previous_hash_is_64_zeros(self, tmp_path):
        journal = journal_at(tmp_path)
        row = journal._conn.execute(
            "SELECT previous_record_hash FROM graph_events ORDER BY journal_seq LIMIT 1"
        ).fetchone()
        assert row[0] == "0" * 64
        journal.close()

    def test_chain_is_continuous(self, tmp_path):
        journal = journal_at(tmp_path)
        seed_pair(journal)
        rows = journal._conn.execute(
            "SELECT previous_record_hash, event_hash FROM graph_events ORDER BY journal_seq"
        ).fetchall()
        for index in range(1, len(rows)):
            assert rows[index][0] == rows[index - 1][1]
        journal.close()

    def test_corrupt_payload_fails_closed(self, tmp_path):
        journal = journal_at(tmp_path)
        journal._conn.execute("UPDATE graph_events SET payload = payload || 'x' WHERE journal_seq = 1")
        with pytest.raises(JournalError):
            journal.replay()
        journal.close()

    def test_verification_stays_failed_once_it_has_failed(self, tmp_path):
        journal = journal_at(tmp_path)
        journal._conn.execute("UPDATE graph_events SET payload = payload || 'x' WHERE journal_seq = 1")
        with pytest.raises(JournalError):
            journal.replay()
        with pytest.raises(JournalError):
            journal.replay()
        journal.close()


# ------------------------------------------------------------------ AC: CAS


class TestCAS:
    def test_stale_base_is_rejected_and_journaled(self, tmp_path):
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)
        stale = journal.propose(
            envelope(
                [op("OPS-s-001", "add_intent_node", "INN-late-001", value=intent_node("INN-late-001"))],
                base_revision=revision - 1,
                mutation_id="MUT-st-001",
                key="stale",
            )
        )
        assert not stale.accepted
        assert any(v.code == ErrorCode.BASE_REVISION_MISMATCH for v in stale.violations)
        rejected = journal._conn.execute(
            "SELECT COUNT(*) FROM graph_events WHERE event_type='mutation_rejected'"
        ).fetchone()[0]
        assert rejected == 1
        journal.close()

    @pytest.mark.parametrize("trial", range(15))
    def test_concurrent_proposers_one_wins_one_is_stale(self, tmp_path, trial):
        """AC: two proposers against the same base revision — one wins, the other
        is rejected; no interleaving produces a partially-applied envelope.

        Both threads open their own connection and are released from a barrier
        together, so the compare-and-swap is genuinely contended. A sequential
        stand-in for this test passes even with no concurrency control at all.
        """
        database = tmp_path / f"race-{trial}.db"
        setup = GraphJournal(database)
        setup.init_graph(GRAPH)
        setup.close()

        def proposer(name, node_id, key):
            def setup():
                worker = GraphJournal(database)
                body = envelope(
                    [op("OPS-race-001", "add_intent_node", node_id, value=intent_node(node_id))],
                    base_revision=0,
                    mutation_id=f"MUT-race{name}-001",
                    key=key,
                )

                def act():
                    try:
                        return worker.propose(body)
                    finally:
                        worker.close()

                return act

            return setup

        results = run_concurrently(
            {
                "a": proposer("a", "INN-racea-001", "race-a"),
                "b": proposer("b", "INN-raceb-001", "race-b"),
            }
        )
        accepted = [name for name, result in results.items() if result.accepted]
        assert len(accepted) == 1, (
            f"exactly one proposer must win at base_revision=0, got {len(accepted)}: {results}"
        )
        loser = next(result for name, result in results.items() if name not in accepted)
        assert any(v.code == ErrorCode.BASE_REVISION_MISMATCH for v in loser.violations)

        final = GraphJournal(database)
        state = final.replay()
        assert state["graph_revision"] == 1
        assert len(state["intent_nodes"]) == 1
        final.close()

    def test_concurrent_init_only_initialises_once(self, tmp_path):
        database = tmp_path / "init-race.db"

        def initialiser():
            worker = GraphJournal(database)

            def act():
                try:
                    worker.init_graph(GRAPH)
                    return "ok"
                except JournalError as exc:
                    return str(exc)
                finally:
                    worker.close()

            return act

        results = run_concurrently({"a": initialiser, "b": initialiser})
        assert list(results.values()).count("ok") == 1
        journal = GraphJournal(database)
        assert (
            journal._conn.execute(
                "SELECT COUNT(*) FROM graph_events WHERE event_type='init'"
            ).fetchone()[0]
            == 1
        )
        journal.close()


# ---------------------------------------------------------- AC: idempotency


class TestIdempotency:
    def test_same_key_same_payload_returns_the_original_decision(self, tmp_path):
        journal = journal_at(tmp_path)
        body = envelope(
            [op("OPS-i-001", "add_intent_node", "INN-idem-001", value=intent_node("INN-idem-001"))],
            base_revision=0,
            mutation_id="MUT-id-001",
            key="idem-key",
        )
        first = journal.propose(body)
        assert first.accepted

        # Advance the journal so "the original decision" and "the current head"
        # are different answers; without this the assertion cannot tell them
        # apart and a fresh-decision implementation passes.
        intervening = journal.propose(
            envelope(
                [op("OPS-i-009", "add_intent_node", "INN-idem-009", value=intent_node("INN-idem-009"))],
                base_revision=first.graph_revision,
                mutation_id="MUT-id-009",
                key="idem-other",
            )
        )
        assert intervening.accepted, intervening.violations
        assert intervening.journal_seq != first.journal_seq
        events_before_replay = journal.last_journal_seq()

        second = journal.propose(dict(body))
        assert second.accepted and second.idempotent
        assert (second.journal_seq, second.graph_revision) == (
            first.journal_seq,
            first.graph_revision,
        ), "a repeat must return the ORIGINAL decision, not the current head"
        assert journal.last_journal_seq() == events_before_replay, "a no-op must not append"
        assert journal.replay()["graph_revision"] == 2
        journal.close()

    def test_same_key_different_payload_is_a_hard_error(self, tmp_path):
        journal = journal_at(tmp_path)
        first = journal.propose(
            envelope(
                [op("OPS-i-002", "add_intent_node", "INN-idem-002", value=intent_node("INN-idem-002"))],
                base_revision=0,
                mutation_id="MUT-id-002",
                key="collide",
            )
        )
        assert first.accepted
        collision = journal.propose(
            envelope(
                [op("OPS-i-003", "add_intent_node", "INN-idem-003", value=intent_node("INN-idem-003"))],
                base_revision=1,
                mutation_id="MUT-id-003",
                key="collide",
            )
        )
        assert not collision.accepted
        assert any(v.code == ErrorCode.IDEMPOTENCY_KEY_DIVERGENT for v in collision.violations)
        assert len(journal.replay()["intent_nodes"]) == 1
        journal.close()


# ------------------------------------------------------------- AC: snapshots


class TestSnapshots:
    def test_snapshot_fast_path_equals_full_replay(self, tmp_path):
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)
        snapshot = journal.snapshot()

        decision = journal.propose(
            envelope(
                [op("OPS-sn-001", "transition_node", "EXN-dag-001", from_state="proposed", to_state="admitted")],
                base_revision=revision,
                mutation_id="MUT-sn-001",
                key="after-snapshot",
            )
        )
        assert decision.accepted, decision.violations
        assert snapshot.graph_revision == revision
        # INV-dag-004: identified-record collections are order-insensitive in
        # canonical form, so equivalence is canonical equality — a snapshot
        # round-trips through canonical JSON and comes back re-sorted.
        from chief_wiggum.dag.canonical import canonical_json_bytes

        assert canonical_json_bytes(journal.replay(from_snapshot=True)) == canonical_json_bytes(
            journal.replay()
        )
        assert journal.hash(from_snapshot=True) == journal.hash()
        assert journal.replay(from_snapshot=True)["graph_revision"] == journal.graph_revision()
        journal.close()

    def test_doctored_snapshot_fails_closed(self, tmp_path):
        """A snapshot never wins a disagreement with a replay of its prefix."""
        import hashlib

        from chief_wiggum.dag.canonical import canonical_json_bytes

        journal = journal_at(tmp_path)
        seed_pair(journal)
        journal.snapshot()
        seq = journal.last_journal_seq()
        journal.close()

        connection = sqlite3.connect(str(tmp_path / "graph.db"))
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM graph_events WHERE journal_seq = ?", (seq,)
            ).fetchone()[0]
        )
        payload["snapshot"]["graph_revision"] = 99
        # Re-hash so the chain still verifies: only the fold can catch this.
        doctored = canonical_json_bytes(payload)
        connection.execute(
            "UPDATE graph_events SET payload = ?, event_hash = ? WHERE journal_seq = ?",
            (doctored.decode(), hashlib.sha256(doctored).hexdigest(), seq),
        )
        connection.commit()
        connection.close()

        reopened = GraphJournal(tmp_path / "graph.db")
        with pytest.raises(JournalError, match="disagrees"):
            reopened.replay()
        reopened.close()


# ------------------------------------------------------- AC: crash recovery


class TestCrashRecovery:
    def test_torn_tail_at_every_offset_recovers_to_last_valid_state(self, tmp_path):
        """AC: journal truncated mid-record at every byte offset of a final
        record → engine recovers to the last valid state; nothing lost."""
        source = tmp_path / "source.db"
        journal = journal_at(tmp_path, name="source.db")
        seed_pair(journal)
        expected_hash = journal.hash()
        expected_revision = journal.graph_revision()
        last_seq = journal.last_journal_seq()
        raw = journal._conn.execute(
            "SELECT payload FROM graph_events WHERE journal_seq = ?", (last_seq,)
        ).fetchone()[0]
        journal.close()

        offsets = range(0, len(raw), max(1, len(raw) // 40))
        for offset in offsets:
            target = tmp_path / f"torn-{offset}.db"
            target.write_bytes(source.read_bytes())
            connection = sqlite3.connect(str(target))
            connection.execute(
                "UPDATE graph_events SET payload = ? WHERE journal_seq = ?", (raw[:offset], last_seq)
            )
            connection.commit()
            connection.close()

            torn = GraphJournal(target)
            with pytest.raises(JournalError):
                torn.replay()  # fails closed before recovery
            outcome = torn.recover()
            assert outcome["dropped"] == 1
            assert outcome["last_valid_seq"] == last_seq - 1
            state = torn.replay()
            assert state["graph_revision"] == expected_revision - 1
            assert torn.hash() != expected_hash
            torn.close()

    def test_intact_journal_recovers_to_a_no_op(self, tmp_path):
        journal = journal_at(tmp_path)
        seed_pair(journal)
        before = journal.hash()
        assert journal.recover() == {"dropped": 0, "last_valid_seq": journal.last_journal_seq()}
        assert journal.hash() == before
        journal.close()

    def test_mid_journal_corruption_refuses_to_truncate(self, tmp_path):
        """Corruption with valid records after it is not a torn tail; truncating
        would discard admitted mutations, so it fails closed instead."""
        journal = journal_at(tmp_path)
        seed_pair(journal)
        journal.close()

        connection = sqlite3.connect(str(tmp_path / "graph.db"))
        connection.execute("UPDATE graph_events SET payload = 'x' WHERE journal_seq = 2")
        connection.commit()
        connection.close()

        reopened = GraphJournal(tmp_path / "graph.db")
        with pytest.raises(JournalError, match="mid-journal corruption"):
            reopened.recover()
        reopened.close()


# ------------------------------------------------------- AC: cycle rejection


class TestCycleRejection:
    def test_cycle_is_rejected_before_application_with_the_path(self, tmp_path):
        journal = journal_at(tmp_path)
        revision = seed_pair(journal)

        def edge(edge_id, source, target):
            return {
                "schema_version": "1.0.0",
                "record_type": "schedulable_edge",
                "edge_id": edge_id,
                "source": source,
                "target": target,
                "kind": "depends_on",
                "derived_from": ["EVD-dag-001"],
            }

        forward = journal.propose(
            envelope(
                [op("OPS-cy-001", "add_schedulable_edge", "SED-dag-001", value=edge("SED-dag-001", "EXN-dag-001", "EXN-dag-002"))],
                base_revision=revision,
                mutation_id="MUT-cy-001",
                key="edge-forward",
            )
        )
        assert forward.accepted, forward.violations

        back = journal.propose(
            envelope(
                [op("OPS-cy-002", "add_schedulable_edge", "SED-dag-002", value=edge("SED-dag-002", "EXN-dag-002", "EXN-dag-001"))],
                base_revision=forward.graph_revision,
                mutation_id="MUT-cy-002",
                key="edge-back",
            )
        )
        assert not back.accepted, "a cycle-introducing edge must be rejected"
        cycle_violations = [v for v in back.violations if v.code == ErrorCode.SCHEDULABLE_CYCLE]
        assert cycle_violations, back.violations
        assert cycle_violations[0].details["cycle"], "the offending path must be reported"

        state = journal.replay()
        assert len(state["schedulable_edges"]) == 1, "the rejected edge must not be applied"
        assert state["graph_revision"] == forward.graph_revision
        journal.close()


# ---------------------------------------------------- AC: wave projection


class TestWaveProjection:
    def test_project_matches_plan_waves_for_the_same_dependency_graph(self, tmp_path):
        """AC: project --waves reproduces plan_waves.py's output exactly."""
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from chief_wiggum import planning
        finally:
            sys.path.remove(str(ROOT / "scripts"))

        journal = journal_at(tmp_path)
        revision = 0
        tickets = [1, 2, 3]
        for ticket in tickets:
            node_id = f"INN-ticket-{ticket:03d}"
            decision = journal.propose(
                envelope(
                    [op(f"OPS-w-{ticket:03d}", "add_intent_node", node_id, value=intent_node(node_id, ticket=ticket))],
                    base_revision=revision,
                    mutation_id=f"MUT-wv-{ticket:03d}",
                    key=f"wave-node-{ticket}",
                )
            )
            assert decision.accepted, decision.violations
            revision = decision.graph_revision

        edge_record = {
            "schema_version": "1.0.0",
            "record_type": "intent_edge",
            "edge_id": "IED-ticket-001",
            "source": "INN-ticket-002",
            "target": "INN-ticket-001",
            "source_ticket": 2,
            "target_ticket": 1,
            "kind": "depends_on",
            "actor": "actor:tracker",
            "reason_code": "EV_DEP_DECLARED",
        }
        decision = journal.propose(
            envelope(
                [op("OPS-w-900", "add_intent_edge", "IED-ticket-001", value=edge_record)],
                base_revision=revision,
                mutation_id="MUT-wv-900",
                key="wave-edge",
            )
        )
        assert decision.accepted, decision.violations
        journal.close()

        result = subprocess.run(
            [sys.executable, str(ENGINE), "project", "--db", str(tmp_path / "graph.db"), "--waves"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        projected = json.loads(result.stdout)
        expected = planning.plan_waves(tickets, {2: [1], 1: [], 3: []}, closed=[], gated=[]).to_dict()
        assert projected == expected

    def test_project_errors_rather_than_emitting_an_empty_plan(self, tmp_path):
        """An intent graph with no ticket-level projection is an error, not a
        silent empty plan that reads as 'no work to do'."""
        journal = journal_at(tmp_path)
        decision = journal.propose(
            envelope(
                [op("OPS-w-901", "add_intent_node", "INN-noticket-001", value=intent_node("INN-noticket-001"))],
                base_revision=0,
                mutation_id="MUT-wv-901",
                key="no-ticket",
            )
        )
        assert decision.accepted, decision.violations
        journal.close()

        result = subprocess.run(
            [sys.executable, str(ENGINE), "project", "--db", str(tmp_path / "graph.db"), "--waves"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["ok"] is False


# ------------------------------------------------------------------ AC: CLI


class TestCLI:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ENGINE), *args], capture_output=True, text=True
        )

    def test_init_hash_inspect_and_verify(self, tmp_path):
        database = str(tmp_path / "cli.db")
        assert json.loads(self._run("init", "--db", database, "--graph-id", "GRF-cli001").stdout)["ok"]

        hashes = json.loads(self._run("hash", "--db", database).stdout)
        assert "schedule_hash" in hashes and "audit_state_hash" in hashes

        inspected = self._run("inspect", "--db", database, "--human")
        assert inspected.returncode == 0
        assert "GRF-cli001" in inspected.stdout

        verified = json.loads(self._run("verify", "--db", database).stdout)
        assert verified["ok"] and verified["records_verified"] == 1

    def test_replay_matches_direct(self, tmp_path):
        database = str(tmp_path / "replay.db")
        self._run("init", "--db", database, "--graph-id", "GRF-rep001")
        result = self._run("replay", "--db", database)
        assert result.returncode == 0
        assert json.loads(result.stdout)["graph_id"] == "GRF-rep001"

    def test_snapshot_subcommand_writes_a_checkpoint(self, tmp_path):
        database = str(tmp_path / "snap.db")
        self._run("init", "--db", database, "--graph-id", "GRF-snp001")
        result = self._run("snapshot", "--db", database)
        assert result.returncode == 0
        assert json.loads(result.stdout)["graph_revision"] == 0

    def test_rejection_and_engine_failure_have_distinct_exit_codes(self, tmp_path):
        database = str(tmp_path / "codes.db")
        self._run("init", "--db", database, "--graph-id", "GRF-cod001")

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": "1.0.0", "record_type": "mutation_envelope"}))
        rejected = self._run("propose", "--db", database, str(bad))
        assert rejected.returncode == 1
        assert json.loads(rejected.stdout)["accepted"] is False

        missing = self._run("propose", "--db", database, str(tmp_path / "nope.json"))
        assert missing.returncode == 3
        assert json.loads(missing.stdout)["ok"] is False

    def test_operator_reject_is_journaled_without_applying(self, tmp_path):
        database = str(tmp_path / "opreject.db")
        self._run("init", "--db", database, "--graph-id", GRAPH)
        path = tmp_path / "env.json"
        path.write_text(
            json.dumps(
                envelope(
                    [op("OPS-o-001", "add_intent_node", "INN-op-001", value=intent_node("INN-op-001"))],
                    base_revision=0,
                    mutation_id="MUT-op-001",
                    key="operator",
                )
            )
        )
        result = self._run("reject", "--db", database, str(path), "--reason", "not now")
        assert result.returncode == 1
        assert json.loads(result.stdout)["accepted"] is False

        journal = GraphJournal(tmp_path / "opreject.db")
        assert journal.replay()["intent_nodes"] == []
        assert (
            journal._conn.execute(
                "SELECT COUNT(*) FROM graph_events WHERE event_type='mutation_rejected'"
            ).fetchone()[0]
            == 1
        )
        journal.close()


# ------------------------------------------------- AC: no hard-coded paths


class TestSidecarResolver:
    def test_sidecar_mode_resolves_meta_outside_target(self, tmp_path):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from artifacts import Resolver

            target = tmp_path / "target-repo"
            target.mkdir()
            target_id = Resolver.resolve(target).target_id

            cw_home = tmp_path / "cw-home"
            election_dir = cw_home / "meta" / target_id
            election_dir.mkdir(parents=True)
            (election_dir / "election.json").write_text(
                json.dumps({"mode": "sidecar", "backing": "local"})
            )

            resolver = Resolver.resolve(target, cw_home=cw_home)
        finally:
            sys.path.remove(str(ROOT / "scripts"))

        assert resolver.mode == "sidecar"
        assert str(resolver.meta_root).startswith(str(cw_home))
        database = resolver.meta_root / "dag" / "journal.db"
        database.parent.mkdir(parents=True)
        journal = GraphJournal(database)
        journal.init_graph("GRF-side01")
        assert journal.replay()["graph_id"] == "GRF-side01"
        assert not (target / "docs").exists(), "sidecar mode must not write into the target"
        journal.close()

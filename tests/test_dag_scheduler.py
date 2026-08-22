"""Continuous ready-set scheduler (chief-wiggum#387).

The scenario tests are the point. Each one reproduces a situation the wave
barrier handled badly, and asserts the specific thing the barrier could not do:
unrelated work keeps running.
"""

import random

import pytest
from chief_wiggum.dag import GraphJournal
from chief_wiggum.dag.admission import LivenessFailure
from chief_wiggum.dag.scheduler import (
    ClaimRefusal,
    ReadyIndex,
    Scheduler,
    SchedulerPolicy,
    SchedulerStore,
    critical_path_lengths,
    full_ready_set,
    priority_key,
)

GRAPH = "GRF-sch001"


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def envelope(operations, *, base_revision, mutation_id, key, authority_class=None):
    from chief_wiggum.dag.schemas import load_authority_matrix

    matrix = {row["operation_type"]: row for row in load_authority_matrix()["operations"]}
    needs = any(matrix.get(op["operation_type"], {}).get("approval_required") for op in operations)
    return {
        "schema_version": "1.0.0",
        "record_type": "mutation_envelope",
        "graph_id": GRAPH,
        "base_revision": base_revision,
        "mutation_id": mutation_id,
        "idempotency_key": key,
        "actor": "actor:test",
        "authority_class": authority_class or ("human" if needs else "automatic"),
        "operations": operations,
        "reason_code": "EV_HUMAN_DECISION",
        "evidence_refs": [],
        "expected_effect": "scheduler fixture",
        "budget_delta": {"unit": "tokens", "value": 0},
        "requires_approval": needs,
    }


def intent_node(node_id):
    return {
        "schema_version": "1.0.0",
        "record_type": "intent_node",
        "intent_node_id": node_id,
        "node_type": "implementation",
        "role": "role:implementer",
        "source_ref": "ticket:#1",
        "in_scope": True,
    }


def execution_node(node_id, intent_id):
    return {
        "schema_version": "1.0.0",
        "record_type": "execution_node",
        "execution_node_id": node_id,
        "intent_node_id": intent_id,
        "node_type": "implementation",
        "role": "role:implementer",
        "lifecycle_state": "proposed",
        "attempt": {"attempt_id": None, "outcome": "pending"},
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


class Graph:
    """Small fixture builder that keeps base_revision bookkeeping out of tests."""

    def __init__(self, tmp_path, name="graph.db"):
        self.journal = GraphJournal(tmp_path / name)
        self.journal.init_graph(GRAPH)
        self.revision = 0
        self.counter = 0

    def _apply(self, operations, authority_class=None):
        self.counter += 1
        decision = self.journal.propose(
            envelope(
                operations,
                base_revision=self.revision,
                mutation_id=f"MUT-sch-{self.counter:03d}",
                key=f"sch-{self.counter}",
                authority_class=authority_class,
            )
        )
        assert decision.accepted, decision.violations
        self.revision = decision.graph_revision
        return decision

    def add_node(self, letter, index):
        intent_id = f"INN-{letter}-{index:03d}"
        exec_id = f"EXN-{letter}-{index:03d}"
        self._apply(
            [{"op_id": f"OPS-i-{self.counter + 1:03d}", "operation_type": "add_intent_node",
              "target_ref": intent_id, "value": intent_node(intent_id)}],
            authority_class="human",
        )
        self._apply(
            [{"op_id": f"OPS-x-{self.counter + 1:03d}", "operation_type": "add_execution_node",
              "target_ref": exec_id, "value": execution_node(exec_id, intent_id)}]
        )
        return exec_id

    def add_evidence(self, evidence_id):
        self._apply(
            [{"op_id": f"OPS-e-{self.counter + 1:03d}", "operation_type": "record_evidence",
              "target_ref": evidence_id, "value": evidence_record(evidence_id)}]
        )
        return evidence_id

    def edge(self, source, target, evidence_id, edge_id):
        """source runs AFTER target."""
        self._apply(
            [{"op_id": f"OPS-d-{self.counter + 1:03d}", "operation_type": "add_schedulable_edge",
              "target_ref": edge_id,
              "value": {"schema_version": "1.0.0", "record_type": "schedulable_edge",
                        "edge_id": edge_id, "source": source, "target": target,
                        "kind": "depends_on", "derived_from": [evidence_id]}}]
        )

    def transition(self, node_id, before, after):
        self._apply(
            [{"op_id": f"OPS-t-{self.counter + 1:03d}", "operation_type": "transition_node",
              "target_ref": node_id, "from_state": before, "to_state": after}]
        )

    def admit(self, node_id):
        self.transition(node_id, "proposed", "admitted")

    def succeed(self, node_id):
        state = self.journal.replay()
        current = next(
            n["lifecycle_state"] for n in state["execution_nodes"]
            if n["execution_node_id"] == node_id
        )
        if current != "running":
            self.transition(node_id, current, "running")
        self.transition(node_id, "running", "succeeded")

    def close(self):
        self.journal.close()


def scheduler_for(graph, tmp_path, policy=None, clock=None, shadow=False):
    store = SchedulerStore(tmp_path / "graph.db")
    return Scheduler(graph.journal, store, policy or SchedulerPolicy(),
                     clock=clock or FakeClock(), shadow=shadow), store


# ------------------------------------------------------------- ready set


class TestReadySet:
    def test_incremental_matches_full_recomputation_randomised(self, tmp_path):
        """AC: incremental ready set equals full recomputation for every state."""
        rng = random.Random(20260822)
        graph = Graph(tmp_path)
        nodes = [graph.add_node("n", index) for index in range(1, 7)]
        evidence = graph.add_evidence("EVD-sched-000000000000001")
        # A random DAG, kept acyclic by only linking later nodes to earlier ones.
        edge_count = 0
        for later in range(1, len(nodes)):
            for earlier in range(later):
                if rng.random() < 0.35:
                    edge_count += 1
                    graph.edge(nodes[later], nodes[earlier], evidence,
                               f"SED-rnd-{edge_count:03d}")
        for node in nodes:
            graph.admit(node)

        index = ReadyIndex(graph.journal.replay())
        assert index.ready() == full_ready_set(graph.journal.replay())

        for _ in range(25):
            state = graph.journal.replay()
            runnable = [
                n["execution_node_id"] for n in state["execution_nodes"]
                if n["lifecycle_state"] in ("ready", "admitted")
            ]
            if not runnable:
                break
            chosen = rng.choice(sorted(runnable))
            if chosen not in full_ready_set(state):
                # Not startable yet; nothing to do this round.
                continue
            graph.succeed(chosen)
            new_state = graph.journal.replay()
            index.apply(new_state, [chosen])
            assert index.ready() == full_ready_set(new_state), (
                f"incremental diverged after {chosen}"
            )
        graph.close()

    def test_scheduler_readiness_agrees_with_the_engine(self, tmp_path):
        """Two independent derivations of the same property must agree."""
        graph = Graph(tmp_path)
        first = graph.add_node("a", 1)
        second = graph.add_node("b", 1)
        evidence = graph.add_evidence("EVD-agree-000000000000001")
        graph.edge(second, first, evidence, "SED-agree-001")
        graph.admit(first)
        graph.admit(second)
        state = graph.journal.replay()
        assert full_ready_set(state) == graph.journal.ready_set() == [first]
        graph.succeed(first)
        state = graph.journal.replay()
        assert full_ready_set(state) == graph.journal.ready_set() == [second]
        graph.close()

    def test_node_control_state_gates_readiness(self, tmp_path):
        """Per-node control is distinct from a graph-wide pause and gates alone."""
        graph = Graph(tmp_path)
        open_node = graph.add_node("ctl", 1)
        intent_id = "INN-ctl-002"
        halted = "EXN-ctl-002"
        graph._apply(
            [{"op_id": "OPS-ctl-010", "operation_type": "add_intent_node",
              "target_ref": intent_id, "value": intent_node(intent_id)}],
            authority_class="human",
        )
        graph._apply(
            [{"op_id": "OPS-ctl-011", "operation_type": "add_execution_node",
              "target_ref": halted,
              "value": dict(execution_node(halted, intent_id), control_state="cancelled")}]
        )
        graph.admit(open_node)
        graph.transition(halted, "proposed", "admitted")
        state = graph.journal.replay()
        assert full_ready_set(state) == [open_node], "a non-active node must not be ready"
        index = ReadyIndex(state)
        assert index.ready() == [open_node]
        graph.close()

    def test_pausing_the_graph_empties_the_ready_set(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("p", 1)
        graph.admit(node)
        index = ReadyIndex(graph.journal.replay())
        assert index.ready() == [node]
        graph._apply(
            [{"op_id": "OPS-c-900", "operation_type": "set_control", "target_ref": "CTL-p-001",
              "value": {"schema_version": "1.0.0", "record_type": "control_record",
                        "control_id": "CTL-p-001", "graph_id": GRAPH, "state": "paused",
                        "actor": "actor:test", "reason_code": "EV_HUMAN_DECISION"}}],
            authority_class="human",
        )
        state = graph.journal.replay()
        index.apply(state, [node])
        assert index.ready() == [] == full_ready_set(state)
        graph.close()


# ------------------------------------------------------------- priority


class TestPriority:
    def test_tie_break_chain_ends_at_the_node_id(self, tmp_path):
        graph = Graph(tmp_path)
        first = graph.add_node("z", 1)
        second = graph.add_node("a", 1)
        graph.admit(first)
        graph.admit(second)
        state = graph.journal.replay()
        policy = SchedulerPolicy()
        ordered = sorted(full_ready_set(state),
                         key=lambda n: priority_key(n, state, policy))
        # Same declared priority, critical path, risk. Age decides, then id.
        assert ordered == [first, second]
        assert priority_key(first, state, policy)[-1] == first
        graph.close()

    def test_declared_priority_outranks_everything_below_it(self, tmp_path):
        graph = Graph(tmp_path)
        first = graph.add_node("z", 1)
        second = graph.add_node("a", 1)
        graph.admit(first)
        graph.admit(second)
        state = graph.journal.replay()
        policy = SchedulerPolicy(declared_priority={second: 10})
        ordered = sorted(full_ready_set(state), key=lambda n: priority_key(n, state, policy))
        assert ordered[0] == second
        graph.close()

    def test_critical_path_prefers_the_longer_chain(self, tmp_path):
        graph = Graph(tmp_path)
        # `lonely` is added FIRST so it wins on age. Only critical-path length
        # can put `head` in front, which is what this test is for.
        lonely = graph.add_node("l", 1)
        head = graph.add_node("h", 1)
        middle = graph.add_node("m", 1)
        tail = graph.add_node("t", 1)
        evidence = graph.add_evidence("EVD-cp-000000000000001")
        graph.edge(middle, head, evidence, "SED-cp-001")
        graph.edge(tail, middle, evidence, "SED-cp-002")
        for node in (head, middle, tail, lonely):
            graph.admit(node)
        state = graph.journal.replay()
        lengths = critical_path_lengths(state)
        assert lengths[head] == 2
        assert lengths[lonely] == 0
        ordered = sorted(full_ready_set(state),
                         key=lambda n: priority_key(n, state, SchedulerPolicy(),
                                                    critical_path=lengths))
        assert ordered[0] == head
        graph.close()


# ------------------------------------------------------ required scenarios


class TestScenarios:
    def test_failed_gate_blocks_only_dependants_not_siblings(self, tmp_path):
        """AC: a gate failure blocks only that node's dependants, provably not siblings."""
        graph = Graph(tmp_path)
        failing = graph.add_node("f", 1)
        dependant = graph.add_node("d", 1)
        sibling_one = graph.add_node("s", 1)
        sibling_two = graph.add_node("s", 2)
        evidence = graph.add_evidence("EVD-gate-000000000000001")
        graph.edge(dependant, failing, evidence, "SED-gate-001")
        for node in (failing, dependant, sibling_one, sibling_two):
            graph.admit(node)

        scheduler, store = scheduler_for(graph, tmp_path)
        assert set(scheduler.refresh(None)) == {failing, sibling_one, sibling_two}

        graph.transition(failing, "ready", "running")
        graph.transition(failing, "running", "blocked")
        ready = set(scheduler.refresh(None))
        assert dependant not in ready, "the dependant must be held"
        assert {sibling_one, sibling_two} <= ready, "siblings must keep running"
        store.close()
        graph.close()

    def test_discovery_mid_run_blocks_only_the_affected_node(self, tmp_path):
        """AC: a discovered dependency blocks its node; three others run on."""
        graph = Graph(tmp_path)
        affected = graph.add_node("af", 1)
        blocker = graph.add_node("bl", 1)
        others = [graph.add_node("ot", index) for index in range(1, 4)]
        for node in [affected, blocker, *others]:
            graph.admit(node)
        scheduler, store = scheduler_for(graph, tmp_path)
        before = set(scheduler.refresh(None))
        assert set(others) <= before

        evidence = graph.add_evidence("EVD-disc-000000000000001")
        graph.edge(affected, blocker, evidence, "SED-disc-001")

        after = set(scheduler.refresh(None))
        assert affected not in after, "the newly dependent node must block"
        assert set(others) <= after, "unrelated work must be untouched"
        store.close()
        graph.close()

    def test_no_progress_raises_an_explicit_liveness_failure(self, tmp_path):
        """AC: it does not spin, and it does not exit 0."""
        graph = Graph(tmp_path)
        stuck = graph.add_node("st", 1)
        graph.admit(stuck)
        graph.transition(stuck, "ready", "blocked")
        scheduler, store = scheduler_for(graph, tmp_path)
        scheduler.refresh(None)
        report = scheduler.liveness()
        assert report.stalled
        with pytest.raises(LivenessFailure, match=stuck):
            scheduler.assert_progress()
        store.close()
        graph.close()

    def test_finished_graph_is_not_a_liveness_failure(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("done", 1)
        graph.admit(node)
        graph.succeed(node)
        scheduler, store = scheduler_for(graph, tmp_path)
        scheduler.refresh(None)
        assert not scheduler.liveness().stalled
        scheduler.assert_progress()
        store.close()
        graph.close()

    def test_replay_determinism_of_dispatch_order(self, tmp_path):
        """AC: replaying the same journal produces the same dispatch order."""
        def run(directory):
            directory.mkdir()
            graph = Graph(directory)
            nodes = [graph.add_node("r", index) for index in range(1, 5)]
            for node in nodes:
                graph.admit(node)
            scheduler, store = scheduler_for(graph, directory)
            scheduler.refresh(None)
            order = scheduler.dispatchable()
            revision = graph.revision
            for node in order:
                scheduler.claim(node, worker="actor:worker", base_revision=revision)
            dispatched = store.dispatch_order(GRAPH)
            hashes = graph.journal.hash()
            store.close()
            graph.close()
            return order, dispatched, hashes

        first = run(tmp_path / "one")
        second = run(tmp_path / "two")
        assert first[0] == second[0]
        assert first[1] == second[1]
        assert first[2] == second[2]


# ---------------------------------------------------------------- leases


class TestLeasesAndClaims:
    def test_stale_revision_refuses_the_claim(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("cl", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(graph, tmp_path)
        scheduler.refresh(None)
        stale = scheduler.claim(node, worker="actor:worker", base_revision=graph.revision - 1)
        assert not stale.granted
        assert stale.refusal is ClaimRefusal.STALE_REVISION
        fresh = scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        assert fresh.granted
        store.close()
        graph.close()

    def test_claiming_a_node_that_is_not_ready_is_refused(self, tmp_path):
        graph = Graph(tmp_path)
        gated = graph.add_node("nr", 1)
        blocker = graph.add_node("nr", 2)
        evidence = graph.add_evidence("EVD-notready-000000000000001")
        graph.edge(gated, blocker, evidence, "SED-nr-001")
        graph.admit(gated)
        graph.admit(blocker)
        scheduler, store = scheduler_for(graph, tmp_path)
        scheduler.refresh(None)
        refused = scheduler.claim(gated, worker="actor:worker", base_revision=graph.revision)
        assert not refused.granted
        assert refused.refusal is ClaimRefusal.NOT_READY
        assert store.active(GRAPH) == [], "a refused claim must not take a lease"
        store.close()
        graph.close()

    def test_a_node_cannot_be_claimed_twice(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("cl", 2)
        graph.admit(node)
        scheduler, store = scheduler_for(graph, tmp_path)
        scheduler.refresh(None)
        assert scheduler.claim(node, worker="actor:one", base_revision=graph.revision).granted
        second = scheduler.claim(node, worker="actor:two", base_revision=graph.revision)
        assert not second.granted
        assert second.refusal is ClaimRefusal.ALREADY_LEASED
        store.close()
        graph.close()

    def test_expired_lease_cannot_commit_afterwards(self, tmp_path):
        """AC: a worker whose lease expired cannot commit its claim afterwards."""
        clock = FakeClock()
        graph = Graph(tmp_path)
        node = graph.add_node("lease", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(
            graph, tmp_path, SchedulerPolicy(lease_seconds=60), clock
        )
        scheduler.refresh(None)
        claim = scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        assert scheduler.commit_allowed(claim.lease_id)

        clock.advance(120)
        assert not scheduler.commit_allowed(claim.lease_id)
        expired = scheduler.expire_leases()
        assert [entry["lease_id"] for entry in expired] == [claim.lease_id]
        assert not scheduler.commit_allowed(claim.lease_id)
        store.close()
        graph.close()

    def test_heartbeat_extends_the_lease(self, tmp_path):
        clock = FakeClock()
        graph = Graph(tmp_path)
        node = graph.add_node("hb", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(
            graph, tmp_path, SchedulerPolicy(lease_seconds=60), clock
        )
        scheduler.refresh(None)
        claim = scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        clock.advance(45)
        assert scheduler.heartbeat(claim.lease_id, progress={"commits": 2})
        clock.advance(45)
        assert scheduler.commit_allowed(claim.lease_id), "a heartbeat must extend the deadline"
        assert store.lease(claim.lease_id)["progress"] == {"commits": 2}
        store.close()
        graph.close()

    def test_expiry_is_evidence_not_a_silent_takeover(self, tmp_path):
        clock = FakeClock()
        graph = Graph(tmp_path)
        node = graph.add_node("ev", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(
            graph, tmp_path, SchedulerPolicy(lease_seconds=30), clock
        )
        scheduler.refresh(None)
        scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        clock.advance(60)
        expired = scheduler.expire_leases()
        assert expired, "expiry must surface, not be swallowed"
        assert expired[0]["execution_node_id"] == node
        # The node is not silently handed to someone else; it must be re-claimed.
        assert store.active_lease_for(GRAPH, node) is None
        store.close()
        graph.close()

    def test_retry_budget_is_bounded_per_node_and_per_graph(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("rt", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(
            graph, tmp_path, SchedulerPolicy(max_retries_per_node=2, max_retries_per_graph=3)
        )
        assert scheduler.register_retry(node, "EXN-rt-101")
        assert scheduler.register_retry(node, "EXN-rt-102")
        assert not scheduler.register_retry(node, "EXN-rt-103"), "per-node budget must bound"
        other = graph.add_node("rt", 2)
        assert scheduler.register_retry(other, "EXN-rt-201")
        assert not scheduler.register_retry(other, "EXN-rt-202"), "per-graph budget must bound"
        store.close()
        graph.close()


# ------------------------------------------------------ concurrency + resume


class TestDispatchAndResume:
    def test_concurrency_cap_is_respected(self, tmp_path):
        graph = Graph(tmp_path)
        nodes = [graph.add_node("cc", index) for index in range(1, 6)]
        for node in nodes:
            graph.admit(node)
        scheduler, store = scheduler_for(graph, tmp_path, SchedulerPolicy(max_concurrent=2))
        scheduler.refresh(None)
        batch = scheduler.dispatchable()
        assert len(batch) == 2
        for node in batch:
            scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        assert scheduler.dispatchable() == [], "the cap must hold while leases are active"
        store.close()
        graph.close()

    def test_resume_reconciles_in_flight_leases(self, tmp_path):
        """AC: an orchestrator killed mid-dispatch resumes with no node running twice."""
        clock = FakeClock()
        graph = Graph(tmp_path)
        alive = graph.add_node("rs", 1)
        dead = graph.add_node("rs", 2)
        for node in (alive, dead):
            graph.admit(node)
        scheduler, store = scheduler_for(
            graph, tmp_path, SchedulerPolicy(lease_seconds=100, max_concurrent=4), clock
        )
        scheduler.refresh(None)
        alive_claim = scheduler.claim(alive, worker="actor:a", base_revision=graph.revision)
        scheduler.claim(dead, worker="actor:b", base_revision=graph.revision)
        clock.advance(50)
        scheduler.heartbeat(alive_claim.lease_id, progress={"commits": 1})
        clock.advance(80)  # the un-heartbeaten lease is now past its deadline

        # Orchestrator restarts against the same journal and store.
        resumed = Scheduler(graph.journal, store, SchedulerPolicy(lease_seconds=100),
                            clock=clock)
        outcome = resumed.resume()
        assert dead not in outcome["still_running"], "an expired lease must not resume as running"
        assert alive in outcome["still_running"], "a live lease must survive the restart"
        assert len(outcome["expired_leases"]) == 1
        assert len(store.active(GRAPH)) == 1, "no node may be running twice"
        store.close()
        graph.close()

    def test_shadow_mode_decides_without_dispatching(self, tmp_path):
        graph = Graph(tmp_path)
        node = graph.add_node("sh", 1)
        graph.admit(node)
        scheduler, store = scheduler_for(graph, tmp_path, shadow=True)
        scheduler.refresh(None)
        claim = scheduler.claim(node, worker="actor:worker", base_revision=graph.revision)
        assert claim.granted, "shadow mode still computes the decision"
        assert store.active(GRAPH) == [], "shadow mode must not take a lease"
        assert store.dispatch_order(GRAPH) == [], "shadow mode must not dispatch"
        store.close()
        graph.close()


# ------------------------------------------------------------- fallback


class TestStaticFallback:
    def test_static_projection_shares_the_same_graph(self, tmp_path):
        """AC: --static shares the graph, so the fallback is exercised, not aspirational."""
        from chief_wiggum.dag import compatibility

        graph = Graph(tmp_path)
        for index in (1, 2):
            intent_id = f"INN-ticket-{index:03d}"
            graph._apply(
                [{"op_id": f"OPS-s-{index:03d}", "operation_type": "add_intent_node",
                  "target_ref": intent_id,
                  "value": dict(intent_node(intent_id), source_ticket=index)}],
                authority_class="human",
            )
        state = graph.journal.replay()
        result = compatibility.project_legacy_waves(
            {
                "schema_version": "1.0.0",
                "record_type": "intent_graph",
                "graph_id": GRAPH,
                "source_ref": "journal",
                "source_digest": "sha256:" + "0" * 64,
                "scan_status": "observed",
                "has_dependency_block": True,
                "source_warnings": [],
                "nodes": state["intent_nodes"],
                "edges": state["intent_edges"],
            }
        )
        assert result.exit_code == 0, result.error
        assert result.plan["waves"], "the static fallback must produce a real plan"
        graph.close()

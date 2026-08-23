"""The per-proposal hashing cost has an upper bound (chief-wiggum#397).

#396 cut a 300-proposal run from 35.3s to 1.13s — about 31x — and nothing
protected that win. The remaining shape is quadratic: `propose()` canonicalises
the whole graph on every accept, through `_compute_schedule_hash` and
`_compute_audit_state_hash`, so N proposals cost O(N^2).

Fixing the SHAPE needs an incremental/Merkle hash, which changes what the
canonical hash IS — that is #384's contract, and #397 lists three open
questions about backward compatibility and journal migration. Not decided, so
not done here.

What IS decidable without any contract change is the CONSTANT, and these tests
pin it.

Everything is asserted as an upper BOUND, never an equality: bounds cannot be
flaky (no wall-clock is measured — the work is counted instead), and a bound
does not stand in the way of the eventual fix. A Merkle hash would canonicalise
the whole graph zero times per accept, which passes comfortably.

**What one accept actually canonicalises**, measured rather than assumed. Two
earlier drafts of this file asserted the wrong thing and passed anyway; only
mutation testing exposed that they measured nothing.

On a 20-proposal graph serialising to 17,695 bytes, one accept produced:

    _compute_schedule_hash       86 bytes  (0.5% of the graph)
    _compute_audit_state_hash 18,430 bytes (104% of the graph)
    _append_event                892 bytes
    _digest                      669 bytes

So **exactly one** canonicalisation scales with the graph, not two — and the
reason matters for #397's eventual fix: `_audit_projection` includes
`mutations`, the whole accepted history, while `_schedule_projection` does not.
The quadratic term is entirely the audit hash. The schedule hash grows with the
size of the schedulable subgraph but never with the length of history.

That narrows the fix #397 is waiting on a decision for: only `audit_state_hash`
needs to become incremental.

The bounds below are therefore in BYTES against the graph size, tuned to the
measured 1.13x so that a SECOND full-graph hash (which would take it to ~2.2x)
fails. A looser bound, or a bound on call count alone, does not catch that —
the first draft used `<= 3 * graph` and caught nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from chief_wiggum.dag import journal as journal_mod  # noqa: E402
from test_dag_journal import (  # noqa: E402
    envelope,
    intent_node,
    journal_at,
    op,
)

# A canonicalisation is "full-graph" when its output is a large fraction of the
# whole graph's serialisation. The event payload and digest are per-envelope and
# stay small as the graph grows, so the split is stable rather than tuned.
FULL_GRAPH_FRACTION = 0.5


class CanonicalisationCounter:
    """Counts calls to canonical_json_bytes and the bytes they produced.

    Counting the work is deterministic; timing it is not. A wall-clock
    assertion would be flaky on a loaded CI box and would end up quarantined,
    which is how a performance guard quietly stops guarding.
    """

    def __init__(self):
        self.sizes: list[int] = []

    @property
    def calls(self) -> int:
        return len(self.sizes)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes)

    def install(self, monkeypatch):
        real = journal_mod.canonical_json_bytes

        def counting(record):
            out = real(record)
            self.sizes.append(len(out))
            return out

        monkeypatch.setattr(journal_mod, "canonical_json_bytes", counting)
        return self


@pytest.fixture
def counter(monkeypatch):
    return CanonicalisationCounter().install(monkeypatch)


def _add_intent(journal, index: int, revision: int):
    return journal.propose(
        envelope(
            [op(f"OPS-cost-{index:04d}", "add_intent_node", f"INN-dag-{index:03d}",
                value=intent_node(f"INN-dag-{index:03d}", ticket=index))],
            base_revision=revision,
            mutation_id=f"MUT-cost-{index:04d}",
            key=f"cost-{index}",
        )
    )


def _run(journal, count: int, start: int = 0, first_index: int = 1) -> int:
    revision = start
    for index in range(first_index, first_index + count):
        decision = _add_intent(journal, index, revision)
        assert decision.accepted, decision.violations
        revision = decision.graph_revision
    return revision


def _graph_bytes(journal) -> int:
    """One serialisation of the whole current graph, measured with the REAL
    function so it does not pollute the counter."""
    from chief_wiggum.dag.canonical import canonical_json_bytes as real
    return len(real(dict(journal.snapshot().snapshot_data)))


# --- the constant #396 won ----------------------------------------------------

def test_an_accept_makes_at_most_one_graph_sized_canonicalisation(tmp_path, counter):
    """One projection carries `mutations` and so scales with the graph. A
    second — or one moved inside a loop over the envelope's operations — is the
    regression this catches."""
    journal = journal_at(tmp_path)
    revision = _run(journal, 20)
    graph = _graph_bytes(journal)

    mark = counter.calls
    decision = _add_intent(journal, 21, revision)
    assert decision.accepted, decision.violations

    big = [n for n in counter.sizes[mark:] if n >= FULL_GRAPH_FRACTION * graph]
    assert len(big) <= 1, (
        f"one accept made {len(big)} graph-sized canonicalisations against a graph "
        f"serialising to {graph} bytes. Exactly one should: the audit projection, "
        f"which carries `mutations`. The schedule projection does not "
        f"(chief-wiggum#397)")


def test_bytes_per_accept_stay_proportional_to_the_graph(tmp_path, counter):
    """The bound that matters. A change serialising the whole mutation history
    per operation, or re-serialising inside a retry, blows through this while
    still making the same number of calls."""
    journal = journal_at(tmp_path)
    revision = _run(journal, 25)
    graph = _graph_bytes(journal)

    mark = counter.total_bytes
    decision = _add_intent(journal, 26, revision)
    assert decision.accepted, decision.violations
    spent = counter.total_bytes - mark

    assert spent <= 1.5 * graph, (
        f"one accept canonicalised {spent} bytes against a graph serialising to "
        f"{graph} ({spent / graph:.2f}x). Measured at 1.13x; a second full-graph "
        f"hash would take it past 2x (chief-wiggum#397)")


def test_the_full_graph_count_does_not_grow_with_the_graph(tmp_path, counter):
    """The COUNT must be constant in graph size even though each call costs
    more. If the count grows too, the run is worse than quadratic."""
    journal = journal_at(tmp_path)
    revision = _run(journal, 10)
    graph = _graph_bytes(journal)

    mark = counter.calls
    revision = _run(journal, 10, start=revision, first_index=11)
    big = [n for n in counter.sizes[mark:] if n >= FULL_GRAPH_FRACTION * graph]

    assert len(big) / 10 <= 1, (
        f"averaged {len(big) / 10} graph-sized canonicalisations per accept over "
        f"graph sizes 10-20; the count must not grow with the graph")


def test_a_rejected_proposal_does_not_cost_more_than_an_accepted_one(tmp_path, counter):
    """A stale base_revision is the common case under contention. Rejection must
    not be the expensive path, or contention compounds the existing shape."""
    journal = journal_at(tmp_path)
    revision = _run(journal, 5)

    mark = counter.total_bytes
    accepted = _add_intent(journal, 6, revision)
    assert accepted.accepted, accepted.violations
    accept_cost = counter.total_bytes - mark

    mark = counter.total_bytes
    stale = _add_intent(journal, 7, revision)  # base_revision is now behind
    assert not stale.accepted, "expected a stale base_revision to be rejected"
    reject_cost = counter.total_bytes - mark

    assert reject_cost <= accept_cost, (
        f"a rejected proposal canonicalised {reject_cost} bytes against "
        f"{accept_cost} for an accepted one — rejection must not be the "
        f"expensive path")


# --- the shape itself, recorded rather than asserted --------------------------

def test_the_known_quadratic_shape_is_still_only_quadratic(tmp_path, counter):
    """#397's shape, bounded from ABOVE so the eventual fix cannot fail it.

    Doubling the proposal count must not more than quadruple the bytes (plus
    slack). This passes today, when the cost is quadratic, and passes for any
    improvement on it — but catches a regression to something cubic.
    """
    journal = journal_at(tmp_path)
    mark = counter.total_bytes
    _run(journal, 10)
    ten = counter.total_bytes - mark

    journal2 = journal_at(tmp_path, name="graph2.db")
    mark = counter.total_bytes
    _run(journal2, 20)
    twenty = counter.total_bytes - mark

    assert ten > 0 and twenty > 0
    assert twenty <= 6 * ten, (
        f"doubling 10 -> 20 proposals multiplied canonicalised bytes by "
        f"{twenty / ten:.1f}x; quadratic predicts about 4x, so this is a "
        f"worse-than-quadratic regression")


def test_the_counter_actually_observes_something(tmp_path, counter):
    """A denominator. If canonical_json_bytes stops being reached through this
    module, every bound above passes vacuously."""
    journal = journal_at(tmp_path)
    _run(journal, 2)
    assert counter.calls > 0, (
        "no canonicalisation was observed — the monkeypatch target moved and "
        "these bounds measure nothing")
    assert counter.total_bytes > 0

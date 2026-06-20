# Traceability: business rule → contract → code → test

Chief Wiggum can prove an epic's contracts are implemented, tested, and
internally consistent — mechanically, from machine-readable annotations, instead
of trusting prose and self-reported coverage. This is the Traceability
Information Model (TIM) + Design-by-Contract pattern.

The chain, navigable in both directions:

```
business rule ──realizes──▶ contract/invariant ──guards/ensures──▶ code
                                   │
                                   └──verifies──▶ test
```

## Stable IDs

`/architect` assigns every contract and invariant a stable ID, immutable once
issued. Business rules (from `/seed`/`/architect`) get IDs too:

- `BR-<slug>-NNN` — business rule
- `CTR-<slug>-NNN` — contract (a REQUIRES/ENSURES condition)
- `INV-<slug>-NNN` — invariant

IDs are *declared* in the epic docs by a markdown heading (`### CTR-order-001 …`),
a bold label (`**INV-order-003**: …`), or a JSON `"id"` field in
`models/contracts.json` / `models/state-machines.json`.

## Annotation grammar (uniform, language-agnostic)

A single LOBSTER-style namespaced tag — works in any language's comments, and
won't collide with JSDoc/decorators/test markers:

```
@cw-trace <verb> <ID> [<ID> ...]      verbs: realizes | guards | ensures | verifies
```

Examples:

```python
# @cw-trace guards CTR-order-001
def create_order(req): ...

@pytest.mark.contract("CTR-order-001")  # @cw-trace verifies CTR-order-001
def test_create_order(): ...
```
```go
// @cw-trace ensures CTR-order-001 INV-order-003
```
```markdown
<!-- in contracts.md, near the contract: -->
### CTR-order-001 — valid date range
<!-- @cw-trace realizes BR-order-001 -->
```

- `realizes` links a contract/invariant to a business rule (authored in the epic docs).
- `guards`/`ensures` links **code** to the contract it enforces.
- `verifies` links a **test** to the contract it checks.

## The checker

`scripts/check_traceability.py` builds the graph from the defined IDs + the
`@cw-trace` annotations and reports **orphan business rules** (no realizing
contract), **uncovered contracts** (no code guard), **untested contracts** (no
verifying test), **dangling annotations** (reference to an undefined ID), and
**invalid links** (a verb whose node types violate `templates/formal-models/tim-schema.json`).

```bash
python3 scripts/check_traceability.py docs/epics/<slug> --source . --format json
python3 scripts/check_traceability.py docs/epics/<slug> --source . --gate soundness  # /architect
python3 scripts/check_traceability.py docs/epics/<slug> --source . --gate coverage   # /close-epic
```

It is a **separate pass**, not compile-time enforcement, and degrades gracefully:
an epic with no annotations reports absence rather than failing. It proves a
trace *link exists* — not that a guard is semantically correct (that is the
Design-by-Contract verification frontier, out of scope; LSP symbol resolution
is the cheaper next step). Mirrors `check_unresolved.py`.

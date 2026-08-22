# ADR #384: publish strict graph records before implementing the engine

Status: accepted for epic #383.

## Decision

Publish a strict v1 JSON Schema catalog and a pure semantic validator before adding persistence or scheduling. Intent, execution, and evidence remain separate planes. All mutation producers use one tagged envelope and one machine-readable authority matrix. Only `depends_on` and `ordered_after` participate in scheduling. Static compatibility delegates to the existing dependency parser and wave planner as the oracle.

The ticket-level lifecycle is distinct from the epic's future proposal/admission/claim machine. Attempt outcome and candidate disposition are also distinct. Terminal correction uses a compensating event plus a replacement record rather than reopening history.

## Consequences

- #385 and #386 can reuse the schemas, error codes, authority matrix, transition matrix, and adversarial fixtures.
- JSON Schema proves record shape; the pure semantic layer proves history-dependent rules such as cycles, authority, references, terminal immutability, and idempotency divergence.
- Unsupported versions fail before structural validation. Network schema retrieval and implicit migration are forbidden.
- Runtime choices—database tables, queues, scheduling heuristics, clocks, retry backoff, and routing identities—remain outside the contract.

## Rejected alternatives

- **One mutable graph:** rejected because machine-derived execution facts could acquire human intent authority.
- **Free-form mutation patches:** rejected because they bypass operation-level authority and make intent writes difficult to prohibit.
- **Copying the static planner:** rejected because two implementations would drift; the existing planner remains the compatibility oracle.
- **Encoding provider identities:** rejected because the portable contract needs only opaque role/capability references.

This ADR refines, and does not replace or weaken, the reviewed epic architecture and invariants.

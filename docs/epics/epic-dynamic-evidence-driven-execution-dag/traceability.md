# Traceability — Dynamic Evidence-Driven Execution DAG

The issue-order AC ranges below are exhaustive against the milestone snapshot used
by `/architect`. Detailed scenarios are in `integration-tests.md`; contract IDs are
defined in `contracts.json`, and business-rule realization links are in
`invariants.md`.

| Ticket | Acceptance criteria | Business rule | Contracts / invariants | Planned integration test |
|---|---|---|---|---|
| #376 | proposed-fix items 1–3 | BR-dag-001 | INV-dag-013 | IT-dag-001 |
| #384 | AC1–AC8 | BR-dag-002 | CTR-dag-005–009; INV-dag-001, INV-dag-005–008, INV-dag-011 | IT-dag-002 |
| #385 | AC1–AC9 | BR-dag-003 | CTR-dag-001–004, CTR-dag-007–009; INV-dag-001–004, INV-dag-006–010 | IT-dag-003 |
| #386 | AC1–AC12 | BR-dag-004 | CTR-dag-001–006; INV-dag-001, INV-dag-003, INV-dag-005, INV-dag-008 | IT-dag-004 |
| #387 | AC1–AC11 | BR-dag-005 | CTR-dag-010–013, CTR-dag-016–017; INV-dag-006–010, INV-dag-014–015 | IT-dag-005 |
| #388 | AC1–AC10 | BR-dag-006 | CTR-dag-018–019, CTR-dag-022; INV-dag-008, INV-dag-015–018, INV-dag-022 | IT-dag-006 |
| #389 | AC1–AC9 | BR-dag-007 | CTR-dag-014–015; INV-dag-010–013, INV-dag-017 | IT-dag-007 |
| #390 | AC1–AC9 | BR-dag-008 | CTR-dag-016–019; INV-dag-004, INV-dag-010, INV-dag-014–015, INV-dag-019 | IT-dag-008 |
| #391 | AC1–AC9 | BR-dag-009 | CTR-dag-023–025; INV-dag-008, INV-dag-020–022 | IT-dag-009 |
| #392 | AC1–AC7 | BR-dag-010 | CTR-dag-020–022; INV-dag-018–019, INV-dag-022 | IT-dag-010 |

Coverage gaps: none at architecture time. The final real-epic “fewer stalls and no
worse outcomes” criterion in #387 is an empirical comparison, not a guaranteed
success condition; the planned test records an honest negative result if dynamic
execution does not win. Likewise #391 may produce hold/rollback.

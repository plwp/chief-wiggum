# accrualflow fixture (chief-wiggum#238 validation)

This directory grounds `tests/test_plan_bet_accrualflow.py` in a **real** bet
from the operator's portfolio (`~/.chief-wiggum/portfolio/bets/accrualflow/`,
state `parked`, read-only source — never modified by this ticket). It is the
richest real artifact set available (bet.json, assumptions.json,
test-cards.json, kill-criteria.json, kill-brief.md, kill-review/), per
chief-wiggum#238's instruction to re-author one real bet through the
`/plan-bet` stage as validation.

`bet.json` and `assumptions.json` below are **structurally faithful but
redacted**: dollar figures, metric names, thresholds, dates, and every
assumption's XYZ-form statement are copied verbatim (they carry no
competitive-sensitivity). The one redaction is ASM-004's named incumbents —
the real statement names a specific low-cost competitor module and a
specific direct-twin competitor domain; both are replaced here with a
structural description ("a low-cost incumbent add-on module", "a discovered
direct competitor twin in the same app-store ecosystem") per this repo's
standing rule against naming the operator's private competitive intelligence
in a public repo (CLAUDE.md / the user's "no private repo references" and
"never name a private product" conventions). The redaction changes no field
shape, no status, no depends_on_element, and no check outcome — only the
free-text `statement` string.

`business-model.json` is newly authored here (this ticket's deliverable) by
hand-deriving canvas/premortem/vpc content from the real bet.json thesis,
the real assumptions.json ledger, and the real kill-criteria.json metric
names — see `tests/test_plan_bet_accrualflow.py` for exactly which field
traces to which real artifact. The real record never declared Fermi
inputs (the bet was parked before that math was run), so this canonical
file omits `fermi_inputs` too — faithfully, not as an oversight.

`business-model-fermi-illustrative.json` is a SEPARATE, clearly-labelled
variant that adds a `fermi_inputs` block to demonstrate the viability gate's
arithmetic against the one real anchor point (`$19` from ASM-003) plus an
operator-estimated MSC/TAM — never presented as real journaled data.

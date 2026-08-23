# External-interface provenance

*Gate: `scripts/check_interface_provenance.py` — report-only (chief-wiggum#350).*

## The escape this exists for

An epic shipped nine tool schemas for a third-party system, along with their
routes, carrying a prose caveat in the source file:

> these schemas are this ticket's own documented, best-effort approximation…
> not a verified contract

The routes were guessed too (`/api/gx-agent/<toolName>`). The real ones differ.
Every live tool call 404'd until it was fixed after deploy.

The artifacts carried **45 `TBD:` markers** and the ratchet recorded
`gate_failed: 0`. Every one of those markers was on a DATA unknown — a venue
GUID, a token, a baseline metric. The INTERFACE guesses — routes, envelope,
schemas — carried none, so they gated nothing.

CW's "unknowns gate work" discipline fired for data and not for shape. A prose
caveat is invisible to `check_unresolved.py`; only a marker gates dependent
work.

## The rule

An operation carrying `"external": true` in `contracts.json` describes a
third-party interface this system **calls**, rather than one it **serves**. It
must either:

- cite verification-grade provenance — a `derived_from` entry of type
  `observed_fact` (somebody captured a real response) or `api_doc` (somebody
  read the vendor's published contract); or
- carry a `TBD:` / `UNRESOLVED:` / marker-position `UNVERIFIED` marker, in
  which case `check_unresolved.py` already blocks dependent work and this gate
  stands down.

```json
{
  "name": "Fetch venue info",
  "method": "GET",
  "path": "/venue-info",
  "external": true,
  "external_system": "SCP",
  "derived_from": [
    { "type": "observed_fact", "ref": "curl capture 2026-08-01, docs/evidence/scp-venue-info.json" }
  ]
}
```

`ticket`, `acceptance_criterion`, `user_input` and `epic_invariant` are
deliberately **not** verification-grade. They record who *asked* for the
interface, never that anyone looked at it — and in the escape above the guessed
schemas were faithfully derived from the ticket.

Provenance is required on the **operation**, not inherited from its entity. One
`api_doc` cite at entity level would cover twenty guessed routes, which is
exactly the shape of the original defect.

## The declaration gap

`external` is optional in the schema, on purpose: an author must not be blocked
into asserting something they don't know. That leaves an omission evasion —
guess a vendor route and simply never declare it external.

This gate cannot close that hole. Nothing mechanical distinguishes a vendor path
from an own-API path. So it refuses to hide it: when an epic declares operations
and **none** are external, the report prints a named `declaration gap` rather
than a clean zero.

```
INAPPLICABLE (declaration gap): 47 operation(s) declared, none marked
  "external": true — so this gate checked nothing.
  That is correct for an epic that integrates no third-party system, and a
  blind spot for one that does.
  `external` is optional by design; a human confirms which it is. Zero of zero
  verified is not a pass.
```

The outcome is `inapplicable`, not `pass`. Zero of zero verified is not
evidence of anything, and a gate that reports it as a pass is the fail-open
shape (`docs/gate-rollout.md`, chief-wiggum#289) this repo keeps paying for.
That was not a hypothetical: the first version of this gate returned `pass` on
the declaration gap, and the dry-run below caught it reporting `pass` for 15 of
22 real epics — including a Stripe billing epic — while checking nothing.

An epic that genuinely integrates nothing external is a normal, valid state.
An epic that integrates Stripe and declares nothing is a lie the report at
least makes visible to a human.

## Why this is not a hedge-prose lint

chief-wiggum#350 also proposed flagging hedge words in contract artifacts —
"best-effort", "approximation", "assumed", "guess", "not verified" — and
requiring they be rewritten as gating markers.

That was measured before it was built, against **314 shipped epic artifacts
across 9 real repos**:

| phrase | hits | verdict |
| --- | --- | --- |
| `best-effort` | 84 | every hit a genuine design property — "a best-effort emitter", "single async best-effort recorder". A term of art, not a hedge |
| `unverified` | 42 | already written as markers, or a `FactStatus` enum member |
| `guess` | 20 | mostly meta-prose about this very discipline |
| `provisional`, `fabricated`, `assumed` | 19 | domain vocabulary, including an invariant *named* "min_stay Is Not Fabricated" |

A gate that noisy teaches the operator to `--force` past it, which costs more
than it catches (`docs/gate-rollout.md`). The presence of a cited source is
mechanically decidable; intent in prose is not. So the rule is structural.

One thing the marker measurement did buy: `UNVERIFIED` is now a
`check_unresolved.py` marker **in marker position only** — `UNVERIFIED:` or
`UNVERIFIED whether/that/if/which/…`. Authors were already writing it and
believing it gated. Bare-word matching measured 82% false on the same corpus
(18 of 22 hits were a state-machine state); marker-position matching found 4
hits, all 4 genuine unknowns the gate had been ignoring.

## Outcomes

Five-state, per chief-wiggum#289, with every count carrying its denominator:

| outcome | meaning |
| --- | --- |
| `pass` | at least one operation was declared external, and every one of them cites a verified source or is marked |
| `findings` | at least one declared external operation cites nothing that anyone saw |
| `inapplicable` | no `contracts.json`; or it declares no operations; or it declares operations and **none** are external (the declaration gap). Nothing of this gate's own kind was checked, which is not a pass |
| `error` | a `contracts.json` was present and could not be read. Exit 3 **even report-only**: report-only means findings do not block, never that the scanner may fail silently |

## Promotion

Report-only. Per `docs/gate-rollout.md`, no workflow passes `--gate` until a
passing `docs/quality/validation/check_interface_provenance.json` record exists
(`docs/gate-validation.md`). The seed classes that record must cover include
the **omission** class — an external operation that is simply never declared —
which this gate does not catch and the declaration-gap note only reports.

## Dry-run evidence (report-only precision)

Run across every epic in the local target-repo cache before shipping:

```
epics scanned: 22
  pass: 0
  inapplicable: 22   (15 of them the declaration gap, 7 with no contracts.json)
findings: 0
crashes: 0
```

Zero false positives, because no repo has adopted `external` yet — which is
also why 15 epics land on the declaration gap. That is the honest starting
state for this gate, not a clean bill of health, and it is exactly what the
note is there to say out loud.


# Debt inventory: mechanical findings with addresses (#214)

`scripts/debt_inventory.py` turns the quality layer's aggregates ("the repo has
12.3% duplication") into **addressable work items**: `debt.json` in the
resolver-determined quality dir (`<target>/docs/quality/` embedded,
`~/.chief-wiggum/meta/<owner>/<repo>/docs/quality/` sidecar — chief-wiggum#213),
each item carrying a `DEBT-` stable ID, a mechanical severity, locations, and a
blast radius. Everything here is mechanical — no LLM judgment anywhere in v1
(the LLM-judged slop lens is explicitly phase 2, its own issue, and per the
trust-class rule can never gate).

```bash
python3 scripts/debt_inventory.py owner/repo            # resolve + clone
python3 scripts/debt_inventory.py --repo ~/repos/app    # local path
python3 scripts/debt_inventory.py --repo ~/repos/app --out /tmp/validate
```

`--out DIR` redirects `debt.json` (and the previous-run lookup that powers
`first_seen`) away from the resolver quality dir — for read-only validation
runs against repos whose meta must not be written.

## Engines and tiers

Four engines under `scripts/quality/`, all consuming the same population:
tracked files, minus the existing quality-engine exclusions
(`complexity.EXCLUDE_RE`: docs/vendor/node_modules/dist/migrations/…), minus
generated artifacts (`*_pb2.py`, `*.pb.go`, `*.min.js`, lockfiles — see
`quality/population.py`), filtered by the #213 `Resolver.in_scope` predicate.

**Scope doctrine — detection repo-wide, authority in-scope** (the same
doctrine as `check_single_writer`): FINDINGS are emitted only for in-scope
files, but the EVIDENCE corpus is always the full repo, pre-scope. A symbol
used from a scope-excluded file is NOT dead (dead_code's builtin token corpus
and vulture scavenge population are pre-scope; staticcheck/knip already see
the whole module/project and only their findings are re-filtered), and a test
whose subject exists in a scope-excluded path is NOT orphaned (test_health's
existence corpus is pre-scope). Scope narrows whose debt it is — it never
manufactures phantom findings. Every engine degrades
gracefully (`{"skipped": ...}`) when its tool is absent, and dead-code reports
skipped languages as **unscanned counts** — absence of a finding in an
unscanned language is never presented as health (the `code_query` posture).

| Engine | What it finds | Tiers |
| --- | --- | --- |
| `dead_code.py` | unused exports/symbols (`file:line` + symbol) | Python: `vulture` if importable, else a conservative built-in AST pass; Go: `staticcheck` (U1000-class, `-f json`) if on PATH, else skipped; TS/JS: `knip` if on PATH, else skipped |
| `clones.py` | per-clone locations clustered into **clone classes** ("these N spans are the same"), keyed by a content hash of the normalized span | jscpd via the ONE shared runner (`duplication.run_jscpd`) — skipped when jscpd/node absent |
| `test_health.py` | orphaned tests (subject module gone — conservative per-language name mapping, reported verbatim in the result), assertion-free tests (Python ast; Go regex tier), skipped/quarantined suites (pytest/`t.Skip`/`.skip(`) | pure Python + git |
| `markers.py` | source-level TODO/FIXME/HACK/XXX with `file:line` + trailing text (first 80 chars) — `check_unresolved.py` covers docs/models only; this covers source | pure Python + git |

**Built-in Python dead-code tier — precision limits** (conservative = fewer
false positives, more misses): a module-level function/class is flagged only
when its identifier appears exactly once (its own definition) across the
identifier tokens of the whole population, tests included. Any string,
comment, or `__all__` mention counts as a use; decorated defs are skipped
entirely (decorators register routes/commands/fixtures); underscore-prefixed
names and `main` are skipped. Framework entry points invoked purely by
convention (e.g. a settings module's hook names) remain the residual
false-positive class — install `vulture` for the precise tier.

**Stale comments are NOT an engine here** — deferred to the symbol-anchored
external-link machinery from #213 (suspect-on-hash-drift generalization),
never regex-guessed.

## `debt.json` schema (`debt/1`)

Envelope: `schema`, `generated_at`, `target_sha` (mandatory —
`Resolver.stamp`), `authority` (printed verbatim by the CLI, hotspots-style),
`scope` (the #213 scope summary), `hotspots_available`/`hotspots_note`,
`engines` (per-engine sub-envelopes minus their finding payloads — `findings`
and clones' `clone_classes` are stripped since the items carry the data;
counts stay, e.g. `clone_class_count`), `unscanned_languages` (dead-code's
skipped-tier file counts), `counts` (engine × severity), `items`.

The envelope also carries a **`boundary` section** (#216 C2) — wholly
out-of-scope evidence captured where an engine sees it cheaply: clone classes
with >= 2 total spans that fell below 2 in-scope members (full pre-filter
member list, `boundary: true`, never counted in `counts` or `items`).
`plan_from_debt.py` turns these into owning-team referrals. **What ISN'T
captured** (`boundary_note` states it): markers, dead code, and test-health
findings in out-of-scope files — those corpora are scope-narrowed at the
source, so out-of-scope instances are never observed; absence from the
boundary section is NOT evidence the out-of-scope code is clean. A
`pending_candidates` block names the pending store, its count, and any
old-layout candidates migrated on this run.

Per item:

| Field | Meaning |
| --- | --- |
| `id` | `DEBT-` + first 10 hex of sha256 over `engine \0 normalized-path \0 anchor` |
| `anchor` | the exact content-anchor string used in the id derivation (#216 F1) — path-independent identity, what `plan_from_debt.py verify` uses to catch moved-not-resolved findings across a `git mv` |
| `engine` / `kind` | producing engine and finding kind (`clone_class`, `orphaned_test`, `TODO`, …) |
| `severity` | mechanical rubric below |
| `symbol` | symbol name / marker text / clone content hash |
| `locations` | `["file:line", …]` — every span/occurrence |
| `blast_radius` | `{coupling_partners, hotspot_decile}` — coupling from `quality/process.py` (`compute_coupling`, the ONE coupling engine, INV-fh-001), partners filtered through the same #213 scope predicate as the population (an out-of-scope partner never appears; if all partners drop, the empty list says so honestly); `hotspot_decile` joined from `hotspots.json` when it exists, else `null` with the absence stated in the envelope |
| `target_sha` | inherited from the envelope stamp |
| `first_seen` / `last_seen` | ISO timestamps; `first_seen` preserved across runs by stable-ID match against the previous `debt.json` |

### Stable IDs — the argued departure from INV-fh-007

Hotspots deliberately carry **no IDs**: a hotspot rank is a risk prior, and an
ID would invite treating a prior as a ticket. Debt items are the opposite —
**addressable work items** that need identity for tickets, waivers, and trend
lines. The ID is content-anchored, never ordinal and never line-based:

- dead code: `engine + file + symbol`
- markers: `engine + file + kind:normalized-text` (identical markers in one
  file collapse into one item with multiple locations)
- test health: `engine + file + kind:test-symbol`
- clone classes: `engine + "" + content-hash` — a class spans files, so the
  normalized-span content hash IS the identity; the path component is empty

Consequences (property-tested in `tests/test_debt_inventory.py`): the same
finding keeps its ID across runs and across SHA moves touching unrelated
files; moving a marker to another line keeps its ID; a FIXED finding's ID is
simply absent from the next inventory — resolved items disappear, survivors
are never renumbered.

## Severity rubric (mechanical, per engine)

- **dead_code** — tool tier (vulture/staticcheck/knip) = `medium`;
  conservative builtin-ast tier = `low`. +1 level (capped at `high`) when the
  file sits in hotspot decile >= 9: dead weight inside actively-churning
  complex code costs more than dead weight in a backwater.
- **clones** — class size >= 3 = `high` (the copy is being propagated);
  size 2 = `medium`.
- **test_health** — `orphaned_test` and `assertion_free_test` = `medium` (a
  test that verifies nothing inside a CI-run suite is manufactured false
  confidence); `skipped_test` = `low` (visible quarantine, honest about
  itself).
- **markers** — `FIXME`/`HACK`/`XXX` = `medium` in a hotspot-decile >= 9
  file, else `low`; `TODO` = `low` always.

Severity never feeds the ID — re-ranking a file's hotspot decile changes an
item's severity, not its identity.

## Authority boundary

Printed verbatim by the CLI and carried in every envelope:

> mechanical debt findings from engine scans at `<sha>`; each item is an
> addressable work item, not a verdict; the absence of a finding in an
> unscanned language is NOT evidence of health (unscanned language counts are
> included in this envelope).

Known false-positive classes, named and bounded:

- **Go helper delegation** (found on a real Go validation corpus): a test
  function whose only "assertion" is a call to a local helper that receives
  `t` (`runScenario(t, tc)` — the helper calls `require.*`/`t.Fatal` inside)
  contains no assertion-ish token itself. The regex tier cannot verify the
  helper's body, so such tests are **never flagged** `assertion_free_test`;
  they are counted under the engine envelope's `helper_delegated` bucket
  (`test_health.helper_delegated.go`) — delegation is *stated*, the helper's
  assertions are *unverified*. Detection: a call passing `t` (or `x.t`/`&x.t`)
  as an argument inside an otherwise assertion-free body.
- **JS/TS assertion-freeness is not scanned in v1** — the gap is carried in
  `test_health.unscanned.assertion_scan` and printed by every debt-rendering
  surface (the inventory report, the slop-gate debt block, the quality
  report's debt section), never left invisible.

## Report-only status and gate-promotion path

Per `docs/gate-rollout.md`, the inventory is **report-only everywhere**: the
CLI always exits 0 and has no `--gate` flag. Surfacing is read-only in three
places:

- **/code-metrics** — `quality_metrics.py` reads `debt.json` from the resolver
  quality dir and `quality/report.py` renders a debt section (counts by
  engine × severity + top items) when it exists;
- **`quality_slop_gate.py`** — a report-only signal block below the slop
  bands; it never contributes to `has_findings` or the `--gate` exit code;
- **`code_query.py orient`** — items whose `locations` name the oriented file
  surface as low-ranked `measured` facts (`source: "debt-inventory"`, exact
  file membership only — the same INV-fh-007/012 discipline as hotspot facts),
  with handles that round-trip through `show`
  (`docs/quality/debt.json#items[DEBT-…]`).

If any part of this is ever to block, it goes through the full
`docs/gate-validation.md` protocol first: a `validation/debt_inventory.json`
record with seeded-defect trials (including the mandatory omission,
config-indirection, and sampling-gap evasion classes), clean-corpus runs with
coverage evidence, an authority-boundary statement, and a journaled ratchet
record — `/close-epic` refuses `--gate` to any checker without a passing
record. Until then, findings feed remediation planning (#216), not gates. The
validation corpus for the precision exercise is dogeared (CW-built, embedded)
+ mcprelay (organic, legacy), with per-finding triage recorded on the issue.

## Consumption: remediation planning (#216)

The inventory's primary consumer is `/plan-epic --from-debt`
(`scripts/plan_from_debt.py`): `DEBT-` items are clustered (clone class →
module → change-coupling) into budgeted refactor tickets, and `/close-epic`
re-runs this inventory as the remediation epic's acceptance test.

Mid-ticket discoveries flow back in via `debt_inventory.py append-candidate`
(`engine: manual`, `candidate: true`). Candidates live in the
**mode-independent pending store**
(`<user_dir>/pending/<target-id>/candidates.json` via `artifacts.user_dir` —
never the target tree, never docs/quality), so filing one writes nothing
in-tree in embedded mode and a scratch-dir `--out` inventory run still
carries it (`build_inventory` merges pending candidates on every run; an
engine finding on the same id supersedes). An engine can neither confirm nor
remove a hand-filed observation — removal is the explicit operator act
`debt_inventory.py resolve-candidate --repo X --id DEBT-...`, which is also
what `plan_from_debt.py verify` checks for ticketed candidates (the fresh
inventory is not consulted for them). `append-candidate` refuses (exit 2)
when the derived id collides with a NON-candidate item — engines own their
evidence. Old-layout candidates embedded in an existing `debt.json` are
adopted into the pending store once, stated in the envelope and report. See
[remediation.md](remediation.md).

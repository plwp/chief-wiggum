# Remediation: turning the debt inventory into work, safely (#216)

The debt inventory ([debt-inventory.md](debt-inventory.md)) produces
addressable `DEBT-` items; this layer turns them into **budgeted epics with a
mechanical acceptance test**, and stops new slop at write time. Deliberately
**not a new skill**: it lands as `/plan-epic --from-debt`, a `refactor`
ticket-kind in `/implement`, scope back-pressure for every ticket kind on
adopted repos, a report-only prevention leg in the review step, and an
inventory re-run acceptance check in `/close-epic`. The structural template
is `patterns/improvement-loop` (signal → finding → cluster → fix-forward →
ratchet-gate), with static analysis as the signal source instead of runtime
telemetry.

```
debt.json ──► /plan-epic --from-debt ──► refactor tickets (/implement) ──► /close-epic re-runs the inventory
   ▲               │ budget REQUIRED         │ characterization first            │ every TICKETED id gone
   │               │ boundary → referrals    │ pathset back-pressure             │ (or explicitly waived)
   └── append-candidate (found ≠ fixed) ◄────┘
```

## Planning: `plan_from_debt.py plan`

`scripts/plan_from_debt.py` consumes `debt.json` (resolver-located, or
`--debt PATH`) and emits `remediation-plan.json` + `remediation-plan.md`
(`remediation-plan/1`, stamped with `target_sha`).

**Clustering precedence** (mechanical, recorded in the plan):

1. **(a) clone class** — a `clones`-engine item (one clone class = one
   `DEBT-` id) is its own ticket: deduplicating a class is one coherent unit
   that legitimately spans modules. Never merged further.
2. **(b) module/directory** — every other item clusters by the directory of
   its first location.
3. **(c) change-coupling partnership** — module clusters whose items'
   `blast_radius.coupling_partners` name files in another module cluster
   merge (union-find): change-coupled debt is one ticket even across
   directories.

Each ticket carries its `DEBT-` ids, all locations, a severity rollup, a
**derived sanctioned pathset** (the items' **in-scope** location files, in
the exact `{"paths": [...], "source": "..."}` shape `ratchet.py pathset`
consumes) plus an empty `collateral` slot declared at approach time, and
`depends_on` — tickets whose pathsets (or coupling partners) overlap are
serialized; disjoint tickets can share a wave. A partially-out-of-scope item
(e.g. a clone class with one foot in another team's tree) never smuggles
out-of-scope files into the sanctioned pathset: those locations are listed
separately as the ticket's `boundary_locations` — informational, feeds the
referral conversation, never sanctioned. The plan also records
`baseline_ids` — the full id population at plan time — which is what lets
`verify` tell a genuinely NEW id from one that existed when the plan was
cut.

### Budgets are required

`plan` refuses (exit 2: *"an unbudgeted remediation epic is unbounded
scope"*) unless at least one of:

- `--budget-count N` — max tickets (lowest-ranked clusters cut);
- `--budget-severity-floor low|medium|high` — items below the floor left
  behind;
- `--budget-cluster-cap N` — max items per ticket (least severe trimmed).

Counts and caps must be **>= 1** (a budget of 0 or less is vacuous, refused
at the CLI), and `--budget-severity-floor low` **alone** does not satisfy the
requirement — 'low' is the lowest severity, so it excludes nothing; combine
it with a count and/or cap (exit 2 explains this).

Everything left behind is recorded in the plan's `excluded` list with its
reason (`over_budget` / `below_severity_floor` / `over_cluster_cap`).
**Leftover inventory is the normal end state of a remediation epic, not a
failure** — `/close-epic` checks only the ticketed ids.

### Boundary findings are referred, never fixed

An item marked `boundary`, or whose EVERY location is outside the #213
domain scope, is **never ticketed**. It lands in the plan's
`boundary_referrals` with a filled issue body from
`templates/boundary-finding.md`: to the owning team, the finding, the
evidence, and an explicit no-auto-fix statement. A fix from outside the
owning team pollutes their churn history and produces diffs they have no
reason to trust — the issue is the hand-off.

Referrals also draw on `debt.json`'s dedicated **`boundary` section** — the
engine-captured wholly-out-of-scope evidence the #214 engines would
otherwise drop before the inventory (see
[debt-inventory.md](debt-inventory.md) for exactly what IS and ISN'T
captured there: clone classes dropped for out-of-scope members are; marker /
dead-code / test-health findings in out-of-scope files are not, because
those corpora are scope-narrowed at the source).

### Grandfathered items are valid input

Remediating a grandfathered finding (#215) **before its expiry is the
point** — the plan tickets them normally and marks them
(`grandfathered_ids`), which is what lets `verify` distinguish a pre-plan
grandfather (not a waiver) from a post-plan one (an explicit waiver).

## The `refactor` ticket-kind (`/implement`)

For `kind: refactor` tickets (`--from-debt` tickets, or any ticket labeled
`refactor`), Step 5's TDD objective **inverts**: instead of failing tests
first, the worker writes **characterization (golden-master) tests pinning
CURRENT behavior before touching code** — approval-test harness per stack
(pytest golden values / `approvaltests` / `syrupy`; Go golden files; Jest
snapshots) — all green BEFORE the refactor commit. The refactor then
proceeds against that pinned baseline:

- the ratchet pass-set may not shrink (already enforced — `docs/ratchet.md`);
- mutation testing runs scoped to the refactored files where a tool exists
  (`mutmut` / `go-mutesting` / Stryker), best-effort with absence stated;
- **behavior preservation is the FIRST review-checklist item** for this kind
  (`templates/review-checklist.md`);
- a sanctioned refactor that must move protected artifacts goes through the
  EXISTING `--amend`/`--retire` ratchet journal path — no new bypass.

## Scope back-pressure (adopted repos, ALL ticket kinds)

Brownfield discipline is a property of the **repo**: it switches on when the
#215 adoption record (`<meta root>/adoption/adoption.json`) exists. On legacy
repos, "while I'm in here" is the dominant failure mode for feature work as
much as debt work — collateral rewrites pollute characterization baselines,
wreck churn metrics, and produce PRs the owning team won't trust.

- **Declared touch plan** — `/implement` Step 4's approach output ends with a
  declared pathset (files/globs the ticket expects to touch), written to
  `$TICKET_TMP/pathset.json`. `--from-debt` tickets derive theirs
  mechanically (`plan_from_debt.py pathset --plan ... --id RT-001
  --collateral <tests that move>`); feature tickets declare theirs in the
  approach.
- **Out-of-pathset flagging** — `ratchet.py pathset --report-only` (the #213
  parking machinery, parameterized by pathset source) runs before review
  (Step 7) and again after review fixes (Step 8); escapes feed the review
  context as a flagged section. **Report-only first** per
  [gate-rollout.md](gate-rollout.md) — teeth (the same park-for-human
  semantics as `ratchet.py protected`) only after a gate-validation record.
- **Found ≠ fixed** — anything discovered mid-ticket is filed **in the same
  turn** as a `DEBT-` candidate (`debt_inventory.py append-candidate --repo X
  --engine manual --path F --note "..."` — same stable-ID mechanics, `engine:
  manual`, `candidate: true`) or as an issue, and left untouched in the diff.
  Candidates live in the **mode-independent pending store**
  (`<user_dir>/pending/<target-id>/candidates.json`) — never the target tree
  (embedded mode writes nothing in-tree) and never a particular `debt.json`
  (a scratch-dir inventory can't lose them); every `build_inventory` run
  merges them in, and only the explicit operator act
  `debt_inventory.py resolve-candidate --repo X --id DEBT-...` removes one.
  `append-candidate` refuses (exit 2) an id that collides with an engine
  finding — engines own their evidence. Scope discipline must not cost
  information, or agents will smuggle fixes.
- **No collateral improvement** — formatting-only, rename-only, and
  style-only hunks outside the declared pathset are scope creep by
  definition, for every ticket kind; the review checklist's Scope Discipline
  section covers the judgment the pathset check can't make.

## Prevention leg (report-only, NEVER blocking)

`scripts/prevention_signals.py --base <ref>` emits diff-scoped slop signals
appended to `/implement`'s review context (Step 7) and the wave report:

- **new duplication** — the clones engine runs live; a clone class with a
  member span inside the diff's added lines AND one outside means the diff
  copied existing code;
- **dead code introduced** — added exports unused anywhere (dead-code
  builtin-ast tier on the changed files, identifier corpus repo-wide);
- **assertion-free tests added** — test_health's tiers on added test
  functions (JS/TS unscanned in v1, stated).

It always exits 0, has no `--gate` flag, and states its own authority
boundary. Git's C-style quoted paths (unicode/space filenames under the
default `core.quotepath`) are unquoted before scanning; a changed path that
still fails to resolve is counted under `unscanned_files` with a reason —
never silently clean. Any unexpected failure prints an honest error block
("prevention signals unavailable: … — treat as not-run, not clean") so the
review context always receives *something*, and still exits 0. Promotion to
a blocking gate requires the full [gate-validation.md](gate-validation.md)
protocol — ship the signal, prove precision, then argue about teeth.

## Acceptance: `/close-epic` re-runs the inventory

For a remediation epic, `/close-epic` Step 2k re-runs `debt_inventory.py`
fresh and runs:

```bash
python3 scripts/plan_from_debt.py verify --repo <target> --plan <remediation-plan.json> --debt <fresh debt.json>
```

Exit 1 lists every **ticketed** id still present — blocking. Absence alone is
not enough: an id whose content **anchor** (every item exposes the exact
anchor its id was derived from; clone classes use the member-content hash)
reappears under a NEW id — one not in the plan's `baseline_ids` — was
**moved, not resolved** (a `git mv` rename survives the probe) and is listed
as `MOVED old -> new` and counted unresolved. **Stated boundary of the
anchor compare:** rewording a marker (TODO/FIXME text) mints a new anchor,
so a reworded-not-fixed marker is not caught as moved — it surfaces in the
informational **new-ids-in-ticket-files report** ("new debt appeared in the
ticket's own files — review before closing"), which never fails the run.
Ticketed **candidates** resolve against the pending store (absent = the
operator ran `resolve-candidate`), never against the fresh inventory. An id
may be explicitly waived instead of fixed via the `adopt.py grandfather
--extend` path (loud, reasoned, expiring); `verify` accepts only
**post-plan** (the entry's own `created_at` postdates the plan's
`generated_at` — entries without timestamps never waive), non-expired
grandfathers as waivers. Budgeted-out leftovers and boundary referrals are
never checked — they were never claimed.

## What proves it

`tests/test_plan_from_debt.py` (clustering precedence, budgets, boundary
referrals, pathset shape, verify semantics), `tests/test_prevention_signals.py`
(diff-scoped signal precision + the always-exit-0 posture),
`tests/test_debt_inventory.py` (append-candidate stable ids + carry-forward),
and `tests/test_remediation_e2e.py` — a full miniature remediation epic
against a fixture repo: sidecar adoption → budgeted plan → green
characterization baseline → refactor → pathset check → prevention signals →
fresh-inventory `verify`. The real-repo exercise ran read-only against
mcprelay (sidecar, isolated `CHIEF_WIGGUM_USER_DIR`) per the #216 acceptance
criteria.

# Sidecar footprint: the knowledge leaves the tree, the authority doesn't

Chief Wiggum's meta-mechanics — `docs/epics/`, `docs/quality/`, `docs/patterns/`,
ratchet journals, `@cw-trace` source comments — are the product's paper trail on
a CW-built repo. On a legacy or client repo they are invasive: some orgs would
reject the PR outright. And on a large shared repo, a team needs CW's attention
(and its gates' authority) confined to its own domain. The **sidecar footprint
mode** answers both: all CW knowledge moves outside the target tree, and a
**domain scope** confines gate authority to the paths the team owns — while the
gates themselves behave identically to embedded mode.

`scripts/artifacts.py` is the single resolver every skill, gate, and query
surface routes through. Nothing assumes `docs/quality` anymore; everything asks
the resolver.

## Two modes, one election

- **`embedded`** (the default — no election file means embedded, so every
  existing repo keeps working unchanged): meta lives in `<target>/docs/`
  exactly as before.
- **`sidecar`**: meta lives under `~/.chief-wiggum/meta/<owner>/<repo>/docs/`
  with the **identical layout** beneath. Default backing is a private git
  meta-repo (`--backing git`: history, cross-machine sync, PR-able contract
  changes); `--backing local` is a plain directory with the same layout.

The election is recorded **once per target, never in the target**:

```bash
python3 scripts/artifacts.py elect <target-path> --mode sidecar --backing local
python3 scripts/artifacts.py show <target-path>            # where does meta resolve?
python3 scripts/status.py --repo <target-path>             # the full live screen
```

It lives at `~/.chief-wiggum/meta/<owner>/<repo>/election.json` (target identity
from the origin remote; a path-hash fallback for remoteless repos). An
embedded→sidecar decision must not itself be a write into the tree whose
footprint is being decided — so it never is. `CHIEF_WIGGUM_USER_DIR` overrides
the `~/.chief-wiggum` root (tests always set it; the real home dir is never a
test fixture).

## Layout: identical beneath the meta root

```
~/.chief-wiggum/meta/<owner>/<repo>/
├── election.json          # the footprint election (mode, backing, when)
└── docs/                  # the SAME tree a target's docs/ carries in embedded mode
    ├── scope.json         # domain scope (optional; missing = whole repo)
    ├── epics/<slug>/      # contracts.md, invariants.md, state-machines.md, models/…
    ├── quality/           # ratchet.json, journal, scorecard, trace-links.json,
    │                      # external-links.json, validation/<gate>.json, debt.json
    ├── patterns/          # adopted.json
    └── design/
```

Because the layout is identical, every path helper (`Resolver.epics_dir()`,
`quality_dir()`, `patterns_dir()`, `design_dir()`) is mode-blind: gates and
skills call the resolver and never care which mode won the election.

## Code stays in-repo, knowledge goes sidecar

The split is by kind, not by convenience:

- **In-target, regardless of mode**: pattern scaffolds, CI config, tests,
  migrations — anything that must ship and run with the product.
- **Sidecar, in sidecar mode**: contracts, state machines, invariants, ratchet
  journal/high-water/scorecard, debt inventory, trace links, gate-validation
  records, justifications — the *knowledge about* the code, none of which the
  product needs at runtime.

One config consequence: the sidecar `ratchet.json`'s `epic_docs` must point at
the sidecar epics tree (an absolute path — the embedded default `docs/epics`
is target-relative and would hash nothing there, a silently vacuous contract
ratchet). `ratchet.py init` resolves this itself: on a sidecar-elected target
it writes the absolute sidecar epics dir; embedded targets keep the portable
`docs/epics` default.

## Threat model: the goalposts leave the reviewed diff

In embedded mode, rule 3 of the ratchet ([ratchet.md](ratchet.md)) is enforced
by **diff inspection**: `ratchet.py protected` parks any worker branch touching
the protected pathset. In sidecar mode the goalposts — contracts, specs,
ratchet state — have **no path inside the target tree**, so a goalpost edit
cannot ride in the worker's *reviewed diff*: there is nothing for a branch to
touch, nothing for a merge to carry, and the C2-style channel (a goalpost move
hidden inside an otherwise-plausible code change) is removed. That is the
claim — no more.

### Trust boundary

What sidecar mode does **not** do (same voice as [ratchet.md](ratchet.md)'s
trust-boundary section — stated assumptions, not TODOs):

- **Workers are not filesystem-sandboxed.** A worker process runs as the same
  user, and the sidecar is a plain directory under `~/.chief-wiggum/` — a
  process that chooses to write it directly, can. The boundary is the **diff**,
  not the disk. (With `--backing git`, the meta-repo's history is the outer
  anchor for such writes, the same role git history plays for embedded
  `docs/quality/**`.)
- **`CHIEF_WIGGUM_USER_DIR` re-roots resolution entirely.** It exists for test
  isolation (see below); an agent that sets it points every resolver at a
  directory of its choosing. It is a convenience knob, not a security control.
- **Elections are overwritable by convention.** `elect` records the mode by
  overwriting `election.json` — deliberately, so an operator can switch modes;
  it means the election itself is an operator convention, not a tamper-evident
  fact. Re-electing is loud (`/status` names the mode live) but not prevented.

A worker that misbehaves at the filesystem level is caught by the layers that
already own that class: the sidecar meta-repo's own git history, and the fact
that gate verdicts are derived live from the sidecar the *orchestrator* reads
— not from anything the worker hands back.

### `CHIEF_WIGGUM_USER_DIR`

The env var overrides the `~/.chief-wiggum` root for **test isolation**: every
test sets it to a tmp dir so the real home dir is never a fixture (precedence:
explicit `cw_home` parameter > `CHIEF_WIGGUM_USER_DIR` > `~/.chief-wiggum`,
see `scripts/artifacts.py user_dir`). It is **not a security boundary** — per
the trust-boundary note above, anything that can set the environment can
re-root where elections, scopes, and sidecar meta resolve.

## Version binding: every artifact names the HEAD it was computed against

Sidecar artifacts float free of the target's git history, so binding is
explicit and mandatory: `Resolver.stamp(payload)` adds `target_sha` (the
target's current HEAD — the same `git_sha`/`--check` discipline as
`scripts/hotspot_discovery.py`), and `Resolver.check_stale(payload)` returns a
warning string when the recorded sha no longer matches HEAD — or when the
field is missing entirely (unverifiable counts as a warning, never a silent
pass). External trace-link entries carry `target_sha` per entry.

## External trace-link store (`quality/external-links.json`)

In sidecar mode, in-source `@cw-trace` annotations are replaced by
symbol-anchored entries — `file :: symbol → <verb> <IDs>` — each carrying a
content hash of the anchored symbol's source span:

```bash
python3 scripts/chief_wiggum/external_links.py add <store> --target <repo> \
    --file internal/billing/reconcile.go --symbol ReconcileStripe \
    --verb guards --ids CTR-bil-001
python3 scripts/chief_wiggum/external_links.py verify <store> --target <repo>
```

Symbol anchoring is tiered: **ast** for Python (the exact qualified-function
span machinery of the verifier-hash dimension, #206), **lsp** where a language
server is configured and installed (gopls today), the emitters' declaration
**regex** tier for the remaining known extensions, and **skip-with-warning**
beyond that — an unanchorable entry is recorded and reported as `unresolved`,
never dropped.

**Hash drift ⇒ suspect.** `verify` re-anchors every link against current
source; a symbol that resolves but re-hashes differently is a **suspect** link:
the claim was validated against code that has since changed — re-verify, don't
trust. Same discipline as [traceability.md](traceability.md)'s suspect links,
applied from the code side instead of the contract side.

Consumers:

- `check_traceability.py` reads the store as a second annotation source
  (defaulting to the resolved meta root's `quality/external-links.json` when
  the mode is sidecar). Verified-**ok** entries join the annotation set and
  satisfy coverage exactly like in-source annotations — same dangling/schema
  validation, same `coverage_requires` rules. **Suspect entries never satisfy
  coverage**; unresolved entries surface as warnings.
- `code_query.py` Plane B folds store entries in as a second annotation source,
  labeled `external-link-store`, so `orient` on an annotation-free sidecar
  target still gets real answers.

## Domain scope (`scope.json`)

`{"include": [globs], "exclude": [globs]}` at the meta root, fnmatch
semantics: missing file = whole repo, empty include = everything, exclude wins.
Seedable from CODEOWNERS. `Resolver.in_scope(path)` / `artifacts.path_in_scope`
is the single matching rule every consumer shares.

**Detection scans repo-wide; authority stops at the boundary.** Gates classify
findings, they don't narrow their scan:

- `check_single_writer.py --scope auto` (or an explicit path): writers inside
  scope stay blocking-eligible exactly as before; writers outside become
  **boundary** findings — `boundary: true`, a labeled report section, never
  the exit code, never auto-fixed. The out-of-domain writer of our controlled
  field is the gate's motivating incident and must stay *visible*, just not
  *ours to block on*.
- Population discipline for the follow-on engines (#214's debt inventory,
  hotspot deciles): baselines and deciles are computed **within scope**, never
  over the full repo population — a team's tenth-decile file is the worst file
  *it owns*, not the worst file in the monorepo.
- `ratchet.py pathset --pathset-file <scope.json or ticket pathset>` is the
  inverse of `protected`: a worker diff that **escapes** the sanctioned
  pathset is parked for the human — one mechanism, parameterized by pathset
  source (the domain `scope.json`, or #216's ticket-scoped `{"paths": [...]}`),
  two altitudes of scope-creep back-pressure.

## What proves it

`tests/test_sidecar_roundtrip.py` runs the same gate sequence — ratchet
init/score/check, traceability coverage, single-writer, `/status` — twice:
once embedded, once from a sidecar against a target with **zero** CW files
in-tree, and asserts the verdicts match and the external-link store satisfies
coverage where the embedded run used in-source annotations.

Like every mode decision here: modes coexist indefinitely. Nothing migrates an
embedded repo to sidecar as a side effect — that is `/adopt`'s job, and it is
an operator's election, not a default.

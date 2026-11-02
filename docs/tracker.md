# tracker.py: a tracker-agnostic issue interface

Every workflow that touches issues (`/create-issue`, `/plan-epic`, `/implement`,
`/implement-wave`, `/close-epic`) used to call the `gh` CLI directly, hard-coupling
issue tracking to GitHub Issues: no offline/local use, no client projects that
live somewhere else, no way to test issue-driven workflows without a network.

`scripts/tracker.py` is a small, pluggable interface over issue tracking,
mirroring the existing `repo.py` / provider-role patterns. This ticket ships two
backends — `github` (the reference implementation) and `local` (a git-committed
markdown backend, and the offline test double for every issue-driven workflow).

## The interface

```python
from tracker import GithubBackend, LocalBackend, IssueDraft, get_tracker

backend = get_tracker("acme/app")  # -> GithubBackend unless configured otherwise
ref = backend.create(IssueDraft(title="Fix bug", body="...", labels=["bug"]))
issue = backend.get(ref)
backend.update(ref, {"state": "closed"})
backend.comment(ref, "Shipped in #45")
backend.group([ref], "Epic: Widgets")
members = backend.members("Epic: Widgets")
```

Every backend implements the same five verbs plus epic grouping:

| Method | Signature | Notes |
|---|---|---|
| `get` | `get(ref) -> Issue` | Fetch one issue by ref. |
| `list` | `list(query=None) -> list[Issue]` | `query` is a substring (matched against title/body) or a `dict` of exact-match filters (e.g. `{"epic": "Epic: Name"}`). |
| `create` | `create(draft: IssueDraft) -> ref` | Returns the new issue's canonical ref. |
| `update` | `update(ref, fields: dict) -> Issue` | `fields` may set `title`, `body`, `state`, `labels` (replaces the set), `assignee`, `epic`. `state` is validated **before dispatch** on every backend: anything other than `"open"`/`"closed"` raises `ValueError` and nothing is mutated. |
| `comment` | `comment(ref, body) -> None` | Adds a comment. Comments are never part of `Issue.body` on any backend (GitHub: a separate resource; local: a delimited `## cw-comments` section) — so `list()` substring queries never match comment text. |
| `group` | `group(refs: list[str], epic_name) -> None` | Assigns every ref to an epic. |
| `members` | `members(epic_name) -> list[Issue]` | All issues currently in that epic. |

`Issue` is a dataclass: `ref`, `title`, `body`, `state`, `labels`, `assignee`,
`epic` (the grouping key), `url_or_path` (a GitHub URL or a local file path).
`IssueDraft` is the create-time input: `title`, `body`, `labels`, `assignee`,
`epic`.

## Issue refs are URIs

- `gh:owner/repo#42` — GitHub, explicit.
- `local:docs/issues/0042.md` — the local backend, path relative to the target
  repo root. The path is containment-checked: absolute paths and any ref that
  resolves outside the repo's `docs/issues/` directory (e.g.
  `local:../other.md`, `local:/tmp/x.md`) are rejected with `ValueError`.
- Bare `owner/repo#42` (no scheme) **keeps meaning GitHub** — zero breakage for
  existing usage and docs.
- `obsidian:<vault-rel-path>` and `jira:PROJ-42` are recognized by the ref
  grammar (`parse_ref`) for forward compatibility, but neither backend is
  wired up yet — constructing one raises `NotImplementedError`. See
  [Out of scope](#out-of-scope-this-ticket) below.

`tracker.parse_ref(ref) -> (scheme, identifier)` is the pure function behind
this; it never touches the network or filesystem.

## Backend resolution

Given a target repo, which backend applies is resolved in this order:

1. `docs/cw/tracker.json` **in the target repo**:
   ```json
   { "backend": "local" }
   ```
2. CW-side fallback, `~/.chief-wiggum/config.json`:
   ```json
   { "tracker": { "backend": "local" } }
   ```
3. Default: `"github"` — today's behavior, unconfigured repos are unaffected.

`backend` may be `github`, `local` or `mcp` (see below). On `mcp`,
ref-addressed `gh:` operations also route through the mcp backend, so a
comment or completion is mirrored, and `task:<service key>` refs resolve to
their GitHub issue.

`tracker.resolve_backend_name(repo_root)` is the pure function; `get_tracker(target,
repo_root=...)` builds the actual backend (a `GithubBackend` bound to `target`
as `owner/repo`, or a `LocalBackend` bound to `repo_root`).

Ref-addressed operations (`get`/`update`/`comment`/`group`) don't need config
resolution at all — the ref's scheme says which backend to build directly
(`gh:` -> `GithubBackend`, `local:` -> `LocalBackend` rooted at `--repo-root`
or cwd). Only repo-addressed operations without an existing ref (`list`,
`create`, `members`) consult `docs/cw/tracker.json` / the CW-side fallback.

## Backends

### `github` — the reference implementation

Wraps `gh issue create/view/list/edit/close/reopen/comment` and `gh api
.../milestones`. Epic grouping maps onto a GitHub milestone, exactly as
`/plan-epic` does today. `state` maps to `gh issue close`/`reopen`; `labels`
updates diff the current set against the requested set and issue the minimal
`--add-label`/`--remove-label` pair.

The `gh` transport is injectable (`GithubBackend(repo, runner=...)`), matching
the existing `chief_wiggum/github.py` convention — this is what makes the
conformance suite (see below) testable without a network.

### `local` — one markdown file per issue

One file per issue, `docs/issues/NNNN.md` (4-digit, zero-padded), **committed
to git** in the target repo. Format:

```markdown
---
id: 42
title: "Fix crash on empty form"
state: "open"
labels: ["bug", "urgent"]
epic: "Epic: Widgets"
assignee: "alice"
---

Body markdown goes here.

## cw-comments

---
First comment text.

---
Second comment text.
```

Comments are appended by `comment()` under a `## cw-comments` heading at the
end of the file. Everything from that heading onward is **excluded** from
`Issue.body`, so body semantics match the GitHub backend (where comments are
a separate resource) and `list()` substring queries never match comment text.
`update(ref, {"body": ...})` replaces only the body and preserves the
comments section.

Every frontmatter value is written with `json.dumps` — JSON is a valid subset
of YAML, so the file is real, parseable YAML frontmatter (openable by any
YAML-aware tool, e.g. Obsidian) without pulling in a `pyyaml` dependency.

IDs auto-increment from the highest existing numeric filename in `docs/issues/`.

### `mcp` — a task service mirrored, GitHub the source of truth (chief-wiggum#498)

For a team that runs an MCP task service: tasks with claims and leases, run
notes written by the holder, completion, and a decision log with provenance.
Selected per target:

```jsonc
// <target meta root>/cw/tracker.json  (docs/cw/tracker.json when embedded)
{ "backend": "mcp", "mcp": { "mirror": "strict" } }
```

```jsonc
// ~/.chief-wiggum/config.json: the machine-local server command and identity
{ "tracker": { "mcp": { "command": ["/path/to/task-service", "serve"],
                        "agent": "<the identity the server authenticates>" } } }
```

The two `mcp` blocks merge key by key, the target's over CW's
(`tracker.resolve_mcp_config`). No `backend` key anywhere means `github`.

| Key | Meaning |
|---|---|
| `command` | stdio MCP server argv (required) |
| `agent` | the service identity; claims are made as this caller, and CW's holder (a wave or worker id) is kept locally |
| `mirror` | `strict` (default) or `best_effort` |
| `actor`, `priority`, `lane` | sent when a task is created (defaults `worker`, `P2`) |
| `environment`, `capabilities`, `claim_state` | sent with a claim |
| `repo` | owner/repo for `task:` refs, when the origin remote does not say |
| `timeout_seconds` | transport timeout |

No secrets pass through CW: the server authenticates itself.

Until the service mirrors its own writes to GitHub:

| Verb | GitHub | Task service |
|---|---|---|
| `get` / `list` / `members` / `ready` / `deps` / `context` | **read here** | never read |
| `create` | first | a task with the title, provenance class and source, and a done-when naming the issue; the service mints the key |
| `comment` | first | a run note when this machine holds a run; outside a run, skipped until the service supports it |
| `status` | the note, as on `github` | a run-status note from the holder's run |
| `complete` | `gh issue close --comment` | completes the holder's run; the run is dropped |
| `decision` | the decision comment | the decision log, task-scoped, with provenance; a relayed human decision is recorded as `human:<name>` |
| `claim` / `heartbeat` / `release` / `lease` | nothing (GitHub holds no claims) | a claim naming the task; every lease it returns is renewed or released |
| `update` / `group` / `link` | only here | no service verb |

**Failure.** GitHub is written first; if it fails nothing reaches the
service. If the service then refuses or fails, `strict` raises `MirrorError`
(CLI exit 3: "GitHub updated, task service failed", with the service's
reason), `best_effort` warns on stderr. Status and completion need the run
this machine claimed, by the same holder; without one the call is a mirror
failure that says "claim it first".

**Claims.** A claim always names its task: the untargeted form (next ready
task) is never used, because it may hand over a different task, and
`capabilities` reports `claim` only when the service accepts a named claim. A
refusal meaning "held by someone else" or "not ready" returns false (CLI exit
1, skip the ticket); any other refusal (identity, capability, capacity,
hours, unknown task) raises with the service's reason. Leases expire: a
long-running holder calls `tracker.py heartbeat REF`.

**Declared gaps are not failures.** A verb the service does not support yet,
or a task the service does not know (an issue older than the adoption),
skips the mirror with a stderr notice. `tracker.py service TARGET` lists the
gaps.

**Provenance on create.** `create --provenance-class --provenance-source
[--agent]` is carried to the service, which requires it. A create with no
stated provenance is sent as an `agent` proposal citing the configured agent
and the date, never as a stronger class. An `agent`-class task is shown as a
proposal (`service TARGET --ref REF` reports `agent_proposal`) and can be
claimed at once: nothing waits on a confirmation.

**Keys.** The service mints `local#<n>` keys, and such a task is not on
GitHub until the service mirrors it. The adapter records which issue each
key it created belongs to, so `task:local#<n>` resolves to that issue; a
`local#<n>` task created on the service side has no issue, is not visible to
GitHub-reading skills, and raises `NotVisibleOnGitHub`. An issue the service
already knows by an `owner/repo#N` key is found by that key. Key and run
records live in `~/.chief-wiggum/state/tracker/<owner>/<repo>.json`, so a
`claim` and a later `complete` in separate invocations share the run.

**The mapping is one class.** The service's tool names, argument shapes,
result parsing and refusal reasons are in
`chief_wiggum.task_service.ServiceMapping` and nowhere else. Tests run the
backend against `tests/fake_task_service.py`, in process and as a stdio MCP
server; the fake rejects missing or unexpected arguments the way the service
does, and enforces holder-only run notes, provenance on create and decision,
the claiming identity, and one live claim per task.

Pending on the service side: comments outside a run, and mirroring its
writes to GitHub. When mirroring lands, the backend can read from the
service and stop writing GitHub itself.

## Adding a backend

1. Implement the seven methods above (`get`, `list`, `create`, `update`,
   `comment`, `group`, `members`) against `Issue`/`IssueDraft`.
2. Register the ref scheme in `KNOWN_SCHEMES` if it's new, and wire it into
   `get_tracker()` / `_backend_for_ref()`.
3. Add it to the conformance suite in `tests/test_tracker.py`
   (`backend_and_verify` fixture) — a backend only ships once it passes the
   *same* parameterized suite as `github` and `local`
   (create → list → group → update → comment round-trip).
4. Document the URI scheme and config value here.

## Out of scope (this ticket)

- **Obsidian**: a config variant of `local` — same file format, pointed at a
  vault-relative path instead of `docs/issues/`. Wiki-links in bodies pass
  through untouched (the local backend never parses body content). Documented
  here only; no code ships in this ticket.
- **Jira**: REST via an API token from the system keyring (`chief-wiggum`
  service — never an env var, per the repo's secret-management convention).
  Deferred to a real client need. Only `title`/`body`/`state`/`labels`/`epic`
  would map — no custom fields or workflows.
- **Two-way sync between backends** — a ref lives in exactly one backend
  (the `mcp` backend mirrors one way, GitHub to the service, until the
  service mirrors back).
- **Migrating existing GitHub issues** anywhere.

## CLI

```bash
python3 scripts/tracker.py --repo-root "$target_root" backend    # print resolved backend name
python3 scripts/tracker.py get gh:acme/app#42
python3 scripts/tracker.py get acme/app#42                      # bare = same as gh:
python3 scripts/tracker.py --repo-root "$target_root" list acme/app --epic "Epic: Name"
python3 scripts/tracker.py --repo-root "$target_root" create acme/app --title "Fix bug" --body "..." --label bug
python3 scripts/tracker.py update gh:acme/app#42 --set state=closed
python3 scripts/tracker.py comment gh:acme/app#42 "Looks good"
python3 scripts/tracker.py group "Epic: Name" gh:acme/app#42 gh:acme/app#43
python3 scripts/tracker.py members acme/app "Epic: Name"

# local backend, operating against a target repo checkout:
python3 scripts/tracker.py --repo-root "$target_root" get local:docs/issues/0001.md
python3 scripts/tracker.py --repo-root "$target_root" group "Epic: Name" local:docs/issues/0001.md
```

Output is JSON (an `Issue` dict, or a list of them) for every command that
returns data, matching sibling scripts like `epic_metadata.py`.

**Always pass `--repo-root`** when calling from a workflow: `--repo-root`
defaults to the current working directory, and workflows run from arbitrary
cwds. Resolve the target checkout first (`target_root=$(python3
"$CW_HOME/scripts/repo.py" resolve "$owner_repo")`) and pass it on every
call — that is what makes the target repo's `docs/cw/tracker.json` config
(and the local backend's storage location) take effect. The `backend`
subcommand prints the resolved backend name so command prompts can gate
GitHub-specific plumbing (e.g. milestone descriptions) on `backend == github`.

## Command migration

Every skill tracks work through `tracker.py`; none calls `gh issue`
(chief-wiggum#498). `tests/test_tracker_boundary.py` fails if any file under
`.claude/commands/`, `skills/`, `scripts/`, `templates/`, `config/` or
`start/` shells out to `gh issue` (shell, argv or `gh api .../issues` form)
outside `scripts/tracker.py`, and plants calls first to prove the scan
bites. `gh` for PRs, repos, labels and milestones is unaffected.

| Skill | Was | Now |
|---|---|---|
| `/implement` Step 1 | `gh issue view --json milestone` | `tracker.py get REF \| jq .epic` |
| `/implement` Step 2 | `gh issue view --json title,body,author,labels,assignees,milestone,comments` | `tracker.py context REF` (same JSON) |
| `/implement` Step 9 | `gh issue view --json labels` | labels from Step 2's `issue-raw.json` |
| `/implement-wave` Step 2 | `gh issue list --milestone --state all/closed` | `tracker.py list --epic --state all/closed --numbers` |
| `/implement-wave` Step 7 | `gh issue close --comment` | `tracker.py complete REF --summary` (one `gh issue close --comment`) |
| `/seed` Step 8 | worker told to use `gh issue create` | `tracker.py create ... --disclose-ai` |
| `/reflect` Step 5 | `gh --repo ... issue create --body-file` | `tracker.py create --body-file` |
| `chief_wiggum.github.list_issues/view_issue` | own `gh issue` argv | `GithubBackend.raw_list/raw_view` (identical argv) |

`/create-issue`, `/plan-epic`, `/architect` and `/close-epic` moved earlier
(#158, #267). In `/plan-epic` the GitHub milestone plumbing stays
conditional on the resolved backend.

## Work-tracking verbs (chief-wiggum#498)

| Verb | CLI | GitHub | local |
|---|---|---|---|
| `read_context(ref) -> dict` | `context REF` | `gh issue view --json title,body,author,labels,assignees,milestone,comments`, passed through | same shape from the file |
| `set_status(ref, status, holder=, note=)` | `status REF ready\|in_progress\|blocked --holder H --note N` | the note as a comment; no note writes nothing; no status label is invented | same |
| `complete(ref, summary, holder=)` | `complete REF --summary S` | `gh issue close N --comment S` | comment + `state: closed` |
| `log_decision(ref, Decision)` | `decision REF --what --decided-by --class --source [--channel --words --relayed-by]` | a comment: prose plus a `<!-- CW-DECISION {json} -->` block | same |
| `lease(ref) -> Lease` | `lease REF` | unsupported (no claims) | from the `.claim` file |
| `create(draft)` with `draft.provenance` | `create --provenance-class --provenance-source [--agent]` | ignored (no behaviour change) | ignored |

`status` takes `ready`, `in_progress` or `blocked`; finishing is `complete`.
A `Decision` carries a `Provenance` whose class is one of `legal`, `human`,
`customer`, `agent` (anything else, including "to confirm", is refused) and
whose `source` cites it; an `agent` decision renders as "agent proposal".
`parse_decision_block(text)` reads the block back.

### Lease and claim, and the DAG contract

`Lease` uses the DAG contract's `lease_state` vocabulary (`unclaimed`,
`active`, `released`, `expired`, `revoked`; a test pins it to
`schemas/dag/v1/vocabulary.schema.json`). `Lease.to_dag_lease_fields()`
supplies a `lease_record`'s fields, so the DAG's lease is the tracker's claim
rather than a second store:

| `lease_record` (docs/dag-contract.md) | `Lease` |
|---|---|
| `lease_id` | `lease_id` (the service's lease id) |
| `execution_node_id` | resolved by the DAG compiler from `intent_ref`: an execution node pins its intent node, which pins the tracker ref |
| `attempt_id` | `run_id` (the claim's run) |
| `epoch` | `epoch` |
| `state` (`lease_state`) | `state` |
| `fencing_token` | `fencing_token`, else `lease_id`, else `run_id` |
| (none) | `holder`, `expires_at` |

`claim(ref, agent_id) -> bool` and `release` keep their #371 meaning;
`lease(ref)` reports the current claim.

## Dependency verb group (chief-wiggum#371)

The five core verbs (`get` / `list` / `create` / `update` / `comment`) plus
epic grouping stay the minimal contract every backend must honour. Dependency
knowledge is a separate, **optional** verb group, so a backend that models
dependencies natively can expose them and one that cannot says so.

| Verb | Shape | Meaning |
|---|---|---|
| `link` | `link(ref, blocked_by) -> None` | Record that `ref` is blocked by `blocked_by` |
| `unlink` | `unlink(ref, blocked_by) -> None` | Remove that edge |
| `deps` | `deps(ref) -> Dependencies` | Edges both ways: `.blocked_by`, `.blocks` |
| `ready` | `ready(query=None) -> list[Issue]` | Open issues with no OPEN blocker |
| `claim` | `claim(ref, agent_id) -> bool` | Atomically claim; `True` if this agent holds it |
| `release` | `release(ref, agent_id) -> bool` | Release; `False` if this agent does not hold it |

### Capability discovery

Ask, do not probe:

```python
from tracker import CAP_CLAIM, get_tracker

backend = get_tracker("acme/app", repo_root=target_root)
if CAP_CLAIM in backend.capabilities():
    won = backend.claim(ref, agent_id)
else:
    ...  # fall back to the wave lock
```

`capabilities()` returns a frozenset of `CAP_DEPENDENCIES` (link/unlink/deps),
`CAP_READY`, and `CAP_CLAIM`. Feature-detecting means an unsupported verb is a
planned branch rather than an exception path, and it is why callers never need
`try/except NotImplementedError` around a verb.

From the CLI: `python3 scripts/tracker.py --repo-root <root> capabilities <target>`.

### Per-backend semantics

| | `local` | `github` |
|---|---|---|
| `link` / `unlink` / `deps` | native, `blocked_by:` list in YAML frontmatter | emulated, `<!-- BLOCKED-BY ... -->` block in the issue body |
| `ready` | computed client-side | computed client-side |
| `claim` / `release` | **supported**, mutual exclusion via `O_EXCL` | **unsupported**, raises `UnsupportedCapability` |

**Why GitHub declines `claim` rather than emulating it.** GitHub offers no
compare-and-set on assignee, so an assignee-based claim is a read followed by a
write, and two workers racing for the same issue can both come away believing
they won. A claim that does not exclude is worse than no claim, because both
workers then proceed. The backend therefore refuses the verb and reports
`CAP_CLAIM` as absent, so a caller chooses its own exclusion (today, the wave
lock) with its eyes open. This is the same fail-closed posture as the rest of
CW: an unsupported verb is loud, never a silent no-op.

**The per-issue `BLOCKED-BY` block is deliberately distinct** from the
milestone-level `<!-- DEPENDENCIES -->` block that `chief_wiggum.github`
parses. That one describes a whole epic's graph in one place; this one records
the edges of a single issue. Parsing one with the other's reader would silently
mix epic-wide and per-issue scope.

### Rules that hold on every backend

- **Cycles are refused at link time**, not discovered at schedule time. A cycle
  makes every node in it permanently unready, so `ready()` would just return a
  silently shorter list. `link` raises `DependencyCycle` and names the path.
- **A blocker that does not exist does not block.** A deleted or not-yet-created
  blocker would otherwise wedge its dependants forever with nothing to close.
  The same rule applies inside cycle traversal, so linking ahead of an issue's
  creation is allowed.
- **`ready()` lists open work only**, and a closed blocker releases its
  dependants.
- **`link` leaves the rest of the issue alone** — title, body prose, labels and
  epic are untouched.

### Epic grouping and dependency edges

They are independent axes. `group()` records *which* epic an issue belongs to;
dependency edges record *ordering* within (or across) epics. `ready()` does not
filter by epic — pass a query, or filter `members()` yourself, if a caller
wants an epic-scoped ready set.

### Not yet wired

This ticket adds the seam. Rewiring `/plan-epic` to write edges instead of
prose, and `/implement-wave` to schedule from `ready()`, are the payoff
follow-ups. A `BeadsBackend` (native dependencies, readiness and claiming) is
what the seam exists to make worth building.

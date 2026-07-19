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
| `update` | `update(ref, fields: dict) -> Issue` | `fields` may set `title`, `body`, `state` (`"open"`/`"closed"`), `labels` (replaces the set), `assignee`, `epic`. |
| `comment` | `comment(ref, body) -> None` | Adds a comment. Not part of `Issue.body` — verify per-backend (see `tests/test_tracker.py`). |
| `group` | `group(refs: list[str], epic_name) -> None` | Assigns every ref to an epic. |
| `members` | `members(epic_name) -> list[Issue]` | All issues currently in that epic. |

`Issue` is a dataclass: `ref`, `title`, `body`, `state`, `labels`, `assignee`,
`epic` (the grouping key), `url_or_path` (a GitHub URL or a local file path).
`IssueDraft` is the create-time input: `title`, `body`, `labels`, `assignee`,
`epic`.

## Issue refs are URIs

- `gh:owner/repo#42` — GitHub, explicit.
- `local:docs/issues/0042.md` — the local backend, path relative to the target
  repo root.
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

Body markdown goes here. Comments are appended below a `---` divider by
`comment()`; they are not folded into the frontmatter.
```

Every frontmatter value is written with `json.dumps` — JSON is a valid subset
of YAML, so the file is real, parseable YAML frontmatter (openable by any
YAML-aware tool, e.g. Obsidian) without pulling in a `pyyaml` dependency.

IDs auto-increment from the highest existing numeric filename in `docs/issues/`.

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
- **Two-way sync between backends** — a ref lives in exactly one backend.
- **Migrating existing GitHub issues** anywhere.

## CLI

```bash
python3 scripts/tracker.py get gh:acme/app#42
python3 scripts/tracker.py get acme/app#42                      # bare = same as gh:
python3 scripts/tracker.py list acme/app --epic "Epic: Name"
python3 scripts/tracker.py create acme/app --title "Fix bug" --body "..." --label bug
python3 scripts/tracker.py update gh:acme/app#42 --set state=closed
python3 scripts/tracker.py comment gh:acme/app#42 "Looks good"
python3 scripts/tracker.py group "Epic: Name" gh:acme/app#42 gh:acme/app#43
python3 scripts/tracker.py members acme/app "Epic: Name"

# local backend, operating against a target repo checkout:
python3 scripts/tracker.py --repo-root "$TARGET_REPO" list acme/app
python3 scripts/tracker.py --repo-root "$TARGET_REPO" get local:docs/issues/0001.md
```

Output is JSON (an `Issue` dict, or a list of them) for every command that
returns data, matching sibling scripts like `epic_metadata.py`.

## Command migration

`/create-issue` and `/plan-epic` resolve issue refs via `tracker.py` instead of
calling `gh issue`/`gh api` directly (see their command markdown). The
remaining `gh`-calling commands (`/seed`, `/implement`, `/implement-wave`,
`/close-epic`) are a later ticket.

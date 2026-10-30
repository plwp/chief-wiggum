# diskspace.py: where the gigabytes are, and getting them back safely

`scripts/diskspace.py` finds the directories that take up the disk, tags each one
with what it is and how safe it is to remove, and removes the ones you pick. It
uses only the standard library.

```bash
python3 scripts/diskspace.py scan                       # ~ and /private/tmp/claude-<uid>
python3 scripts/diskspace.py scan ~/repos --min-size 200M --json /path/scan.json
python3 scripts/diskspace.py clean --from /path/scan.json --dry-run
python3 scripts/diskspace.py clean --from /path/scan.json
python3 scripts/diskspace.py docker
```

## What `scan` looks at

- Default roots: `~` (which covers `~/.chief-wiggum`) and `/private/tmp/claude-<uid>`
  (Claude Code session scratchpads).
- Size is **on-disk** (`st_blocks * 512`), not apparent size. A sparse `Docker.raw` can
  claim 460 GB and occupy 86 GB; the scan reports 86 GB. Hard-linked files count once.
- Not walked: `~/Library/Mail`, `~/Library/Messages`, `~/.Trash`, Photos libraries,
  other volumes (a different `st_dev`), symlinks, and other apps' sandbox containers
  under `~/Library/Containers` (macOS asks permission per app; `--deep-containers`
  walks them anyway). Docker's container is always walked. Unreadable entries are skipped.
- A row is emitted for every categorised directory of at least `--min-size` (default
  500M), and for any other directory whose size, after its own rows are taken out, is
  still that big. The per-category totals count each byte once.
- The table goes to the terminal; the full scan goes to JSON (`--json`, else
  `$CW_TMP` or `~/.chief-wiggum/tmp/diskspace/`) so `clean` never rescans.
- Git checkouts are asked, read-only, for branch, dirty count, commits on no remote ref,
  and upstream (`git --no-optional-locks status` does not rewrite the index).

Categories: `build-artefact` (node_modules beside a package.json, `target` beside a
Cargo.toml, `.next`, `__pycache__`, `.terraform`, `.pytest_cache`, and `dist`, `build`,
`.venv`/`venv` inside a repo), `cache` (`~/Library/Caches/*`, `~/.cache/*`, go-build,
npm `_cacache`, Go module cache, cargo registry, Xcode DerivedData, ollama models),
`git-worktree` (a `.git` file), `repo-clone` (a `.git` directory), `session-tmp`
(`~/.chief-wiggum/tmp/*`, Claude `scratchpad` dirs), `venv` (a `pyvenv.cfg`), `docker`,
`downloads`, `user-data` (everything else). The rule table is at the top of the script.

## The three safety classes

| Class | What | What `clean` does |
|---|---|---|
| `regenerable` | build artefacts, caches, venvs, terraform providers, worktrees that are clean, pushed and untouched for 24 h, session temp older than 7 days | pre-selected; removed with `shutil.rmtree` (trashing node_modules frees nothing) |
| `check` | worktrees or clones with dirty, unpushed or recent work, session temp under 7 days, Downloads, model caches that are slow to refill (ollama, Hugging Face, whisper, Playwright browsers), anything uncategorised | off by default, reason shown; if selected, **moved** to `~/.Trash/diskspace-<timestamp>/` |
| `ask` | Docker's data, anything under Documents, Desktop or Pictures | listed but not selectable without `--include-ask`; then moved to the Trash |

## Running `clean`

Rows are listed by class and numbered. Type numbers or ranges (`3 5-7`) to toggle,
`none` or `all`, then `go`. The plan (delete / trash / SKIP with the reason, and the
total) is printed first; `--dry-run` stops there, otherwise it asks `Proceed? [y/N]`.
At the end it prints the free-space change measured by `df` before and after.

## What it never does

- Touch anything outside the scan's roots, a scan root itself, an ancestor of a root,
  or a protected folder (`~`, `~/Library`, `~/Documents`, `~/Desktop`, `~/Downloads`,
  `~/repos`, `~/.chief-wiggum`, the cache roots, `~/.Trash`). Symlinks are refused.
- Delete a `check` or `ask` row outright: those go to the Trash, and only on the same
  volume (otherwise the row is skipped).
- Leave a worktree registered: a removed worktree goes through `git worktree remove
  --force` from its main repo, or `git worktree prune` after a move to the Trash.
  Worktrees nested inside a selected directory are handled the same way.
- Remove a worktree whose HEAD has commits on no remote ref, unless its row was a
  `check` row selected explicitly. Git state is re-read at delete time, so a scan that
  saw a worktree clean does not license deleting it after new commits.
- Prune Docker without asking. `docker` reports Docker.raw on-disk vs apparent size,
  gives the engine 20 s to answer `docker system df`, and offers `builder prune -af` and
  `image prune -af` (safe) and `container prune` and `volume prune` (ask), each behind
  y/N. A hung engine is reported as hung, with the one manual step: restart Docker
  Desktop.

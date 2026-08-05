# Contributing to Chief Wiggum

External contributions are welcome — including AI-assisted and fully agent-authored ones. This repo is itself an agentic system, so "an agent wrote this PR" is not a mark against it. What gates a contribution is the same thing that gates every internal change: the work has been **run**, and it meets its issue's acceptance criteria.

## The rules

1. **Link an issue and meet its acceptance criteria.** The issue's ACs are the contract; a PR that ignores them will get changes requested however plausible the diff looks, and closed if it goes stale. No issue yet? Open one first (`templates/issue.md` shape: summary, concrete defect, acceptance criteria). PRs are reviewed against the ACs, not against vibes.

2. **Run what you change — and show it.** Put evidence of execution in the PR body: the test run, a snippet smoke-tested against a fixture, the command and its output. "Looks right" is not evidence. These guidelines exist because a submitted shell snippet crashed on every invocation — it had never been executed once.

3. **Prompt files are executable code.** Everything under `.claude/commands/` and `skills/` is executed verbatim by agents on maintainers' machines. External changes to those files are re-authored by the maintainer before merge — your credit is preserved via commit authorship or a `Co-authored-by:` trailer — and are reviewed as a code-execution surface (including for prompt injection), not as documentation.

4. **Bug fixes ship with a test that pins the failure.** If the defect lives in a prompt-file snippet, the fix belongs in a script under `scripts/` (where a test can hold it), with the prompt file reduced to a call — untestable inline logic is how the defect got in.

5. **Gate scripts carry validation records.** Editing a blocking-capable gate (`scripts/ratchet.py`, `check_traceability.py`, `check_single_writer.py`, …) moves its `--scanner-version` and stales its record under `docs/quality/validation/` — the suite fails until the record is re-authored per `docs/gate-validation.md`. This is by design; budget for it.

6. **Follow the repo's standing conventions.** Helpers are Python, never bash scripts; secrets come from the system keyring, never env vars; everything is project-agnostic — no hardcoded repo names or local paths (resolve via `scripts/env.py` / `scripts/repo.py`). `AGENTS.md` is the harness-neutral instruction set; `CLAUDE.md` is the Claude Code adapter.

7. **Disclose AI assistance.** One line in the PR body is enough ("generated with X, reviewed and executed by me"). Disclosure is never held against a PR — an undisclosed, unexecuted generation is what burns trust.

8. **Run the full suite before opening the PR.** `python3 -m pytest tests/` — CI runs it, but a PR opened red wastes the round-trip.

## What stops a PR at the door

A PR that misses any of these gets a comment naming the specific gap and a changes-requested review — the diff itself won't be reviewed until the gap is addressed, and the PR is closed if it goes stale without movement:

- No linked issue, or the linked issue's acceptance criteria are not met.
- No evidence the change was ever executed.
- Changes to prompt files or gate scripts with no accompanying test.

Speed is not a substitute for any of these. A fast, plausible diff that fails them costs the maintainer more than no PR — it triggers the full external-change review (execution check, injection review, provenance) with nothing usable at the end.

## Credit

Genuine reports and first attempts are valued even when the final fix is re-authored: the maintainer preserves contribution credit via your commit as the branch base or a `Co-authored-by:` trailer on the squashed commit, and the superseding PR links yours.

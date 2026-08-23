# Ship - PR Creation with Mermaid Diagrams

Create a well-documented pull request with mermaid architecture diagrams, test evidence, and full context.

## Usage
```
/ship [--repo owner/repo] [--base main] [--issue number]
```

## Parameters
- `--repo`: Target repository (default: current repo)
- `--base`: Base branch (default: main)
- `--issue`: Issue number to link (optional)

## Disclosure (#317)

The PR body drafted in Step 5 carries an AI-authorship disclosure line
automatically — `draft_pr.py` builds it via `chief_wiggum.shipping.build_pr_body`,
which appends it. If any commit on this branch was authored outside `/implement`
(so it never ran through that workflow's Disclosure step), append the trailer
before pushing: `"${CW_PY:-python3}" "$CW_HOME/scripts/ai_disclosure.py" commit-trailer --file <msg>`.
See `docs/ai-act-posture.md`.

## Workflow

### Step 0: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# Pin the interpreter CW scripts run under. A bare `python3` is whatever
# the shell resolves, so a Homebrew bump silently strands keyring /
# jsonschema / google-genai and kills consults mid-phase (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
CW_TMP=$("${CW_PY:-python3}" "$CW_HOME/scripts/env.py" tmp)
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
```

### Step 1: Analyse the diff

Get the full diff and commit history for the current branch:

```bash
git log --oneline $DEFAULT_BRANCH..HEAD
git diff --stat $DEFAULT_BRANCH...HEAD
git diff $DEFAULT_BRANCH...HEAD
```

Understand:
- What files were changed and why
- The scope of changes (new files, modified files, deleted files)
- The commit history narrative

### Step 2: Generate mermaid diagrams

Based on the diff analysis, generate appropriate mermaid diagrams:

**Color palette** — all mermaid diagrams must use this palette via `%%{init:}%%` theme overrides:

```
#003f5c  (deep navy)
#2f4b7c  (slate blue)
#665191  (muted purple)
#a05195  (plum)
#d45087  (rose)
#f95d6a  (coral)
#ff7c43  (tangerine)
#ffa600  (amber)
```

Apply it by adding a theme init block at the top of every mermaid diagram:

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
```

Use `style` directives to assign specific palette colours to nodes based on their role:
- `#003f5c` / `#2f4b7c` — existing infrastructure, databases, external services
- `#665191` / `#a05195` — modified components
- `#d45087` / `#f95d6a` — new components added in this PR
- `#ff7c43` / `#ffa600` — user-facing / entry points

**Component Relationship Diagram** (always include):
Show the components that were changed and how they relate to each other.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333'}}}%%
graph TD
    A[Modified Component]:::modified --> B[Dependency]:::existing
    A --> C[New Component]:::new
    C --> D[Existing Service]:::existing
    classDef existing fill:#003f5c,stroke:#2f4b7c,color:#fff
    classDef modified fill:#665191,stroke:#a05195,color:#fff
    classDef new fill:#d45087,stroke:#f95d6a,color:#fff
    classDef entry fill:#ff7c43,stroke:#ffa600,color:#fff
```

**Data Flow Diagram** (include if data flow changed):
Show how data moves through the modified components.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#003f5c', 'primaryTextColor': '#fff', 'primaryBorderColor': '#2f4b7c', 'secondaryColor': '#665191', 'tertiaryColor': '#a05195', 'lineColor': '#2f4b7c', 'textColor': '#333', 'actorTextColor': '#fff', 'actorBkg': '#003f5c', 'actorBorder': '#2f4b7c', 'activationBorderColor': '#d45087', 'activationBkgColor': '#f95d6a', 'signalColor': '#2f4b7c'}}}%%
sequenceDiagram
    participant U as User
    participant A as API
    participant D as Database
    U->>A: Request
    A->>D: Query
    D-->>A: Result
    A-->>U: Response
```

**Before/After** (include if architecture changed):
Show the structural change.

Guidelines for diagrams:
- Keep them focused on what changed, not the entire system
- Use descriptive node labels
- Highlight new components vs modified ones using the classDef colour roles above
- Maximum 15 nodes per diagram (simplify if larger)

### Step 3: Verify and compile test evidence

**Always re-run tests before shipping.** Do not rely on stale results from earlier in the session — code may have changed since tests last ran.

The verification runner detects the project type (Go/Node/Python/Make/Docker/Playwright), runs the requested profiles, and emits structured evidence (command, exit code, duration, log tail) for the PR body:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/run_verification.py" --repo "$(git rev-parse --show-toplevel)" --profile test,lint --markdown
```

It exits non-zero if any step fails. **If tests fail, stop and fix them** — do not create a PR with failing tests. Use `--dry-run` first to see the planned commands, and add `build`/`smoke` to `--profile` when relevant.

If browser-use screenshots exist, reference them.

### Step 4: Price the build

Fold this session's token spend into the factory ledger and render the measured actual (`docs/ticket-cost.md`), so the PR carries the same cost evidence `/implement` produces.

**Skip this step entirely when no issue number is known.** `--ticket` attribution is guarded, never blind — with nothing to attribute to, do not ingest with a ticket tag; the PR then carries no cost section, which is honest.

`/ship` is standalone: there is no `/implement` build-start stamp to window the ingest to. Window it to this branch's first commit instead — mechanical and prompt-free, but best-effort: work before the branch existed is not counted, and unrelated sessions in this repo inside the window may be. That caveat ships **in the PR body**, appended to the cost section below, not just here.

```bash
branch_start_ts=$(git log "$DEFAULT_BRANCH..HEAD" --format=%ct | tail -1)
"${CW_PY:-python3}" "$CW_HOME/scripts/factory_log.py" ingest-claude-transcripts \
  --repo "$owner_repo" --ticket "$issue_number" \
  --since-ts "$branch_start_ts"
"${CW_PY:-python3}" "$CW_HOME/scripts/ticket_cost.py" actual \
  --repo "$owner_repo" --ticket "$issue_number" \
  --format markdown > "$CW_TMP/implementation-cost.md"
cat >> "$CW_TMP/implementation-cost.md" <<'EOF'

> **Attribution**: standalone `/ship` has no per-ticket build-start stamp, so this
> ingest is windowed to the branch's first commit. Work done before the branch
> existed is not counted; unrelated sessions in this repo inside the window may be.
EOF
```

If the issue body carries a `Nominal cost: ~$X.XX` line (stamped by `/create-issue`), add `--estimate X.XX` to the `actual` call so the section shows estimate-vs-actual variance. If the output says **Unmetered**, keep it verbatim — that is absence of telemetry, never a $0 build; do not drop the section or render it as $0.

### Step 5: Draft the PR

Assemble the PR body with the tested helper. It folds in the verification evidence, optional model-conformance/UX manifests, the implementation-cost section from Step 4, and a Mermaid diagram (themed with the shared palette automatically), validates the required sections, and can print the `gh pr create` command:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/draft_pr.py" \
  --issue "$issue_number" --title "$title" --summary "$summary" \
  --change "Change 1" --change "Change 2" \
  --mermaid-file "$CW_TMP/architecture.mmd" \
  --verification "$CW_TMP/verification.json" \
  --implementation-cost "$CW_TMP/implementation-cost.md" \
  --base "$base_branch" --out "$CW_TMP/pr-body.md" --print-command
```

(Omit `--implementation-cost` when Step 4 was skipped — the section is then omitted.) The Mermaid palette no longer needs to be hand-copied — `draft_pr.py` injects the `%%{init}%%` theme (use `--mermaid-sequence` for sequence diagrams). The diagram *content* is still yours to author. It exits non-zero if a required section (Summary, Changes, Test Evidence) is missing.

### Step 6: Preview and confirm

Show the user the full PR body and ask:
1. Does the summary capture it?
2. Are the diagrams accurate?
3. Any additional context to add?
4. Ready to create?

### Step 7: Create the PR

```bash
git push -u origin HEAD
```

```bash
gh pr create \
  --repo "$owner_repo" \
  --title "$title" \
  --body-file "$CW_TMP/pr-body.md" \
  --base "$base_branch"
```

If an issue was specified, it should be linked via "Closes #N" in the body.

Then, if Step 4 priced the build, **record the calibration point** so standalone PRs feed the estimator too. Read the Effort size (`S|M|L|XL`) from the issue body's Labels section; omit `--effort` if the issue has none, and pass `--estimate` when the issue carried a nominal-cost figure:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/ticket_cost.py" record \
  --repo "$owner_repo" --ticket "$issue_number" --effort "$effort"
```

### Step 8: Report

Show:
- PR URL
- PR number
- Files changed count
- Suggest: "Want me to request reviewers or add labels?"

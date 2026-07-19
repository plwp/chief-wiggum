# Create Issue - Well-Structured GitHub Issue Creation

Create a well-structured GitHub issue with clear title, description, acceptance criteria, and labels.

## Usage
```
/create-issue <owner/repo> [description]
```

## Parameters
- `owner/repo`: Target GitHub repository
- `description` (optional): Free-text description of what needs to be done. If omitted, start an interactive session.

## Workflow

### Step 0: Resolve CW_HOME and the target repo root

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
target_root=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

`$target_root` is the local checkout of the target repo. Every `tracker.py`
call below passes `--repo-root "$target_root"` — that is how the target repo's
`docs/cw/tracker.json` backend config is honored (and where the `local`
backend stores issue files). Without it, tracker.py would resolve config from
the current working directory instead of the target repo.

### Step 1: Gather requirements

If a description was provided, parse it. Otherwise, ask the user:
1. What type of issue? (bug, feature, enhancement, chore)
2. Brief description of what needs to be done
3. Why is this needed? (user impact, business value)
4. Any technical details or constraints?

### Step 2: Draft the issue

Using the template at `$CW_HOME/templates/issue.md`, fill in:

- **Title**: Clear, concise, imperative. Examples:
  - Bug: "Fix crash when submitting empty form"
  - Feature: "Add email notifications for booking confirmation"
  - Chore: "Upgrade Go to 1.23"

- **Summary**: One sentence.

- **User Story** (for features):
  - As a [role], I want [capability] so that [benefit]

- **Acceptance Criteria**: Specific, **mechanically verifiable** checkboxes. Write 3-6 criteria that define "done". Each criterion must be something an automated system can independently verify — not vague ("works well") but concrete ("GET /health returns 200 with `{"status":"ok"}`"). Good criteria answer: "How would I prove this is done without reading the code?"

- **Suggested Fix**: A single, concrete implementation approach — not a list of options. The issue author has the most context (full codebase analysis, domain understanding, cross-ticket awareness). The implementing agent has less context and will default to the simplest option, which is often wrong. **Make the design decision here.** If genuinely uncertain, consult Codex/Gemini for a second opinion before writing the ticket — don't defer the decision to the implementor.

  Bad: "Either: 1. Remove it, 2. Use it for X"
  Good: "Use `rejected` instead of `cancelled` when a booking request is declined. This gives semantic separation for reporting and audit trail."

- **Technical Notes**: Implementation hints, affected files, API changes. Only if relevant.

- **Out of Scope**: What this ticket does NOT cover. Important for preventing scope creep.

- **Labels**: Suggest appropriate labels based on type and severity.

### Step 3: Preview and confirm

Show the user the full issue markdown and ask:
1. Does the title capture it?
2. Are the acceptance criteria complete?
3. Any labels or milestone to add?
4. Ready to create?

### Step 4: Create the issue

Resolve the issue ref via `tracker.py` instead of calling `gh issue` directly —
this is what makes the workflow backend-agnostic (GitHub today, `local` or
others per `docs/cw/tracker.json` in the target repo). See `docs/tracker.md`
for the full interface.

Create the issue exactly **once**:

```bash
ref=$(python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" create "$owner_repo" \
  --title "$title" \
  --body "$body" \
  --label "$label1" --label "$label2")
```

If a milestone/epic was specified, set it on the issue you just created — do
**not** run a second create (the backend maps `epic` to a GitHub milestone or
a local frontmatter `epic` field as appropriate):

```bash
if [ -n "$milestone" ]; then
  python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" update "$ref" --set "epic=$milestone"
fi
```

### Step 5: Report

Fetch the created issue back to show its URL/path and confirm the fields landed:

```bash
python3 "$CW_HOME/scripts/tracker.py" --repo-root "$target_root" get "$ref"
```

Show the created issue's `url_or_path` and ref. Ask if the user wants to:
- Create another issue
- Start implementing this one (`/implement owner/repo#N` for the `github`
  backend; `/implement "$ref"` otherwise)

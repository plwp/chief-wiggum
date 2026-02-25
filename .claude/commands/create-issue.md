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

### Step 1: Gather requirements

If a description was provided, parse it. Otherwise, ask the user:
1. What type of issue? (bug, feature, enhancement, chore)
2. Brief description of what needs to be done
3. Why is this needed? (user impact, business value)
4. Any technical details or constraints?

### Step 2: Draft the issue

Using the template at `~/repos/chief-wiggum/templates/issue.md`, fill in:

- **Title**: Clear, concise, imperative. Examples:
  - Bug: "Fix crash when submitting empty form"
  - Feature: "Add email notifications for booking confirmation"
  - Chore: "Upgrade Go to 1.23"

- **Summary**: One sentence.

- **User Story** (for features):
  - As a [role], I want [capability] so that [benefit]

- **Acceptance Criteria**: Specific, testable checkboxes. Write 3-6 criteria that define "done".

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

```bash
gh issue create \
  --repo "$owner_repo" \
  --title "$title" \
  --body "$body" \
  --label "$labels"
```

If a milestone was specified:
```bash
gh issue edit "$issue_number" --repo "$owner_repo" --milestone "$milestone"
```

### Step 5: Report

Show the created issue URL and number. Ask if the user wants to:
- Create another issue
- Start implementing this one (`/implement owner/repo#N`)

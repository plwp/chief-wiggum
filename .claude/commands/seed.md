# Seed - Architecture Brainstorm & Issue Seeding

Interactive brainstorming session that explores a project's requirements, establishes architecture decisions with multi-AI consultation, and seeds the backlog with well-structured GitHub issues.

## Usage
```
/seed <owner/repo>
```

## Parameters
- `owner/repo`: Target GitHub repository

## Workflow

### Step 0: Resolve paths and session temp

```bash
CW_HOME=$(python3 -c "from pathlib import Path; print(Path('__file__').resolve().parent.parent.parent)" 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
```

Resolve the target repo:
```bash
TARGET_DIR=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 1: Understand the project

Read all available project documentation in the target repo:
- README, CLAUDE.md, REQUIREMENTS.md, or similar planning docs
- Any existing architecture docs, brainstorming notes, transcripts
- Existing code structure (if any)

If the repo is empty or has no requirements docs, tell the user and suggest they run `/transcribe` first to capture a discovery session.

**Summarise** what you've found: what the project is, what's been decided, what's still open.

### Step 2: Explore sister projects (if any)

Ask the user: "Are there any existing repos I should explore for patterns, tech stack, or lessons learned?"

If yes, send an **Explore sub-agent** (`subagent_type: "Explore"`, thoroughness: "very thorough") in the background to analyse the sister repo. The agent should report on:
- Tech stack and architecture patterns
- What works well (patterns worth replicating)
- What's painful (patterns to avoid)
- Database, auth, deployment, testing patterns
- AI/LLM integration (if any)

Continue the conversation while the agent works.

### Step 3: Interactive architecture brainstorm

Work through the key architecture decisions with the user. Don't assume — ask. Cover:

1. **Backend language/framework** — Consider: what will the AI writing the code produce best? What ecosystem fits the problem domain?
2. **Frontend stack** — If the user has a preference, use it. If not, recommend modern defaults.
3. **Database** — Schema flexibility vs relational integrity. Audit trail requirements. Multi-tenancy model.
4. **AI/LLM integration** — Which model provider? RAG vs context stuffing? Structured output? Streaming?
5. **Infrastructure & deployment** — Cloud provider, cost optimisation, IaC.
6. **Authentication & authorisation** — Who are the users? What roles? Any anonymous flows?
7. **Streaming & real-time** — SSE, WebSockets, or polling? What needs to be real-time?
8. **Document/file generation** — Any server-side rendering? What formats?
9. **Mobile vs desktop** — What's the primary device?
10. **Multi-tenancy** — Needed from day one? What model?

Incorporate findings from the sister project exploration (Step 2) as they arrive.

Don't try to cover everything in one pass. Let the conversation flow naturally. The user may have strong opinions on some topics and none on others.

### Step 4: Capture architecture decisions

Once the key decisions are made, write them to `$CW_TMP/architecture-decisions.md`:
- What we're building (one paragraph)
- Each decision with rationale
- Patterns carried forward from sister projects
- Lessons learned to apply
- Open questions (deferred)

Show the user a summary and confirm the decisions look right before proceeding.

### Step 5: Multi-AI consultation

Write a consultation prompt to `$CW_TMP/consultation-prompt.md` asking for a critical review of the architecture decisions. The prompt should ask:
1. What's solid?
2. What concerns you?
3. What's missing?
4. Specific technical feedback on the key decisions
5. Cost optimisation traps

Fire off **Gemini and Codex in parallel** using `consult_ai.py`:
```bash
python3 "$CW_HOME/scripts/consult_ai.py" gemini "$CW_TMP/consultation-prompt.md" \
  --context "$CW_TMP/architecture-decisions.md" -o "$CW_TMP/review-gemini.md"

python3 "$CW_HOME/scripts/consult_ai.py" codex "$CW_TMP/consultation-prompt.md" \
  --context "$CW_TMP/architecture-decisions.md" -o "$CW_TMP/review-codex.md"
```

Run both in the background. Continue the conversation or work on other steps while waiting.

### Step 6: Synthesise AI feedback

When both reviews are back:
1. Read both reviews
2. Present a **synthesis** to the user: what both agree on, unique insights from each, top 3 recommended changes
3. Fold accepted changes into the architecture decisions document
4. Update `$CW_TMP/architecture-decisions.md` with the revised decisions

### Step 7: Commit architecture decisions

Copy the finalised architecture decisions to the target repo as `ARCHITECTURE.md`. Update `CLAUDE.md` if the tech stack has changed from what was previously documented.

Commit and push:
```bash
cd "$TARGET_DIR"
git add ARCHITECTURE.md CLAUDE.md
git commit -m "Add architecture decisions from seed session"
git push
```

### Step 8: Seed the backlog

Based on the architecture decisions and requirements docs, plan out the initial issues. Organise by:
- **Foundation** — Repo scaffold, infrastructure, database, auth, audit trail
- **Core engine** — AI pipeline, knowledge base, streaming
- **Milestone 1** — First user-facing value (often a free tier or demo)
- **Milestone 2** — Core paid features
- **Milestone 3** — Advanced features, ongoing workflows

For each issue, define:
- Clear title (imperative voice)
- Description with context
- Acceptance criteria (checkboxes)
- Labels (create labels first if they don't exist)

**Always include these repo-specific skill issues in the foundation set:**
- **Create /test skill** — A `.claude/commands/test.md` skill for the target repo that runs the full test suite (backend + frontend + E2E), reports results, and optionally fixes failures. Should detect the project's test framework and run the appropriate commands.
- **Create /deploy skill** — A `.claude/commands/deploy.md` skill for the target repo that handles deployment to the target environment (build, push, deploy, verify health). Should match the project's infrastructure (e.g. Cloud Run, Vercel, AWS, etc.) and include smoke tests post-deploy.

These go in the first sprint — they're needed as soon as there's code to test and deploy.

Run issue creation in a **sub-agent** (`subagent_type: "general-purpose"`) to keep the main context clean. The sub-agent should use `gh issue create` for each issue and `gh label create` for any new labels.

### Step 9: Report

Present the user with:
1. Link to the ARCHITECTURE.md commit
2. Summary of issues created (count by group, with issue numbers/URLs)
3. Suggested next steps:
   - `/triage owner/repo` to prioritise the backlog
   - `/plan-sprint owner/repo` to plan the first sprint
   - `/implement owner/repo#N` to start building

## Key Principles

- **This is a conversation, not a checklist.** The brainstorming should feel natural. Skip steps that aren't relevant, go deeper on topics that matter.
- **User decides, AI advises.** Present options and trade-offs. Don't railroad into a specific stack.
- **Multi-AI consultation adds genuine value.** Different models catch different things. The synthesis is more valuable than any single review.
- **Capture everything.** Architecture decisions, session summary, and AI reviews are all persisted. Nothing should be lost if the session ends.
- **Issues should be actionable from day one.** Each issue should have enough context that someone (human or AI) could pick it up and start working without additional conversation.

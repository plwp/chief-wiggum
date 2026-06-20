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
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
CW_TMP=$(python3 "$CW_HOME/scripts/env.py" tmp)
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

If yes, send a background **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) to analyse the sister repo; it signals completion by writing its findings artifact. *Claude Code adapter:* `subagent_type: "Explore"`, thoroughness "very thorough", in the background. The worker should report on:
- Tech stack and architecture patterns
- What works well (patterns worth replicating)
- What's painful (patterns to avoid)
- Database, auth, deployment, testing patterns
- AI/LLM integration (if any)

Continue the conversation while the agent works.

### Step 2.5: Ingest domain ground truth

Most seed failures are not bad architecture — they're architecture built on **guessed facts**. Before brainstorming, find out what real sources of truth exist and ingest them. Ask the user:

1. **Existing data model** — "Does this product read from or write to an existing data source? Where does its truth live?" If yes, ingest it **before any data contracts are written**:
   - The semantic/modeling layer (dbt, Dataform, LookML, a metrics store) — canonical metric definitions, dimensions, measures
   - The physical schema — introspect it (run `\d`/`SHOW CREATE TABLE`/`db.collection.findOne()` against a real instance, or read migration files). Never trust table/column names from memory or docs alone.
   - The transformation repo's **history and PRs** — deprecations, frozen sources, unit/locale normalisation rules, test-record exclusions, dedup patterns, known-bad data. Send an **explorer worker** (contract: `docs/worker-contracts.md#read-only-explorer-worker`) over the repo history; caveats live in PR descriptions, not schemas.
2. **Real use cases** — "Where do real user requests live?" (issue tracker, support queue, team chat channels, existing dashboards). If accessible, mine them with a worker to derive:
   - The question patterns the product must answer (these become golden eval cases for `/architect` traceability)
   - The dimensions/measures/entities users actually slice by
   - Domain caveats stated by the team ("ignore test accounts", "EU revenue is net of VAT")
   - Demo scenarios and UI suggestion prompts/empty-state copy grounded in real requests

Write the findings to `$CW_TMP/domain-context.md` with **citations** (file paths, PR links, ticket links) for every claim. Facts that cannot be confirmed against a real source are written with an explicit `TBD:` marker — these gate dependent work later (`/architect` and `/implement-wave` run `check_unresolved.py` against artifacts).

If the product has no existing data source and no usage history (true greenfield), note that in `domain-context.md` and move on — don't invent ceremony.

### Step 3: Interactive architecture brainstorm

Work through the key architecture decisions with the user. Don't assume — ask. Cover:

1. **Backend language/framework** — Consider: what will the AI writing the code produce best? What ecosystem fits the problem domain?
2. **Frontend stack** — If the user has a preference, use it. If not, recommend modern defaults.
3. **Design source & brand** — the most important UI question, asked FIRST, not as an afterthought:
   - "Is there an existing design system, component library, or brand we must match?"
   - "Is this replicating or extending an existing product? If so, where does its UI live?"
   - If yes to either, **ingest the design source as a first-class input**:
     - Extract design tokens (palette including brand gradients, typography scale, spacing, radii) from the existing CSS/theme files or brand kit
     - Prefer **adopting** the existing component library over hand-rolling
     - Capture reference screenshots/URLs of the product to match; for replication projects, clone or link the existing UI source as a styling reference
   - If genuinely net-new, don't design it in this conversation — record audience, tone preferences, and any constraints, and route the actual design work to `/design` (divergent rendered mockups → human choice → extracted tokens). "Default component-library theme" is not a decision — it's how brandless admin-tool UIs ship (see dogeared-coach retro).
   - Record the design source in the architecture decisions — `/design` turns it into `docs/design/` (binding tokens, approved mockups, reference screenshots), `/architect` folds that into the `design` section of `ui-spec.json`, and the design-fidelity gate in `/implement` reviews rendered screenshots against it.
4. **Database** — Schema flexibility vs relational integrity. Audit trail requirements. Multi-tenancy model.
5. **AI/LLM integration** — Which model provider? RAG vs context stuffing? Structured output? Streaming?
6. **Infrastructure & deployment** — Cloud provider, cost optimisation, IaC.
7. **Authentication & authorisation** — Who are the users? What roles? Any anonymous flows?
8. **Streaming & real-time** — SSE, WebSockets, or polling? What needs to be real-time?
9. **Document/file generation** — Any server-side rendering? What formats?
10. **Mobile vs desktop** — What's the primary device?
11. **Multi-tenancy** — Needed from day one? What model?

Incorporate findings from the sister project exploration (Step 2) as they arrive.

Don't try to cover everything in one pass. Let the conversation flow naturally. The user may have strong opinions on some topics and none on others.

### Step 4: Capture architecture decisions

Once the key decisions are made, write them to `$CW_TMP/architecture-decisions.md`:
- What we're building (one paragraph)
- Each decision with rationale
- Patterns carried forward from sister projects
- Lessons learned to apply
- Open questions (deferred) — anything that must be confirmed against an external source gets a `TBD:` marker so downstream gates catch it

Show the user a summary and confirm the decisions look right before proceeding.

### Step 5: Multi-AI consultation

Write a consultation prompt to `$CW_TMP/consultation-prompt.md` asking for a critical review of the architecture decisions. The prompt should ask:
1. What's solid?
2. What concerns you?
3. What's missing?
4. Specific technical feedback on the key decisions
5. Cost optimisation traps

Fire the `explorer` quorum (providers run in parallel, with retries + output validation) using `consult_ai.py`:
```bash
python3 "$CW_HOME/scripts/consult_ai.py" --role explorer "$CW_TMP/consultation-prompt.md" \
  --context "$CW_TMP/architecture-decisions.md" --output-dir "$CW_TMP/consult"
```

Responses land at `$CW_TMP/consult/explorer-<provider>.md` with status in `explorer-manifest.json`. Continue the conversation or work on other steps while waiting.

### Step 6: Synthesise AI feedback

When both reviews are back:
1. Read both reviews
2. Present a **synthesis** to the user: what both agree on, unique insights from each, top 3 recommended changes
3. Fold accepted changes into the architecture decisions document
4. Update `$CW_TMP/architecture-decisions.md` with the revised decisions

### Step 7: Commit architecture decisions

Copy the finalised architecture decisions to the target repo as `ARCHITECTURE.md`. If Step 2.5 produced domain context, copy it to `docs/domain-context.md` — `/architect` loads it before writing data contracts. Update `CLAUDE.md` if the tech stack has changed from what was previously documented.

Commit and push:
```bash
cd "$TARGET_DIR"
mkdir -p docs
cp "$CW_TMP/domain-context.md" docs/domain-context.md 2>/dev/null || true
git add ARCHITECTURE.md CLAUDE.md docs/domain-context.md
git commit -m "Add architecture decisions from seed session"
git push
```

### Step 8: Seed the backlog

Based on the architecture decisions, domain context, and requirements docs, plan out the initial issues. Where Step 2.5 mined real use cases, fold them in: derived question patterns become acceptance criteria and golden eval cases, and team-stated caveats become explicit constraints on the relevant issues. Organise by:
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

**Always include these repo-specific foundation items:**
- **Copy `pre-merge-check.sh`** — Copy `$CW_HOME/templates/pre-merge-check.sh` to `scripts/pre-merge-check.sh` in the target repo. This language-agnostic script auto-detects project layers (Go, Node, Python, Rust) and runs their test/lint/build commands. It serves as the CI gate for repos without branch protection. Reference it in the target repo's CLAUDE.md.
- **Create /test skill** — A `.claude/commands/test.md` skill for the target repo that runs the full test suite (backend + frontend + E2E), reports results, and optionally fixes failures. Should detect the project's test framework and run the appropriate commands.
- **Create /deploy skill** — A `.claude/commands/deploy.md` skill for the target repo that handles deployment to the target environment (build, push, deploy, verify health). Should match the project's infrastructure (e.g. Cloud Run, Vercel, AWS, etc.) and include smoke tests post-deploy.

These go in the first sprint — they're needed as soon as there's code to test and deploy.

Run issue creation in an **issue-authoring worker** (contract: `docs/worker-contracts.md#issue-authoring-worker`) to keep the orchestrator context clean. *Claude Code adapter:* `subagent_type: "general-purpose"`. The worker should use `gh issue create` for each issue and `gh label create` for any new labels.

### Step 9: Report

Present the user with:
1. Link to the ARCHITECTURE.md commit
2. Summary of issues created (count by group, with issue numbers/URLs)
3. Suggested next steps:
   - `/design owner/repo` to produce the product design (mockups → human choice → `docs/design/`) — for any product with a UI, run this before architecting epics
   - `/plan-epic owner/repo` to group issues into an epic with dependency ordering
   - `/architect owner/repo --epic "Epic: [Name]"` to define contracts and invariants
   - `/implement owner/repo#N` to start building

## Key Principles

- **This is a conversation, not a checklist.** The brainstorming should feel natural. Skip steps that aren't relevant, go deeper on topics that matter.
- **User decides, AI advises.** Present options and trade-offs. Don't railroad into a specific stack.
- **Multi-AI consultation adds genuine value.** Different models catch different things. The synthesis is more valuable than any single review.
- **Capture everything.** Architecture decisions, session summary, and AI reviews are all persisted. Nothing should be lost if the session ends.
- **Issues should be actionable from day one.** Each issue should have enough context that someone (human or AI) could pick it up and start working without additional conversation.

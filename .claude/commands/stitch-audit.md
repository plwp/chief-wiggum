# Stitch Audit — Cross-Layer Data Flow Analysis

Traces a feature's data flow across the full stack — frontend forms, API handlers, database operations, admin views — and flags where fields get lost, names drift, validation diverges, or intent dies at layer boundaries.

No existing tool does this. Contract testing covers one boundary. OpenAPI codegen covers spec-to-types. This skill fills the gap: **cross-layer, cross-language, full-trace analysis**.

## When to use

- After an AI agent implements a full-stack feature (the primary use case)
- Before shipping a PR that touches multiple layers
- When debugging "the form sends X but the API doesn't accept it" issues
- Periodic audit of convention consistency across the codebase

## Usage

```
/stitch-audit <owner/repo> --trace <keyword>
/stitch-audit <owner/repo> --patterns [path]
```

## Parameters

- `owner/repo`: Target GitHub repository (e.g., `plwp/dgrd`)
- `--trace <keyword>`: Trace a feature's data flow across all layers (e.g., `waitlist`, `booking`, `psychosocial`)
- `--patterns [path]`: Scan for convention inconsistencies, optionally scoped to a sub-path (e.g., `backend/`)

## Autonomy

**Run to completion without pausing.** This is an audit/analysis skill — no human-in-the-loop checkpoints. Present the final report and let the user decide what to act on.

---

## Workflow — Trace Mode (`--trace <keyword>`)

### Step 1: Resolve paths

```bash
CW_HOME=$(python3 "$(dirname "$0")/../../scripts/repo.py" home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CW_TMP="$HOME/.chief-wiggum/tmp/$(uuidgen | tr '[:upper:]' '[:lower:]')"
mkdir -p "$CW_TMP"
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

### Step 2: Discovery + Extraction

Run the extraction orchestrator to find and parse all schemas related to the keyword:

```bash
python3 "$CW_HOME/scripts/stitch_extract.py" "$TARGET_REPO" --trace "$keyword" -o "$CW_TMP/extraction.json"
```

This uses the pluggable extractor architecture — whichever extractors match the repo's tech stack run automatically (Go+MongoDB, TypeScript, etc.).

After extraction completes, report what was found:
- Number of schemas extracted per layer
- Which extractors were active
- Any layers with zero results (potential gap)

If extraction finds nothing, stop and report: "No schemas found for keyword '$keyword'. Try a different keyword or check that the feature exists in this repo."

### Step 3: Cross-boundary diffing

Run the stack-agnostic differ on the extraction output:

```bash
python3 "$CW_HOME/scripts/stitch_diff.py" "$CW_TMP/extraction.json" --format json -o "$CW_TMP/findings.json"
python3 "$CW_HOME/scripts/stitch_diff.py" "$CW_TMP/extraction.json" --format text -o "$CW_TMP/findings.txt"
```

Read `$CW_TMP/findings.txt` for a quick overview. Read `$CW_TMP/findings.json` for structured data.

If there are zero findings, report "Clean — no mismatches detected" and skip Steps 4-5.

### Step 4: Git provenance

For BREAK/WARN findings, trace how each break was introduced:

```bash
python3 "$CW_HOME/scripts/stitch_provenance.py" "$CW_TMP/findings.json" --repo "$TARGET_REPO" --gh-repo "$owner_repo" -o "$CW_TMP/provenance.json"
```

This enriches each finding with:
- The commit SHA that introduced each side of the break
- The PR that contained the commit
- Issues linked to the PR (via `Closes #N`)
- Analysis: same_commit / same_pr / same_issue / different_work_streams

### Step 5: Gemini semantic analysis

Build the analysis prompt from the template:

```bash
PROMPT=$(cat "$CW_HOME/templates/stitch-audit-prompt.md")
```

Replace placeholders:
- `{{KEYWORD}}` → the trace keyword
- `{{REPO}}` → owner/repo
- `{{EXTRACTION_JSON}}` → contents of `$CW_TMP/extraction.json`
- `{{DIFF_REPORT}}` → contents of `$CW_TMP/findings.txt`
- `{{PROVENANCE}}` → contents of `$CW_TMP/provenance.json`

Write the filled prompt to `$CW_TMP/stitch-prompt.md`, then consult Gemini:

```bash
python3 "$CW_HOME/scripts/consult_ai.py" gemini "$CW_TMP/stitch-prompt.md" -o "$CW_TMP/stitch-gemini.md"
```

If Gemini times out, retry once. If it fails again, proceed without it — the automated findings are still valuable.

### Step 6: Report

Present the final report to the user, structured as:

---

#### Stitch Audit Report: `<keyword>` in `<owner/repo>`

**Extractors active**: (list)
**Schemas found**: (count by layer)
**Boundaries checked**: (list of layer pairs that had data on both sides)

##### BREAK (data loss / type mismatch)
For each:
- What: the mismatch
- Where: source file:line → target file:line
- Provenance: how it was introduced (commit/PR/issue)
- Fix: concrete action

##### WARN (naming / validation drift)
Same format.

##### INFO (dead fields / conventions)
Same format, more concise.

##### Gemini Analysis
Include the Gemini response (Summary, Critical Findings, Observations, Verdict).

---

## Workflow — Patterns Mode (`--patterns [path]`)

### Step 1: Resolve paths

Same as trace mode.

### Step 2: Convention scan

```bash
python3 "$CW_HOME/scripts/stitch_extract.py" "$TARGET_REPO" --patterns "$scan_path" -o "$CW_TMP/patterns.json"
```

### Step 3: Report

Present the pattern analysis:
- **Naming styles**: Distribution of snake_case / camelCase / PascalCase across layers
- **Tag coverage**: Percentage of Go struct fields with json/bson tags
- **Schema definition styles**: Interface vs Zod ratio, consistency
- **Inconsistencies**: Any layer or file that breaks the dominant convention

Flag specific files that diverge from the codebase norm. No provenance or Gemini analysis needed — this is a quick convention check.

---

## Known Limitations

This is a Tier 1 regex-based analysis. It catches 80-85% of issues — good enough for an audit tool. Known gaps:

- Embedded Go structs (anonymous fields) — tags inherited but not inlined
- TypeScript `Pick<T, K>`, `Omit<T, K>`, mapped types — not resolved
- Multi-file type composition (`type A extends B` across files) — not traced
- Complex nested `bson.M` inside `$set` / `$push` — partially captured
- Zod `.transform()` / `.pipe()` chains — partially captured

False negatives are acceptable. False positives should be rare.

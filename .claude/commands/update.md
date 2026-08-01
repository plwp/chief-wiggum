# Update - Refresh Model & Library Reference

Fetch the latest AI model IDs and library versions, update `models.md`, and push to the repo.

## Usage
```
/update
```

## Workflow

### Step 0: Resolve CW_HOME

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
```

### Step 1: Fetch latest model information

Research the current state of each provider's models by checking their official sources:

**Claude (Anthropic):**
- Check https://docs.anthropic.com/en/docs/about-claude/models for latest model IDs
- Note any new models, deprecated models, or ID changes
- Get current Vertex AI and Bedrock IDs

**Gemini (Google):**
- Check https://ai.google.dev/gemini-api/docs/models for latest model list
- Note production vs preview models
- Flag any newly deprecated models

**OpenAI:**
- Check https://platform.openai.com/docs/models for latest model IDs
- Note flagship, coding, and reasoning models
- Flag any newly deprecated models

### Step 2: Fetch latest library versions

Check PyPI for current versions of each package:

```bash
pip3 index versions browser-use 2>/dev/null | head -1
pip3 index versions langchain-anthropic 2>/dev/null | head -1
pip3 index versions langchain-google-vertexai 2>/dev/null | head -1
pip3 index versions openai-whisper 2>/dev/null | head -1
pip3 index versions playwright 2>/dev/null | head -1
pip3 index versions google-cloud-aiplatform 2>/dev/null | head -1
```

### Step 3: Update models.md

Read the current `$CW_HOME/models.md` and update it with the new information:
- Update the "Last updated" date
- Update model tables with any new/changed/deprecated models
- Update library version table
- Keep the same format and structure
- Add notes about breaking changes if any model IDs changed

### Step 3.5: Refresh model pricing (`config/model_pricing.json`)

`config/model_pricing.json` is the grounded per-model token-cost table `factory_log.cost_for` uses (and `/reflect` reports consult cost from). Prices drift — re-fetch each provider's **live pricing page** (never key prices from memory) and update the `input_per_mtok` / `output_per_mtok` for every model, plus the row's `as_of` and the top-level `as_of`:

- Anthropic — via the `claude-api` skill reference / `platform.claude.com/docs/en/pricing`
- OpenAI — `developers.openai.com/api/docs/pricing`
- Google — `ai.google.dev/gemini-api/docs/pricing`
- Zhipu (GLM) — `docs.z.ai/guides/overview/pricing`

For tiered models, record the base (≤200k-context) rate. Leave a row `null` + `verified: false` if a price genuinely can't be confirmed (don't fabricate). `python3 -c "import json;json.load(open('config/model_pricing.json'))"` must stay valid.

### Step 3.6: Refresh the language support matrix doc (`docs/languages.md`)

`docs/languages.md` is mechanically rendered from `config/languages.json` (#162) — never hand-edit it. If the matrix changed (a new language, tier promotion, dep_profile change), regenerate the doc so it can't drift from the artifact:

```bash
python3 "$CW_HOME/scripts/render_languages_doc.py"
```

### Step 4: Review changes

Show the user a diff of what changed in `models.md`, `config/model_pricing.json`, and (if regenerated) `docs/languages.md`:
- Highlight new models
- Highlight deprecated models
- Highlight version bumps
- Highlight price changes
- Highlight any language-matrix changes
- Ask if the changes look correct

### Step 5: Commit and push

```bash
cd "$CW_HOME"
git add models.md config/model_pricing.json docs/languages.md
git commit -m "docs: update models, pricing, and library versions — $(date +%Y-%m-%d)"
git push
```

Report what was updated.

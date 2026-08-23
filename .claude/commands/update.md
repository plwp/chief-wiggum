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
# Pin the interpreter CW scripts run under. A bare `python3` is whatever
# the shell resolves, so a Homebrew bump silently strands keyring /
# jsonschema / google-genai and kills consults mid-phase (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
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

**OpenRouter (the `openrouter` tool's models — chief-wiggum#368, #372):**
- Check https://openrouter.ai/models for the current slugs of every provider in
  `config/providers.json` whose `tool` is `openrouter` — today `deepseek`,
  `deepseek-flash`, `kimi`, `glm`, `qwen`, `minimax`
- These are **routing slugs, not vendor IDs** (`deepseek/deepseek-v4-flash`,
  `moonshotai/kimi-k3`), and they change independently of the vendor's own
  naming. A stale slug does not degrade — the call 404s and the provider drops
  out of its role, which since #416 is reported as a quorum gap rather than
  silently absorbed, but is still a dead reviewer
- Update BOTH `models.md` and the `model` field in `config/providers.json`;
  they drift apart otherwise, and `providers.json` is the one that actually
  dispatches
- Note context-window changes: several roles send whole diffs inline because
  these providers declare `reads_repo=false`

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
- The OpenRouter table must stay in step with `config/providers.json`: if a
  slug changed, change it in both, and say so in the diff you show the user

### Step 3.5: Refresh model pricing (`config/model_pricing.json`)

`config/model_pricing.json` is the grounded per-model token-cost table `factory_log.cost_for` uses (and `/reflect` reports consult cost from). Prices drift — re-fetch each provider's **live pricing page** (never key prices from memory) and update the `input_per_mtok` / `output_per_mtok` for every model, plus the row's `as_of` and the top-level `as_of`:

- Anthropic — via the `claude-api` skill reference / `platform.claude.com/docs/en/pricing`
- OpenAI — `developers.openai.com/api/docs/pricing`
- Google — `ai.google.dev/gemini-api/docs/pricing`
- Zhipu (GLM) — `docs.z.ai/guides/overview/pricing`

For tiered models, record the base (≤200k-context) rate. Leave a row `null` + `verified: false` if a price genuinely can't be confirmed (don't fabricate). `"${CW_PY:-python3}" -c "import json;json.load(open('config/model_pricing.json'))"` must stay valid.

### Step 3.6: Refresh the language support matrix doc (`docs/languages.md`)

`docs/languages.md` is mechanically rendered from `config/languages.json` (#162) — never hand-edit it. If the matrix changed (a new language, tier promotion, dep_profile change), regenerate the doc so it can't drift from the artifact:

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/render_languages_doc.py"
```

### Step 3.7: Refresh the design-taste brief (`docs/design-taste.md`)

`docs/design-taste.md` (chief-wiggum#250) is the external grounding source every
divergent-design flow reads (`/design` Step 1, the `landing-page-smoke-test`/`presale`
patterns' INV-LPS-006/INV-PRE-006, Track H stamped assets) — a model's own priors date
badly and converge on AI-default aesthetics. Refresh it the same way models.md is
refreshed: research agents over the **pinned source roster** (§6 of the doc — curated
galleries, type-foundry showcases, design writing, a rotating "small products with
taste" list), never a scraper or a training pass (explicit non-goal). Regenerate §1–§4
(current-craft moves, anti-patterns, typography/palette/layout notes, per-genre direction
briefs), bump `as_of`, and note what changed since the last refresh. This step is
independent of the model/pricing refresh above — run it even if models.md needed no
changes, and skip it only if `as_of` is already within the 90-day staleness window and no
material shift in current design practice is known.

### Step 4: Review changes

Show the user a diff of what changed in `models.md`, `config/model_pricing.json`, and (if regenerated) `docs/languages.md`:
- Highlight new models
- Highlight deprecated models
- Highlight version bumps
- Highlight price changes
- Highlight any language-matrix changes
- Highlight design-taste brief changes (if refreshed)
- Ask if the changes look correct

### Step 5: Commit and push

```bash
cd "$CW_HOME"
git add models.md config/model_pricing.json docs/languages.md docs/design-taste.md
git commit -m "docs: update models, pricing, and library versions — $(date +%Y-%m-%d)"
git push
```

Report what was updated.

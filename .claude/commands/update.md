# Update - Refresh Model & Library Reference

Fetch the latest AI model IDs and library versions, update `models.md`, and push to the repo.

## Usage
```
/update
```

## Workflow

### Step 0: Resolve CW_HOME

```bash
CW_HOME=$(python3 -c "from pathlib import Path; print(Path('__file__').resolve().parent.parent.parent)" 2>/dev/null || echo "$HOME/repos/chief-wiggum")
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

### Step 4: Review changes

Show the user a diff of what changed in `models.md`:
- Highlight new models
- Highlight deprecated models
- Highlight version bumps
- Ask if the changes look correct

### Step 5: Commit and push

```bash
cd "$CW_HOME"
git add models.md
git commit -m "docs: update models and library versions — $(date +%Y-%m-%d)"
git push
```

Report what was updated.

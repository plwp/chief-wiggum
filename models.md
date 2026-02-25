# AI Models & Library Versions Reference

Last updated: 2026-02-25

Use this file when selecting models for consultations, browser-use, and reviews.
Refresh with `/update`.

## Claude (Anthropic)

| Model | API ID | Use for |
|-------|--------|---------|
| Opus 4.6 | `claude-opus-4-6` | Implementation, complex reasoning |
| Sonnet 4.6 | `claude-sonnet-4-6` | Code review, general tasks |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | Fast triage, simple queries |

**Vertex AI IDs**: `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5@20251001`
**Bedrock IDs**: `anthropic.claude-opus-4-6-v1`, `anthropic.claude-sonnet-4-6`

## Gemini (Google)

| Model | ID | Use for |
|-------|-----|---------|
| 2.5 Pro | `gemini-2.5-pro` | Quality-critical reasoning |
| 2.5 Flash | `gemini-2.5-flash` | Fast, cost-effective (default) |
| 2.5 Flash Lite | `gemini-2.5-flash-lite` | Highest throughput, lowest cost |

**Deprecated** (do not use): `gemini-2.0-flash`, `gemini-2.0-flash-lite`

## OpenAI

| Model | ID | Use for |
|-------|-----|---------|
| GPT-5.2 | `gpt-5.2` | Flagship |
| GPT-5.1 Codex | `gpt-5.1-codex` | Coding-optimized |
| GPT-5.1 Mini | `gpt-5.1-mini` | Fast/cheap |
| o3 | `o3` | Reasoning |
| o4-mini | `o4-mini` | Fast reasoning |

**Deprecated** (do not use): `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `o1`, `o3-mini`

## Whisper (Local)

| Model | Params | Notes |
|-------|--------|-------|
| `turbo` | 809M | Recommended — near large-v3 accuracy, much faster |
| `large-v3` | 1.55B | Best accuracy, slow |
| `base` | 74M | Fast, good for English |
| `tiny` | 39M | Fastest, lowest accuracy |

## Python Libraries

| Package | Version | Notes |
|---------|---------|-------|
| browser-use | 0.11.11 | Python >=3.11 |
| langchain-anthropic | 1.3.4 | |
| langchain-google-vertexai | 3.2.2 | For Vertex AI path |
| google-cloud-aiplatform | latest | For Vertex AI path |
| openai-whisper | 20250625 | |
| playwright | 1.58.0 | Python >=3.9 |

## Default Model Choices

For `/implement` multi-AI consultation:
- **codex CLI**: uses whatever model codex defaults to
- **gemini CLI**: uses whatever model gemini defaults to
- **Claude sub-agent**: `claude-opus-4-6`

For browser-use (langchain):
- Default: `claude-sonnet-4-6` (via langchain-anthropic)
- Vertex AI alternative: `gemini-2.5-flash` (via langchain-google-vertexai)

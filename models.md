# AI Models & Library Versions Reference

Last updated: 2026-03-08

Use this file when selecting models for consultations, browser-use, and reviews.
Refresh with `/update`.

## Claude (Anthropic)

| Model | API ID | Use for |
|-------|--------|---------|
| Opus 4.6 | `claude-opus-4-6` | Implementation, complex reasoning |
| Sonnet 4.6 | `claude-sonnet-4-6` | Code review, general tasks |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | Fast triage, simple queries |

**Vertex AI IDs**: `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5@20251001`
**Bedrock IDs**: `anthropic.claude-opus-4-6-v1`, `anthropic.claude-sonnet-4-6`, `anthropic.claude-haiku-4-5-20251001-v1:0`

## Gemini (Google)

| Model | ID | Use for |
|-------|-----|---------|
| 3.1 Pro (preview) | `gemini-3.1-pro-preview` | Latest flagship, complex reasoning |
| 2.5 Pro | `gemini-2.5-pro` | Best stable model for complex tasks |
| 2.5 Flash | `gemini-2.5-flash` | Fast, cost-effective (default) |
| 2.5 Flash Lite | `gemini-2.5-flash-lite` | Highest throughput, lowest cost |
| 3 Flash (preview) | `gemini-3-flash-preview` | High-performance preview |

**Deprecated** (do not use): `gemini-3-pro-preview` (shutdown March 9, 2026), `gemini-2.0-flash`, `gemini-2.0-flash-lite`

## OpenAI

| Model | ID | Use for |
|-------|-----|---------|
| GPT-5.4 | `gpt-5.4` | Flagship |
| GPT-5.4 Pro | `gpt-5.4-pro` | Highest capability |
| GPT-5.3 Codex | `gpt-5.3-codex` | Most capable coding model |
| GPT-5 Mini | `gpt-5-mini` | Fast/cheap |
| o3 | `o3` | Reasoning |
| o3-pro | `o3-pro` | Reasoning with more compute |
| o4-mini | `o4-mini` | Fast reasoning |

**Deprecated** (do not use): `gpt-5.2`, `gpt-5.1-codex`, `gpt-5.1-mini`, `gpt-4o`, `gpt-4o-mini`, `o1`, `o1-mini`

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
| browser-use | 0.12.1 | Python >=3.11 |
| langchain-anthropic | 1.3.4 | |
| langchain-google-vertexai | 3.2.2 | For Vertex AI path |
| google-cloud-aiplatform | 1.140.0 | For Vertex AI path |
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

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

## OpenRouter (chief-wiggum#368, #372)

Reached over the OpenRouter HTTP API by the single `openrouter` tool in
`scripts/consult_ai.py`; the key is `OPENROUTER_API_KEY` from the keyring,
passed at call time and never placed in the environment. Provider names below
are the `config/providers.json` names, which is what roles refer to — the
`openrouter` tool plus a `model` is the transport underneath.

| Provider | Model ID | Cost tier | Use for |
|----------|----------|-----------|---------|
| `deepseek-flash` | `deepseek/deepseek-v4-flash` | 1 | The code-quorum seat gemini vacated. Required in `reviewer` and `risky_diff_review`, optional in `explorer` and `architecture_critic`. Chosen for cost and speed, not distribution entropy. |
| `deepseek` | `deepseek/deepseek-v4-pro` | 2 | Required in `divergence`. Slower and dearer than flash; note its default 300s budget is often not enough for a large diff — pass `--timeout 900`. |
| `kimi` | `moonshotai/kimi-k3` | 2 | Required in `divergence`. Carries its own 900s `timeout_seconds`. |
| `glm` | `z-ai/glm-5.2` | 2 | Optional in `divergence`. |
| `qwen` | `qwen/qwen3.7-max` | 2 | Optional in `divergence`. |
| `minimax` | `minimax/minimax-m3` | 2 | Optional in `divergence`. |

All six declare `reads_repo=false`, `needs_inline_diff=true` and
`accepts_images=false`, with a 128k context window. Two consequences that bite
in practice:

- **A prompt sent to any of them must be self-contained.** They cannot read the
  repository, so a review prompt has to carry the diff inline AND enough
  context about the code the diff calls into. A blind reviewer that is not told
  what a helper does will infer, and inferred defects arrive at high confidence
  — verify every finding against the real code before acting on it.
- **`gemini-vertex` remains only where images are sent** (`design_critic`),
  because no OpenRouter provider here accepts them.

The `divergence` role exists to widen the quorum's *pretraining distribution*,
not its prompting, and stays opt-in. `deepseek-flash`'s seat in the code roles
is a separate decision made on cost and speed.

## Whisper (Local)

| Model | Params | Notes |
|-------|--------|-------|
| `turbo` | 809M | Recommended — near large-v3 accuracy, much faster |
| `large-v3` | 1.55B | Best accuracy, slow |
| `base` | 74M | Fast, good for English |
| `tiny` | 39M | Fastest, lowest accuracy |

## Text-to-Speech (narration)

| Engine | Model | Notes |
|--------|-------|-------|
| ElevenLabs | `eleven_multilingual_v2` | Preferred for `/tutorial-video` (auto when `ELEVENLABS_API_KEY` is in the keyring). Narrator voice comes from the target repo's `docs/tutorials/voice.json` (chosen: Sally the Aussie, `5GZaeOOG7yqLdoTRsaa6` — paid plan is active). George (`JBFqnCBsd6RMkjVDRZzb`) is only reachable via explicit `--allow-voice-fallback`; a refused/unavailable voice is a hard failure, never a silent fallback |
| OpenAI TTS | `gpt-4o-mini-tts` | Fallback; voices: alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer |
| OpenAI TTS | `tts-1-hd` | Higher fidelity, slower |
| macOS `say` | system voices | Offline fallback (`--engine say`), no API key needed |

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

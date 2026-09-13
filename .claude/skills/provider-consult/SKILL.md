---
name: provider-consult
description: How chief-wiggum consults AI providers - roles in config/providers.json, scripts/consult_ai.py usage, optional-provider timeouts, the divergence role and deepseek-flash, keyring secrets and the Vertex AI alternative. Load before running or debugging any consult.
---

# Consulting providers

Moved out of the root CLAUDE.md so it loads only when a workflow consults a provider.

## Secret Management

Secrets are stored in the **system keyring** (macOS Keychain, Linux SecretService, etc.) via the `keyring` Python library under the `chief-wiggum` service. They are NEVER stored as environment variables.

```bash
python3 scripts/keychain.py list                       # show status (not values)
python3 scripts/keychain.py set ANTHROPIC_API_KEY      # store (prompts securely)
python3 scripts/keychain.py delete ANTHROPIC_API_KEY   # remove
```

In Python scripts, secrets are loaded on demand:
```python
from keychain import get_secret
api_key = get_secret("ANTHROPIC_API_KEY")  # fetched from Keychain, never env
client = Anthropic(api_key=api_key)        # passed directly to constructor
```

### Required secrets (for SDK calls)

- `ANTHROPIC_API_KEY` - For browser-use (langchain-anthropic SDK)
- `OPENAI_API_KEY` - Optional, if calling OpenAI APIs directly
- `GEMINI_API_KEY` - Optional, if calling Gemini APIs directly

### Vertex AI (alternative to API keys for Google)

- `GOOGLE_CLOUD_PROJECT` - Your GCP project ID
- `GOOGLE_CLOUD_LOCATION` - Region (default: `us-central1`)
- Authenticate via `gcloud auth application-default login`

Use `gemini-vertex` as the tool name in `consult_ai.py` to route through Vertex AI.


## Provider Roles

Provider roles live in `config/providers.json`. Use `scripts/consult_ai.py` directly for one provider, or `--role <role> --output-dir <dir>` for a configured quorum:

```bash
python3 scripts/consult_ai.py codex prompt.md -o response.md
python3 scripts/consult_ai.py --role reviewer prompt.md --output-dir "$CW_TMP/reviews"
```

Roles define required and optional providers. Required providers must succeed; optional providers may be disabled or fail without blocking the role quorum. This keeps Claude, Codex, Gemini, and interactive delegates configurable rather than hard-coded into workflow logic.

**Optional providers fail fast, not slow**: the `claude-interactive` delegate's own budget is a generous 1800s (`TOOL_TIMEOUTS` in `scripts/consult_ai.py`) — appropriate when it's the one voice a step is waiting on, wasteful when it's merely an optional third opinion a role can run without. A role's `optional_timeout_seconds` (`config/providers.json`) caps how long an OPTIONAL provider's delegate call may run before `consult_ai.py`'s role quorum abandons it; every shipped role sets it to `300`. Unset, it falls back to `consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS`. This only affects the claude-interactive delegate (tool providers already run well under that budget) and only when a provider is in the role's `optional` list — a required provider always gets the delegate's full 1800s. See chief-wiggum#188.

**Distribution entropy is opt-in, not the default quorum**: the `divergence` role (`deepseek`, `kimi` required; `glm`, `qwen`, `minimax` optional) reaches frontier non-Western models over the OpenRouter HTTP API (`openrouter` tool in `scripts/consult_ai.py`, key from the keyring as `OPENROUTER_API_KEY`). Its purpose is to widen the quorum's *pretraining distribution*, not its prompting — the usual providers cluster on Anthropic/OpenAI/Google priors, so on generative questions (strategy, naming, positioning) they converge for reasons that have nothing to do with the question being settled. The divergence role itself stays opt-in: it is a second opinion you ask for on purpose, not a tax on every consult. Separately from that role, `deepseek-flash` (`deepseek/deepseek-v4-flash` over the same OpenRouter path) fills the code-quorum seat Gemini vacated — required in `reviewer`/`risky_diff_review`, optional in `explorer`/`architecture_critic` — chosen for cost and speed, not distribution entropy. It honestly declares `reads_repo=false`/`needs_inline_diff=true`, so those roles' prompts must stay self-contained; `gemini-vertex` remains only where images are sent (`design_critic`), because every OpenRouter provider is `accepts_images=false`. Two caveats — it is a plain API call with **no repo, filesystem, or web access** (prompts must be self-contained, so it cannot review a diff), and the role sets no lenses, because same-prompt-different-distribution is where the value is (see "Same prompt for all AIs" above). Asking the same question in Mandarin is a second, independent entropy axis on the same models.


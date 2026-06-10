# LLM Providers — June 2026 Snapshot

All quotas are tracked by `registry/models.json` and updated by the
Auto-Updater. The numbers below are the spec's baseline; live numbers
may differ. `[VERIFY]` tags mean the Auto-Updater hasn't confirmed them
in this cycle yet.

## Free / Generous Tier

### DeepSeek
- **Models**: `deepseek-r1-0528`
- **Quota**: ~12M tokens/day (rolling)
- **Strengths**: deep reasoning, agentic chains, math, coding SOTA
- **Limitations**: weak multimodal, moderate censorship
- **Notes**: Best free model for long reasoning chains in June 2026.

### Alibaba Qwen
- **Models**: `qwen3-235b-a22b-thinking-2507`
- **Quota**: ~8M tokens/day via OpenRouter
- **Strengths**: tool calling, parallel tool use, instruction following
- **Limitations**: moderate speed
- **Notes**: King of free tool-calling. Use as the planner/agent brain.

### Google AI Studio (Gemini)
- **Models**: `gemini-2.5-flash`
- **Quota**: ~15M tokens/day, 1M context window
- **Strengths**: vision, long context, research, multimodal, fast
- **Limitations**: none significant
- **Notes**: Best free quality/speed/long-context ratio.

### Moonshot (Kimi)
- **Models**: `moonshot-v2-200k`
- **Quota**: ~6M tokens/day, 200k context
- **Strengths**: extreme coherence, narrative, long writing
- **Limitations**: moderate coding
- **Notes**: Unmatched coherence above 80k tokens. Use for long writing.

### ByteDance Doubao
- **Models**: `doubao-fast`
- **Quota**: ~20M tokens/day, 32k context
- **Strengths**: high volume, cheap parallel, ultra fast
- **Limitations**: moderate reasoning/quality
- **Notes**: Volume king. 100+ parallel calls cheap.

### Groq
- **Models**: `llama-4-scout-17b-16e-instruct`
- **Quota**: ~5M tokens/day, rate-limited per minute
- **Strengths**: ultra fast inference, JSON mode, high throughput
- **Limitations**: moderate reasoning
- **Notes**: Speed demon. Use for MoA draft generation.

### Zhipu GLM
- **Models**: `glm-4-plus`, `glm-4v-plus`
- **Quota**: Generous, varies by model
- **Strengths**: bilingual Chinese-English, technical knowledge
- **Notes**: `[VERIFY]` Add to registry if you need Chinese-language support.

## Paid Tier (PRESERVE)

### OpenAI GPT
- **Models**: `gpt-5.5` (via openai-codex OAuth)
- **Quota**: Monthly subscription (rolling)
- **Strengths**: instruction following, JSON mode, structured output, safety
- **Limitations**: Cost
- **Notes**: Use ONLY when free tier cannot deliver. Orchestrator
  defaults to `preserve_paid_quota=True` and only flips it for
  `min_quality=exceptional` or when free-tier best score < threshold.

## Provider-Specific Notes

### Rate Limits
- DeepSeek: per-minute rate limit (not in the daily quota). Back off if you see 429s.
- OpenRouter: aggregate across all models. Spikes hit before daily cap.
- Gemini: per-minute + per-day. The 1M context model has stricter per-minute.
- Groq: strictly per-minute; can exhaust in seconds if you parallel hard.

### Daily Reset Times
Most providers reset at UTC midnight. Some (Doubao, Moonshot) reset on
rolling windows. The cron `reset-quotas` script resets all at the same
time; if you need provider-specific reset times, edit
`scripts/crontab.example`.

### API Key Sources
- **DeepSeek**: https://platform.deepseek.com → API Keys
- **OpenRouter**: https://openrouter.ai/keys
- **Gemini**: https://aistudio.google.com/app/apikey
- **Groq**: https://console.groq.com/keys
- **Moonshot**: via OpenRouter
- **Doubao**: via OpenRouter
- **OpenAI (Codex)**: ChatGPT Pro OAuth (handled by Hermes automatically)

## Adding a New Provider

1. Get an API key from the provider.
2. Add to `.env`: `PROVIDER_API_KEY=...`
3. Add a model entry to `registry/models.json`:
   ```json
   {
     "model_id": "provider/model-name",
     "provider": "provider",
     "display_name": "...",
     "context_window": ...,
     "input_price": 0.0,
     "output_price": 0.0,
     "is_free": true,
     "tier_rank": ...,
     "daily_quota_tokens": ...,
     "strength_tags": [...],
     "weakness_tags": [...],
     "best_for": [...],
     "performance_score": ...
   }
   ```
4. Add a model entry to `config/config.yaml` so LiteLLM knows about it.
5. `python -m scripts.validate_config` to confirm.
6. `python -m scripts.operations reset-quotas` to seed the quota.

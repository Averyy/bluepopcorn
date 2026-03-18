# TODO: Pluggable LLM backends

Support all three backends, user selects via `config.toml`. All three coexist — no option is
removed.

---

## The three backends

### 1. `claude_code` (current default — effectively free)
Uses `claude -p` subprocess. Covered by Claude Max/Pro subscription, so $0 per call.
Tradeoff: 1–3s subprocess startup overhead per call (Node.js process spawn).

### 2. `anthropic` (native SDK — best Claude experience)
Uses the `anthropic` Python SDK directly. Fastest for Claude models. Unlocks prompt caching
(`cache_control`) — the system prompt (personality + instructions + memory) is re-sent on every
call, so caching it saves ~90% of those input tokens after the first call.

### 3. `openai_compat` (universal — what most open-source projects use)
Uses the `openai` Python SDK with a configurable `base_url`. Works with any OpenAI-compatible
endpoint: Ollama (local/free), OpenRouter (300+ models), Groq, Mistral, DeepSeek, etc. Just
change two strings in config. Note: Anthropic via this path loses prompt caching.

---

## Config changes

**`config.toml`** — new fields in `[llm]`:
```toml
[llm]
backend = "claude_code"   # claude_code | anthropic | openai_compat
model = "haiku"
fallback_model = "sonnet"
timeout = 60
base_url = ""             # openai_compat only (e.g. "http://localhost:11434/v1")
```

**`.env`** — new optional keys:
```
ANTHROPIC_API_KEY=...     # required for backend = "anthropic"
OPENAI_API_KEY=...        # required for backend = "openai_compat"
```

---

## Code changes

### `llm.py` — refactor into protocol + 3 backends + factory

```
LLMClient (thin wrapper — handles fallback retry, loads system prompt)
  └── LLMBackend (protocol: decide(prompt, model) → (LLMDecision, dict))
        ├── ClaudeCodeBackend   — existing subprocess logic, extracted as-is
        ├── AnthropicBackend    — anthropic.AsyncAnthropic(), tool use for structured output
        └── OpenAICompatBackend — openai.AsyncOpenAI(base_url=...), function calling for structured output
```

`LLMClient` stays the same externally — `actions/` code doesn't change.

The fallback retry logic moves up into `LLMClient.decide()` so it works for all backends.

**Structured output per backend:**
- `ClaudeCodeBackend`: `--json-schema` flag (existing)
- `AnthropicBackend`: force tool use — single tool with `LLM_JSON_SCHEMA` as `input_schema`,
  `tool_choice={"type":"tool","name":"decide"}`, parse `content[0].input`
- `OpenAICompatBackend`: function calling — single function with `LLM_JSON_SCHEMA` as
  `parameters`, `tool_choice={"type":"function","function":{"name":"decide"}}`,
  parse `json.loads(tool_calls[0].function.arguments)`

**Model name mapping** (so config can keep "haiku"/"sonnet" shorthands):
- `ClaudeCodeBackend`: passes through as-is (CLI resolves it)
- `AnthropicBackend`: "haiku" → `claude-haiku-4-5-20251001`, "sonnet" → `claude-sonnet-4-6`,
  "opus" → `claude-opus-4-6`, anything else passed through
- `OpenAICompatBackend`: passed through as-is (user sets the full model name for their provider)

### `config.py` — new fields on `Settings`:
- `llm_backend: str = "claude_code"`
- `anthropic_api_key: str = ""` (from env `ANTHROPIC_API_KEY`)
- `openai_api_key: str = ""` (from env `OPENAI_API_KEY`)
- `llm_base_url: str = ""` (from `config.toml [llm] base_url`)

### `pyproject.toml` — new deps:
- `anthropic>=0.40`
- `openai>=1.50`

---

## What doesn't change

- `actions/` package — calls `llm.decide()` exactly as today
- `types.py` — `LLM_JSON_SCHEMA` and `LLMDecision` unchanged
- All other modules — no changes needed

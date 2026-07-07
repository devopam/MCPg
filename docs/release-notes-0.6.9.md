# MCPg v0.6.9 — release notes

**Released:** 2026-07-07
**Tool surface:** **252** tools across 19 capability buckets (read-only
mode exposes ~185)
**Tests:** unit + integration suite green (PG 14 / 15 / 16 / 17 / 18 / 19
/ WarehousePG)
**Runtime:** Python 3.14

A **patch bump (0.6.8 → 0.6.9)** headlined by a big expansion of the
natural-language-to-SQL provider fleet, plus two correctness fixes.
Backward-compatible — no tool signatures changed.

## Headline: 19 built-in NL→SQL providers, plug-and-play

`translate_nl_to_sql` now ships **19 built-in providers**, up from seven.
The expanded fleet adds **xAI (Grok), GitHub Models, Hugging Face, Groq,
Mistral, Together, Fireworks, DeepInfra, Cerebras, Nebius, SambaNova, and
Moonshot (Kimi)** alongside the original Anthropic, OpenAI, Gemini,
DeepSeek, Qwen, OpenRouter, and Perplexity.

Each is genuinely plug-and-play: set the vendor's conventional API-key
env var and MCPg auto-discovers it — including the ones that deviate from
the `<VENDOR>_API_KEY` convention (**Hugging Face → `HF_TOKEN`**,
**GitHub Models → `GITHUB_TOKEN`**, **DeepInfra → `DEEPINFRA_TOKEN`**).
Every base URL and key env var was verified against the vendor's own docs.

Under the hood, all provider metadata now lives in a **single
declarative registry** (`nl2sql.ProviderSpec` / `_PROVIDERS`) that every
lookup table derives from. Adding a provider — or refreshing a default
model when a vendor retires one — is a **one-line data edit, no code
change**. Local stacks (Ollama, vLLM, LM Studio) stay first-class on the
keyless `MCPG_NL2SQL_CUSTOM_PROVIDERS` path.

## Also fixed

- **Windows: the HTTP transport now connects to Postgres.** Running MCPg
  with `MCPG_TRANSPORT=streamable-http` (or `sse`) on Windows previously
  failed every database connection with a 30-second pool timeout —
  uvicorn reinstalled the `ProactorEventLoop`, which async psycopg
  rejects. `run_http` now pins the `WindowsSelectorEventLoopPolicy` and
  serves uvicorn on it. (stdio was unaffected.)
- **`serverInfo` now reports MCPg's own version.** The MCP `initialize`
  handshake had been advertising the MCP SDK's version instead of
  mcpg's; the advertised version is now pinned to `mcpg.__version__`.

## Under the hood

- **Version single-sourced** from `src/mcpg/__init__.py`.
  `pyproject.toml` reads it dynamically via hatchling, so pip,
  `mcpg --version`, and the `serverInfo` handshake can never disagree — a
  release bumps exactly one line.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.9   # or :latest
```

Or grab `mcpg-0.6.9.mcpb` from this release and double-click it into
Claude Desktop. No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.9]` for the complete
itemised list.

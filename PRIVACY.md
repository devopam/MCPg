# Privacy policy

MCPg is a **self-hosted** MCP server. You run it; your data stays with
you.

## What MCPg does with your data

- **Your database contents never leave your infrastructure.** Every
  tool talks only to the PostgreSQL server(s) you configure via
  `MCPG_DATABASE_URL` (and optional replica/secondary DSNs). Query
  results are returned to *your* MCP client and nowhere else.
- **No telemetry, no phone-home.** MCPg collects no usage analytics,
  sends no crash reports, and makes no network calls you didn't
  configure. The optional Prometheus `/metrics` endpoint and
  OpenTelemetry export are off by default and, when enabled, ship
  metrics/traces only to endpoints you specify.
- **One documented exception — NL→SQL.** The `translate_nl_to_sql`
  tool sends your natural-language question plus relevant schema
  context (table/column names, not row data) to the LLM provider whose
  API key *you* configured (Anthropic, OpenAI, Gemini, DeepSeek, Qwen,
  OpenRouter, or Perplexity — via the vendor's conventional env
  var). It is the only tool that contacts an external
  service. It is flagged `openWorldHint: true` in its MCP annotations
  and does nothing until you both set a key and call it.

## Credentials

- Database connection strings are read from environment variables and
  are never logged in full — the audit trail and log output redact
  passwords.
- When installed as a Claude Desktop extension (`.mcpb`), the
  connection URL is declared `sensitive` in the manifest, so the host
  application stores it in the operating system's keychain rather than
  in plain-text configuration.

## Audit trail

When `MCPG_AUDIT_PERSIST` is enabled, MCPg writes tool-call audit
events to a table **inside your own database** (`mcpg_audit.events`),
with credential-redacting argument capture. That data is yours; the
`prune_audit_events` tool deletes it on your schedule.

## Questions

Open an issue at <https://github.com/devopam/MCPg/issues> or see
[SECURITY.md](SECURITY.md) for vulnerability reporting.

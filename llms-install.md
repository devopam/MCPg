# Installing MCPg — instructions for an AI coding agent

You are an AI agent (Cline, Roo, or similar) setting up the **MCPg**
PostgreSQL MCP server for a user. Follow these steps exactly. Do not
consult README.md for install steps — it documents many paths; this
file is the single canonical one.

## Prerequisites

MCPg runs via `uvx` (from [uv](https://docs.astral.sh/uv/)), which
fetches and runs the published package with no separate install. Check
whether `uv` is available:

```bash
uvx --version
```

If that fails, install uv first:
- macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"`

## Step 1 — Get the database connection string from the user

MCPg needs a PostgreSQL connection URL. **Ask the user for it** if you
don't already have one. It looks like:

```
postgresql://USER:PASSWORD@HOST:5432/DBNAME
```

Rules to relay to the user:
- A **remote** host requires `?sslmode=require` (or stronger) appended
  — MCPg refuses insecure remote connections at startup.
- `localhost` / `127.0.0.1` needs no sslmode.
- If the user has no database to point at yet, tell them they can seed
  a throwaway demo dataset after setup (see Step 4).

Do **not** invent or hard-code credentials. Wait for the user's value.

## Step 2 — Write the MCP server configuration

Add this block to the user's Cline MCP settings
(`cline_mcp_settings.json`), merging into any existing `mcpServers`
object. Replace the placeholder URL with the value from Step 1:

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://USER:PASSWORD@HOST:5432/DBNAME"
      }
    }
  }
}
```

Notes:
- MCPg starts in **read-only** access mode by default — safe. Do not
  add write/DDL/shell env vars unless the user explicitly asks; those
  are `MCPG_ACCESS_MODE=unrestricted` plus `MCPG_ALLOW_DDL=true` etc.
- No API keys are required for core use. (The optional natural-language
  → SQL tool needs an LLM provider key, but everything else works
  without one.)

## Step 3 — Verify the connection

After saving the config, MCPg should appear as a connected server in
Cline's MCP panel with its tools listed. Confirm it works by calling
the `list_schemas` tool (or ask: "list the schemas in the database").
A successful response means setup is complete.

If it fails to connect, the most common causes are:
1. `uv` not on PATH → re-check the Prerequisites section above.
2. Database unreachable / wrong credentials → verify the URL with the
   user; check the host is running and reachable.
3. Remote host without `?sslmode=require` → MCPg rejects it at startup;
   append the sslmode parameter.

## Step 4 — (Optional) Seed a demo dataset

If the user has no interesting data to explore, they can populate a
curated demo schema (400 customers, 3,000 orders, 900 reviews, with
deliberately planted flaws for the analysis tools to find):

```bash
uvx mcpg --demo        # seed the mcpg_demo schema
uvx mcpg --demo-drop   # remove it later
```

(Run with the same `MCPG_DATABASE_URL` set in the environment.) Then
suggest they try: *"Why is this slow: SELECT * FROM mcpg_demo.orders
WHERE customer_id = 42 ORDER BY order_date DESC"* — MCPg will find the
missing index.

## Done

MCPg exposes 252 tools spanning catalog introspection, query
intelligence, health checks, index tuning, hybrid search, and more —
all read-only by default. Point the user at
[docs/integrations.md](docs/integrations.md) and
[docs/tour.md](docs/tour.md) for what to try next.

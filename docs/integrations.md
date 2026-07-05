# Client integrations — wire MCPg into your editor or agent

MCPg speaks all three MCP transports (`stdio`, `streamable-http`,
`sse`), so it works with every major MCP client. This page gives the
fastest install path per client. Everything below assumes `uv` is
installed (`pipx install uv` or see [astral.sh/uv](https://docs.astral.sh/uv/));
`uvx mcpg` fetches and runs the latest release with no separate
install step. Every snippet below uses the same **local-only example
DSN** (`postgresql://user:pass@localhost:5432/mydb`) — replace it with
your own; remote hosts require `?sslmode=require` (or stronger), which
MCPg enforces at startup.

> **Safe by default everywhere**: whatever the client, MCPg starts in
> read-only mode and every tool carries MCP `readOnlyHint` /
> `destructiveHint` annotations, so clients that support them can
> auto-approve reads and gate writes.

## Claude Desktop

**One-click**: download `mcpg-<version>.mcpb` from the
[latest release](https://github.com/devopam/MCPg/releases/latest) and
double-click it. You'll be prompted for the connection URL (stored in
the OS keychain). Manual config alternative in the
[installation guide](installation.md#claude-desktop-stdio-manual-config).

## Claude Code (CLI)

```bash
claude mcp add mcpg --env MCPG_DATABASE_URL=postgresql://user:pass@localhost:5432/mydb -- uvx mcpg
```

## Cursor

**One-click**: [Add to Cursor](https://cursor.com/install-mcp?name=mcpg&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJtY3BnIl0sImVudiI6eyJNQ1BHX0RBVEFCQVNFX1VSTCI6InBvc3RncmVzcWw6Ly91c2VyOnBhc3NAbG9jYWxob3N0OjU0MzIvbXlkYiJ9fQ%3D%3D)
(then edit the placeholder connection URL under Settings → MCP).

Manual — `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project):

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

## VS Code (Copilot agent mode)

**One-click**: [Install in VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522mcpg%2522%252C%2522command%2522%253A%2522uvx%2522%252C%2522args%2522%253A%255B%2522mcpg%2522%255D%252C%2522env%2522%253A%257B%2522MCPG_DATABASE_URL%2522%253A%2522%2524%257Binput%253Adatabase_url%257D%2522%257D%252C%2522inputs%2522%253A%255B%257B%2522type%2522%253A%2522promptString%2522%252C%2522id%2522%253A%2522database_url%2522%252C%2522description%2522%253A%2522PostgreSQL%2520connection%2520URL%2520%2528postgresql%253A%252F%252Fuser%253Apass%2540host%253A5432%252Fdb%2529%2522%252C%2522password%2522%253Atrue%257D%255D%257D)
· [Install in VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522mcpg%2522%252C%2522command%2522%253A%2522uvx%2522%252C%2522args%2522%253A%255B%2522mcpg%2522%255D%252C%2522env%2522%253A%257B%2522MCPG_DATABASE_URL%2522%253A%2522%2524%257Binput%253Adatabase_url%257D%2522%257D%252C%2522inputs%2522%253A%255B%257B%2522type%2522%253A%2522promptString%2522%252C%2522id%2522%253A%2522database_url%2522%252C%2522description%2522%253A%2522PostgreSQL%2520connection%2520URL%2520%2528postgresql%253A%252F%252Fuser%253Apass%2540host%253A5432%252Fdb%2529%2522%252C%2522password%2522%253Atrue%257D%255D%257D)

The one-click install prompts for your connection URL as a masked
input — it never lands in plain-text settings.

Manual — `.vscode/mcp.json` in your workspace:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "database_url",
      "description": "PostgreSQL connection URL",
      "password": true
    }
  ],
  "servers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "${input:database_url}"
      }
    }
  }
}
```

## Windsurf

`~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

## JetBrains IDEs (AI Assistant)

Settings → Tools → AI Assistant → Model Context Protocol (MCP) →
Add → *As JSON*, then paste the same `mcpServers` block shown for
Windsurf above. Available in AI Assistant 2025.1+ across IntelliJ
IDEA, PyCharm, DataGrip, and the rest of the family.

## Cline / Roo Code (VS Code extensions)

Cline can install MCPg autonomously — ask it to "set up the MCP server
at github.com/devopam/MCPg" and it follows
[`llms-install.md`](../llms-install.md). Or configure it by hand: MCP
Servers panel → Configure MCP Servers, or edit
`cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

## Zed

`settings.json` (⌘, / Ctrl-,):

```json
{
  "context_servers": {
    "mcpg": {
      "command": {
        "path": "uvx",
        "args": ["mcpg"],
        "env": {
          "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
        }
      }
    }
  }
}
```

## Google Antigravity (and Gemini CLI)

Antigravity's IDE and CLI share one MCP config —
`~/.gemini/config/mcp_config.json` (also reachable in-app via
*Manage MCP Servers → View raw config*; Antigravity reloads it
automatically on save):

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

For a remote/shared deployment, use `"serverUrl": "https://your-host:8000"`
with MCPg running the `streamable-http` transport (see the HTTP
section below).

## Continue

`~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: mcpg
    command: uvx
    args:
      - mcpg
    env:
      MCPG_DATABASE_URL: postgresql://user:pass@localhost:5432/mydb
```

## Qwen Code (CLI)

`~/.qwen/settings.json` (global) or `.qwen/settings.json` in your
project — the top-level `mcpServers` object:

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

`qwen mcp add` can write the entry for you, and the `/mcp` dialog
inside an interactive session shows every configured server and its
tools. Remote deployments can use `"httpUrl"` against MCPg's
`streamable-http` transport instead of `command`.

## Perplexity (Mac app, paid plans)

Perplexity's desktop app runs local MCP servers through a helper
process: Settings → **Connectors** → install the *PerplexityXPC*
helper → **Add Connector**. Use the *Advanced* tab and paste the same
`mcpServers` JSON shown for Windsurf above (the *Simple* tab's single
command field can't carry the `MCPG_DATABASE_URL` environment
variable). Once the connector shows *Running*, toggle it on under
*Sources* on the home screen.

## ChatGPT (remote connector — developer mode)

ChatGPT connects to **remote** MCP servers only: HTTPS required, no
localhost, OAuth or no-auth connectors. Run MCPg's `streamable-http`
transport on a reachable host (see the HTTP section below), then in
ChatGPT: Settings → Apps → Advanced settings → **Developer mode**
(Pro/Plus/Business/Enterprise, web) → add a connector with your
server URL.

Auth caveat, stated plainly: ChatGPT's connector options don't include
static bearer headers, so either front MCPg with an OAuth-terminating
gateway or run a no-auth connector **only** on a private network
against a **read-only, non-production** database — never expose an
unauthenticated database tool surface to the public internet.

## Microsoft Copilot

- **GitHub Copilot (VS Code agent mode)** — that's the
  [VS Code section above](#vs-code-copilot-agent-mode); the one-click
  badge covers it.
- **Microsoft 365 Copilot / Copilot Studio** — Copilot Studio agents
  connect to remote MCP servers: deploy MCPg's `streamable-http`
  transport on an HTTPS endpoint and register it as an MCP tool/custom
  connector in Copilot Studio. Same read-only-by-default posture
  applies; use `MCPG_AUTH_MODE=oidc` with your Entra ID issuer for
  enterprise-grade auth.

## Anything that speaks HTTP (LangGraph, custom agents, web apps)

Run MCPg as a server and point the client at it:

```bash
MCPG_DATABASE_URL=postgresql://user:pass@localhost:5432/mydb \
MCPG_TRANSPORT=streamable-http \
MCPG_HTTP_PORT=8000 \
MCPG_HTTP_AUTH_TOKEN=change-me \
mcpg
```

Then connect any MCP-over-HTTP client to `http://localhost:8000` with
the bearer token. For OIDC/JWT auth, rate limiting, and TLS options,
see the [installation guide](installation.md) and
[cookbook](cookbook.md).

---

## Asked about, not (yet) coverable

- **Aider** — no native MCP client support as of mid-2026 (tracked
  upstream in [Aider-AI/aider#4506](https://github.com/aider-ai/aider/issues/4506));
  community bridges like `mcpm-aider` exist but aren't stable enough
  for us to document. This section will gain a real config the moment
  native support lands.
- **DeepSeek** — a model provider, not an MCP client. DeepSeek models
  drive MCPg just fine *through* the clients above (e.g. Cline or
  Cursor with `deepseek-v4-pro` selected — note the pure-reasoning R1
  models can't call tools). Separately, a `DEEPSEEK_API_KEY` on the
  MCPg server enables DeepSeek as the `translate_nl_to_sql` provider —
  as do Qwen (`DASHSCOPE_API_KEY`), OpenRouter, and Perplexity keys.

---

**No interesting data to point at?** Seed the
[demo dataset](demo.md) first: `mcpg --demo` plants a curated schema
that gives every tool above something real to find.

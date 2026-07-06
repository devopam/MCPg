---
title: MCPg Demo
emoji: 🐘
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Read-only PostgreSQL MCP server (MCPg) live demo endpoint
---

# MCPg — live read-only demo

A public, **read-only** [MCPg](https://github.com/devopam/MCPg) instance — a
production-grade PostgreSQL MCP server (252 tools). It exists so MCP
directories (e.g. Smithery) can connect to score/route it, and as a
"try MCPg live" endpoint.

- **MCP endpoint:** `/mcp` (streamable-HTTP)
- **Access mode:** read-only, pointed at a throwaway demo database — no real
  data is exposed.

Run MCPg yourself against your own database: `uvx mcpg`.

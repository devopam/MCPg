# Hosting a public MCPg demo endpoint

A small, **read-only** MCPg instance hosted at a public HTTPS URL,
backed by a throwaway demo database. It exists so directories like
**Smithery** can connect to score/route it, and it doubles as a
"try MCPg live" endpoint. No real data is ever exposed — it points at
a demo database and runs in `read-only` access mode.

This uses the already-published GHCR image (`ghcr.io/devopam/mcpg`) —
no bespoke build, no repo code changes.

## 1. Provision + seed a demo database

Any managed Postgres works; a free-tier [Neon](https://neon.tech)
project is zero-maintenance and scales to zero. Once you have its
connection string, seed the curated demo dataset **from your own
machine** (the seeder needs direct Postgres access):

```bash
MCPG_DATABASE_URL="postgresql://…@…neon.tech/neondb?sslmode=require" \
  uvx mcpg --demo
```

Optional hardening: create a dedicated read-only Postgres role for the
hosted instance instead of the owner role. MCPg already enforces
read-only at the app layer, so this is defence-in-depth, not required
for a throwaway demo DB.

## 2. Deploy the endpoint (Fly.io)

[`fly.toml`](fly.toml) in this directory is ready to go. From a machine
with [flyctl](https://fly.io/docs/flyctl/install/) installed:

```bash
cd deploy
flyctl launch --no-deploy --copy-config --name mcpg-demo
# Set the connection string as a SECRET (never committed):
flyctl secrets set MCPG_DATABASE_URL="postgresql://…@…neon.tech/neondb?sslmode=require"
flyctl deploy
```

Your endpoint is then `https://mcpg-demo.fly.dev/mcp`. (Railway,
Render, or any container host works the same way — the only
requirements are a public HTTPS URL and the two env vars.)

Verify it's live:

```bash
curl -sS https://mcpg-demo.fly.dev/mcp -H "Accept: text/event-stream" | head
```

## 3. Register the URL with Smithery

Add the hosted endpoint to the existing `devopam/mcpg` listing so
Smithery scores it:

```bash
export SMITHERY_API_KEY="…"
npx --yes @smithery/cli@latest mcp publish https://mcpg-demo.fly.dev/mcp -n devopam/mcpg
```

Smithery connects, runs its tool scan against the read-only demo data,
and the listing gains its routing score.

## Notes

- **Read-only, public**: the instance serves only read tools against
  demo data. Even so, treat the demo DB as disposable.
- **Cost**: with `auto_stop_machines`, the instance idles to zero and
  wakes on demand — a demo fits comfortably in free allowances.
- **Local use is unaffected**: real users still install MCPg locally
  (`uvx mcpg`) pointed at their own database, running next to it. This
  hosted instance is purely for discovery/scoring + a live demo.

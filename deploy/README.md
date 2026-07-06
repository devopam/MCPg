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

## 2. Deploy the endpoint

Any container host with a public HTTPS URL works — the only
requirements are the image `ghcr.io/devopam/mcpg:latest`, a reachable
Postgres via `MCPG_DATABASE_URL`, and the transport env vars. Two
ready-to-go recipes:

### Option A — Hugging Face Spaces (recommended)

Hugging Face Spaces runs a Docker container at a public HTTPS URL for
free with no payment card. [`hfspace/`](hfspace/) contains the whole
recipe: a one-line `Dockerfile` wrapping the GHCR image on port 7860, a
`README.md` with the Space metadata, and [`deploy.py`](hfspace/deploy.py)
which creates/updates the Space and sets the DB secret via the HF API:

```bash
pip install huggingface_hub
export HF_TOKEN="hf_…"        # a write / manage-spaces token
export MCPG_DATABASE_URL="postgresql://…@…neon.tech/neondb?sslmode=require"
python deploy/hfspace/deploy.py           # → <you>/mcpg-demo
```

Endpoint: `https://<owner>-mcpg-demo.hf.space/mcp`. The free tier sleeps
after ~48h idle and cold-starts in ~30–60s — fine for periodic scoring.

### Option B — Fly.io

[`fly.toml`](fly.toml) is ready to go, but Fly requires a payment card on
file. From a machine with [flyctl](https://fly.io/docs/flyctl/install/):

```bash
cd deploy
flyctl launch --no-deploy --copy-config --name mcpg-demo
flyctl secrets set MCPG_DATABASE_URL="postgresql://…@…neon.tech/neondb?sslmode=require"
flyctl deploy
```

Endpoint: `https://mcpg-demo.fly.dev/mcp`. (Render, a small VPS, or any
container host works the same way.)

Verify whichever you deployed (substitute your URL) with a real MCP
`initialize` — a clean `serverInfo` reply means it connected to the DB:

```bash
curl -sS https://<your-endpoint>/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data-raw '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```

## 3. Register the URL with directories

Add the hosted endpoint to directories that score by connecting, e.g.
Smithery:

```bash
export SMITHERY_API_KEY="…"
npx --yes @smithery/cli@latest mcp publish https://<your-endpoint>/mcp -n devopam/mcpg
```

They connect, scan the tools against the read-only demo data, and the
listing gains its routing score.

## Notes

- **Read-only, public**: the instance serves only read tools against
  demo data. Even so, treat the demo DB as disposable.
- **Cost**: both recipes idle to zero and wake on demand — a demo fits
  comfortably in free allowances (HF Spaces needs no payment card).
- **Local use is unaffected**: real users still install MCPg locally
  (`uvx mcpg`) pointed at their own database, running next to it. This
  hosted instance is purely for discovery/scoring + a live demo.

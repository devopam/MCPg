#!/usr/bin/env python3
"""Create (or update) the public read-only MCPg demo on Hugging Face Spaces.

Hugging Face Spaces hosts a Docker container at a public HTTPS URL for
free with no payment card, which is exactly what MCP directories
(Smithery, Glama, …) need to connect to and score. This script wraps the
already-published GHCR image in a Docker Space, points it at a throwaway
demo database, and runs it read-only — no real data is ever exposed.

The two files uploaded to the Space live next to this script:
  - ``Dockerfile``  – ``FROM ghcr.io/devopam/mcpg:latest`` + port 7860 env
  - ``README.md``   – Space metadata (``sdk: docker``, ``app_port: 7860``)

The database connection string is stored as a Space *secret*, never
committed. It is read from the environment, not hard-coded.

Prereqs:
  pip install huggingface_hub
  export HF_TOKEN="hf_..."            # a write / manage-spaces token
  export MCPG_DATABASE_URL="postgresql://…@…neon.tech/neondb?sslmode=require"

Usage:
  python deploy.py [repo_id]          # default repo_id: <you>/mcpg-demo

Re-running is idempotent: it updates the existing Space in place.
The endpoint is then  https://<owner>-<name>.hf.space/mcp
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    dsn = os.environ.get("MCPG_DATABASE_URL")
    if not token or not dsn:
        print("Set HF_TOKEN and MCPG_DATABASE_URL in the environment first.", file=sys.stderr)
        return 1

    api = HfApi(token=token)
    default_owner = api.whoami()["name"]
    repo_id = sys.argv[1] if len(sys.argv) > 1 else f"{default_owner}/mcpg-demo"
    here = Path(__file__).resolve().parent

    # 1) The Docker Space itself (idempotent).
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    # 2) The connection string as a Space secret — injected as an env var
    #    at runtime, never baked into the image or the repo.
    api.add_space_secret(repo_id, "MCPG_DATABASE_URL", dsn, description="Demo DB (read-only)")
    # 3) Dockerfile + README.md in one commit. HF rebuilds on push.
    api.upload_folder(
        folder_path=str(here),
        repo_id=repo_id,
        repo_type="space",
        allow_patterns=["Dockerfile", "README.md"],
        commit_message="Deploy MCPg read-only demo",
    )

    endpoint = f"https://{repo_id.replace('/', '-')}.hf.space/mcp"
    print(f"Space:    https://huggingface.co/spaces/{repo_id}")
    print(f"Endpoint: {endpoint}")
    print("\nRegister it with directories that score by connecting, e.g. Smithery:")
    print(f"  npx --yes @smithery/cli@latest mcp publish {endpoint} -n {repo_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

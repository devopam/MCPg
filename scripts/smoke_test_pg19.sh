#!/usr/bin/env bash
# Live smoke test of every Phase 3 PG 19 tool against a local PostgreSQL 19 Beta.
#
# What this does, end-to-end:
#   1. Builds the existing `.github/ci-postgres-pg19.Dockerfile` image
#      locally as `mcpg-pg19-smoke` (pgvector built from source on top
#      of `postgres:19beta1`).
#   2. Starts the container on port 5443 (deliberately not 5432 so it
#      doesn't clash with any PG 14-18 you might already have running).
#   3. Waits for the server to accept connections.
#   4. Runs `scripts/smoke_test_pg19.py`, which exercises every Phase 3
#      tool's status probe + a representative write (when DDL is gated
#      on `MCPG_ACCESS_MODE=unrestricted`).
#   5. Tears down the container — unless `--keep` was passed.
#
# Pre-reqs:
#   - Docker daemon running locally.
#   - `uv` on PATH (the standard MCPg dev setup).
#
# Usage:
#   scripts/smoke_test_pg19.sh           # run smoke + clean up
#   scripts/smoke_test_pg19.sh --keep    # leave the container running
#   scripts/smoke_test_pg19.sh --down    # tear down a leftover container only
#
# The script is idempotent — re-running it removes a stale container
# before starting a fresh one. If you want a deeper smoke (live REPACK
# against a real 100k-row table, end-to-end logical replication slot,
# etc.) extend `scripts/smoke_test_pg19.py`; that's the script the
# operator iterates on, not this launcher.

set -euo pipefail

IMAGE=mcpg-pg19-smoke
CONTAINER=mcpg-pg19-smoke
PORT=5443
DB=mcpg_smoke
USER=postgres
PASSWORD=postgres
DB_URL="postgresql://${USER}:${PASSWORD}@127.0.0.1:${PORT}/${DB}"

cd "$(dirname "$0")/.."

down() {
  if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo ">>> Stopping + removing ${CONTAINER}"
    docker rm -f "${CONTAINER}" >/dev/null
  fi
}

case "${1:-}" in
  --down) down; exit 0 ;;
esac

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

down  # idempotent — remove any prior container

echo ">>> Building ${IMAGE} from .github/ci-postgres-pg19.Dockerfile"
docker build \
  -f .github/ci-postgres-pg19.Dockerfile \
  -t "${IMAGE}" .

echo ">>> Starting ${CONTAINER} on port ${PORT}"
docker run -d --name "${CONTAINER}" -p "${PORT}:5432" \
  -e POSTGRES_USER="${USER}" \
  -e POSTGRES_PASSWORD="${PASSWORD}" \
  -e POSTGRES_DB="${DB}" \
  "${IMAGE}" >/dev/null

echo ">>> Waiting for the server to accept connections"
for _ in $(seq 1 30); do
  if docker exec "${CONTAINER}" pg_isready -U "${USER}" -d "${DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! docker exec "${CONTAINER}" pg_isready -U "${USER}" -d "${DB}" >/dev/null 2>&1; then
  echo "!!! Server never came up — dumping last 50 log lines"
  docker logs --tail 50 "${CONTAINER}"
  down
  exit 1
fi

echo ">>> Running scripts/smoke_test_pg19.py"
MCPG_TEST_DATABASE_URL="${DB_URL}" \
  uv run python scripts/smoke_test_pg19.py

if [[ "${KEEP}" -eq 1 ]]; then
  echo ">>> --keep set; leaving ${CONTAINER} running at ${DB_URL}"
  echo "    Tear down with: scripts/smoke_test_pg19.sh --down"
else
  down
fi

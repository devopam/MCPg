#!/usr/bin/env bash
# Live PG 19 performance benchmark — quantifies the wins MCPg claims.
#
# Sibling of `scripts/smoke_test_pg19.sh` — same launcher pattern,
# different payload. Runs `scripts/benchmark_pg19.py` against a fresh
# PG 19 Beta container and emits actual timings + sizes for:
#
#   - skip-scan vs dedicated single-column index
#   - REPACK CONCURRENTLY vs VACUUM FULL
#   - LZ4 vs pglz TOAST compression
#
# AIO `io_uring` vs `worker` isn't covered here — switching `io_method`
# requires a server restart, so it lives as a manual recipe in
# `docs/plans/pg19-operations-playbook.md`.
#
# Pre-reqs:
#   - Docker daemon running locally.
#   - `uv` on PATH (the standard MCPg dev setup).
#
# Usage:
#   scripts/benchmark_pg19.sh           # run benchmark + clean up
#   scripts/benchmark_pg19.sh --keep    # leave the container running
#   scripts/benchmark_pg19.sh --down    # tear down a leftover container only

set -euo pipefail

IMAGE=mcpg-pg19-bench
CONTAINER=mcpg-pg19-bench
PORT=5444  # one above the smoke harness so both can co-exist
DB=mcpg_bench
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

if ! docker info >/dev/null 2>&1; then
  echo "!!! Docker daemon is not reachable. Start Docker Desktop / your" >&2
  echo "    daemon, then re-run this script." >&2
  exit 1
fi

case "${1:-}" in
  --down) down; exit 0 ;;
esac

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

cleanup_on_exit() {
  if [[ "${KEEP}" -eq 0 ]]; then
    down
  fi
}
trap cleanup_on_exit EXIT

down  # idempotent

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

echo ">>> Running scripts/benchmark_pg19.py"
MCPG_TEST_DATABASE_URL="${DB_URL}" \
  uv run python scripts/benchmark_pg19.py

if [[ "${KEEP}" -eq 1 ]]; then
  echo ">>> --keep set; leaving ${CONTAINER} running at ${DB_URL}"
  echo "    Tear down with: scripts/benchmark_pg19.sh --down"
fi

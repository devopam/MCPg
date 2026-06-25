# WarehousePG (Greenplum-derived MPP) base image for the `warehousepg-latest`
# CI matrix entry — used by `.github/workflows/ci.yml` to run unit + contract
# tests against the real WarehousePG SQL parser.
#
# We pin to a community-published single-node sandbox image. WarehousePG
# itself is wire-compatible with libpq so the existing pytest fixtures
# (which connect via `MCPG_TEST_DATABASE_URL`) work unchanged — we just need
# the server's catalog and SQL surface to be MPP-shaped (gp_segment_
# configuration, gp_distribution_policy, pg_appendonly, etc.) so the
# WarehousePG-specific tools have real catalog rows to read.
#
# The image tag here is a starting placeholder. WarehousePG has only
# recently been published as a community fork; if the upstream tag changes
# or moves, update the FROM line below. The CI matrix entry is gated with
# `continue-on-error: true` precisely so a missing tag doesn't block the
# main suite while the upstream image landscape stabilises.

FROM warehousepg/warehousepg:latest

# Surface the default WarehousePG port + credentials. The CI workflow
# expects POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB env vars to drive
# the wire-compatible interface — WarehousePG honours these because the
# coordinator is just a customised libpq backend.
ENV POSTGRES_USER=postgres \
    POSTGRES_PASSWORD=postgres \
    POSTGRES_DB=mcpg_test

EXPOSE 5432

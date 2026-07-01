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
# Image: `woblerr/warehousepg` (https://github.com/woblerr/docker-greenplum),
# an actively CI-built, publicly published single-node WarehousePG sandbox.
# The previous `warehousepg/warehousepg:latest` reference never existed on
# Docker Hub (confirmed 404 — "repository does not exist"), so every build
# of this Dockerfile failed at the very first `FROM` line since the day the
# WarehousePG CI lane was added; this was masked by `continue-on-error` on
# the matrix entry. EnterpriseDB's `warehouse-pg-docker` repo was considered
# and rejected: it publishes no pre-built image (build-your-own via a
# Makefile) and needs an EDB auth token for the RPM-based build, so it can't
# be used in an unauthenticated public CI pipeline.
FROM woblerr/warehousepg:7.4.1-WHPG

# This image's entrypoint reads GREENPLUM_*-prefixed env vars, NOT the
# POSTGRES_* ones the ci.yml `docker run` step passes for every other
# matrix entry (those are harmlessly ignored here). GREENPLUM_USER defaults
# to `gpadmin` — the conventional Greenplum-family superuser / OS account —
# left at its default rather than renamed, to match the documented,
# best-tested behaviour as closely as possible.
#   https://github.com/woblerr/docker-greenplum#readme
ENV GREENPLUM_PASSWORD=postgres \
    GREENPLUM_DATABASE_NAME=mcpg_test \
    GREENPLUM_DEPLOYMENT=singlenode

EXPOSE 5432

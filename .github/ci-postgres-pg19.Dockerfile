# PG 19 (beta) CI image. PostGIS is omitted — its PG 19 apt package
# isn't published yet — so any test that requires PostGIS is expected
# to skip / fail under PG 19. pgvector now ships v0.8.3 which adapts
# to PG 19's LWLock API rename (`AddinShmemInitLock`, `LW_EXCLUSIVE`,
# `LWLockAcquire` / `LWLockNewTrancheId` / `LWLockRegisterTranche` /
# `LWLockRelease` symbols changed mid-PG-18→19; pgvector v0.8.0 didn't
# compile against PG 19, v0.8.3 does). We pin to v0.8.3 so the image
# is reproducible, and keep the best-effort try-and-continue wrapper
# from PR #152 as a safety net in case a future PG 20 introduces
# similar churn before pgvector tracks it.
#
# Drop this file and route PG 19 back to `ci-postgres.Dockerfile` once:
#   1. pgvector ships an official `pgvector/pgvector:pg19` image, and
#   2. PostGIS ships a `postgresql-19-postgis-3` apt package.
#
# Until then, this Dockerfile is the PG 19 readiness scaffold (issue #120
# Phase 1) — the surface MCPg actually compiles against to triage
# behavioural / view-shape drift between PG 18 and PG 19.

FROM postgres:19beta1

ARG PG_MAJOR=19

# Build dependencies for pgvector. apt-get retries are courtesy — the
# pgdg repo occasionally returns 502 mid-fetch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        "postgresql-server-dev-${PG_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

# Try pgvector v0.8.3 (PG 19-compatible) build. The build is expected
# to succeed; the try-and-continue wrapper from PR #152 stays as a
# safety net so a future upstream churn doesn't gate the rest of the
# image. Tests that need pgvector skip cleanly via the existing
# `extension_installed("vector")` guard when the build does fall
# through.
RUN set -e; \
    git clone --depth 1 --branch v0.8.3 https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    if make && make install; then \
        echo "pgvector v0.8.3 built successfully against PG ${PG_MAJOR}."; \
    else \
        echo "pgvector v0.8.3 did not compile against PG ${PG_MAJOR}. Continuing without pgvector — vector tests will skip on this image. Roadmap 14.1 tracks the wait-for-upstream-support."; \
    fi; \
    rm -rf /tmp/pgvector

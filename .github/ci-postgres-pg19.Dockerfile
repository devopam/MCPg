# PG 19 (beta) CI image. PostGIS is omitted — its PG 19 apt package
# isn't published yet — so any test that requires PostGIS is expected
# to skip / fail under PG 19. pgvector v0.8.0 also doesn't compile
# against PG 19 (LWLock API moved: `AddinShmemInitLock`, `LW_EXCLUSIVE`,
# `LWLockAcquire` / `LWLockNewTrancheId` / `LWLockRegisterTranche`
# / `LWLockRelease` symbols changed mid-PG 18→19; observed on PR #152
# 2026-06-22), so this image now tries the build and continues without
# pgvector when it fails — same posture as PostGIS. Vector-dependent
# tests skip under PG 19 until pgvector publishes PG 19-compatible
# upstream support. The `continue-on-error` matrix entry in
# `.github/workflows/ci.yml` makes those skips non-blocking while
# PG 19 is still in beta.
#
# Drop this file and route PG 19 back to `ci-postgres.Dockerfile` once:
#   1. pgvector ships an official `pgvector/pgvector:pg19` image (or a
#      PG 19-compatible source release), and
#   2. PostGIS ships a `postgresql-19-postgis-3` apt package.
#
# Until then, this Dockerfile is the PG 19 readiness scaffold (issue #120
# Phase 1) — the surface MCPg actually compiles against to triage
# behavioural / view-shape drift between PG 18 and PG 19.

FROM postgres:19beta1

ARG PG_MAJOR=19

# Build dependencies for pgvector (best-effort below). apt-get retries
# are courtesy — the pgdg repo occasionally returns 502 mid-fetch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        "postgresql-server-dev-${PG_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

# Try pgvector v0.8.0 build. When pgvector publishes PG 19 support, the
# build succeeds and vector tests run. Until then the build fails on
# the LWLock API rename — log it loudly to the image build output and
# continue, so the rest of the image (which is what actually exercises
# PG 19's planner / catalog drift) still ships. Tests that need
# pgvector skip cleanly via the existing `extension_installed("vector")`
# guard.
RUN set -e; \
    git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    if make && make install; then \
        echo "pgvector built successfully against PG ${PG_MAJOR}."; \
    else \
        echo "pgvector v0.8.0 does not compile against PG ${PG_MAJOR} (LWLock API moved). Continuing without pgvector — vector tests will skip on this image. Roadmap 14.1 tracks the wait-for-upstream-support."; \
    fi; \
    rm -rf /tmp/pgvector

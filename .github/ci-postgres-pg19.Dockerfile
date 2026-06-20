# PG 19 (beta) CI image. pgvector doesn't publish a `pg19` image tag yet
# (issue #120), so we build pgvector from source on top of the official
# `postgres:19beta1` image. PostGIS is also omitted — its PG 19 apt
# package isn't published yet — so any test that requires PostGIS is
# expected to skip / fail under PG 19. The `continue-on-error` matrix
# entry in `.github/workflows/ci.yml` makes those failures non-blocking
# while PG 19 is still in beta.
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

# pgvector is small and stable enough to build from source against the
# PG 19 headers. Pin to the latest release tag so the image is
# reproducible.
RUN git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector \
    && make \
    && make install \
    && rm -rf /tmp/pgvector

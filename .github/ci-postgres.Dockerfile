# CI-only PostgreSQL image: pgvector's image plus the PostGIS extension, so
# the test matrix can integration-test pgvector and PostGIS features against
# a real database. Not used at runtime — see the project Dockerfile for that.
ARG PG_MAJOR=16
# Intentionally NOT pinned to a @sha256 digest (unlike this repo's other
# Dockerfiles) — this FROM line is deliberately parameterized by
# PG_MAJOR to drive the ci.yml PG 14-18 matrix off one shared image
# family; a single digest pin would fix it to one PG major version and
# defeat that design. Scorecard's Pinned-Dependencies check will keep
# flagging this line; that's an accepted, intentional exception.
FROM pgvector/pgvector:pg${PG_MAJOR}
ARG PG_MAJOR
RUN apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-postgis-3" \
    && rm -rf /var/lib/apt/lists/*

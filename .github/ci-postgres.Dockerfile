# CI-only PostgreSQL image: pgvector's image plus the PostGIS extension, so
# the test matrix can integration-test pgvector and PostGIS features against
# a real database. Not used at runtime — see the project Dockerfile for that.
ARG PG_MAJOR=16
FROM pgvector/pgvector:pg${PG_MAJOR}
ARG PG_MAJOR
RUN apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-postgis-3" \
    && rm -rf /var/lib/apt/lists/*

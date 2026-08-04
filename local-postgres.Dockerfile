# Custom PostgreSQL 17 image with pgvector, postgis, Apache AGE, and pg_turboquant precompiled.
FROM pgvector/pgvector:pg17@sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38

# Install system dependencies, postgis, and Apache AGE extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-17-postgis-3 \
    postgresql-17-age \
    build-essential \
    postgresql-server-dev-17 \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy, compile and install pg_turboquant
COPY scratch/pg_turboquant /tmp/pg_turboquant
RUN cd /tmp/pg_turboquant \
    && make \
    && make install \
    && rm -rf /tmp/pg_turboquant

# Clean up build dependencies to reduce image size
RUN apt-get purge -y --auto-remove build-essential postgresql-server-dev-17 git curl \
    && rm -rf /var/lib/apt/lists/*

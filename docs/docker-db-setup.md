# Repeatable Local PostgreSQL Database Deployment Guide

This document describes how to deploy the custom local PostgreSQL 17 database with precompiled `pgvector`, `pg_turboquant`, `postgis`, and `Apache AGE` extensions on a new machine.

---

## 1. Prerequisites
Ensure you have the following installed on the target machine:
- **Docker** (with daemon running)
- **Git** and **Git LFS**
- **Python 3.12+** and **uv** (for running verification scripts)

---

## 2. Git LFS Dataset Setup
The dataset SQL files (such as `demo_data/pagila-insert-data.sql`) are tracked via Git LFS. When cloning the repository on a new machine, ensure LFS files are downloaded correctly:

1. Install Git LFS:
   ```bash
   git lfs install
   ```
2. Pull the database seeding/dataset files:
   ```bash
   git lfs pull
   ```
3. To configure tracking for any additional SQL/dataset dumps:
   ```bash
   git lfs track "demo_data/*.sql"
   ```

---

## 3. Build the Custom Database Image
The custom Dockerfile [local-postgres.Dockerfile](file:///c:/Users/devop/OneDrive/Documents/GitHub/MCPg/local-postgres.Dockerfile) compiles `pg_turboquant` (including our type resolution path bypass patch) directly into the image.

Run the build command from the root of the repository:
```bash
docker build -t mcpg-local-db -f local-postgres.Dockerfile .
```

---

## 4. Run the Container
Run a new container using the built image:
```bash
docker run -d \
  --name mcpg-postgres \
  -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres \
  mcpg-local-db
```
This maps port `5433` on the host to standard PostgreSQL port `5432` in the container.

---

## 5. Enable Extensions and Schemas
Connect to the database and initialize the target database and extensions. You can execute these commands automatically using `docker exec`:

```bash
# Create the test database
docker exec -i mcpg-postgres psql -U postgres -c "CREATE DATABASE mcpg_test;"

# Install extensions and schemas in the test database
docker exec -i mcpg-postgres psql -U postgres -d mcpg_test -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE SCHEMA IF NOT EXISTS mcpg_demo;
  CREATE EXTENSION IF NOT EXISTS pg_turboquant;
"
```

---

## 6. Verify Deployment
Run the live vector search and maintenance test script to verify everything compiles, indexes, and queries correctly:

```bash
uv run python scratch/test_tq_live.py
```

Expected output:
```
Connected to database.
Recreating test table mcpg_demo.tq_test...
Seeding 200 random 128-dim vectors...
DEBUG: search_path=mcpg_demo, public, vector_oid=16386, halfvec_oid=16499
Building pg_turboquant ANN index (USING turboquant)...
Index build successful.
...
```

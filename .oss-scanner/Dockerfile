# Build image for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
# Referenced from the enrollment `project.yaml` (projects/mcpg/) as
# `dockerfile: .oss-scanner/Dockerfile`.
#
# MCPg is a *pure-Python* MCP server: there are no compiled artifacts, so the
# scanner template's native/C toolchain (clang, CMake, debug info, frame
# pointers) does not apply. This image's only job is to stand up the full
# source tree + dependency environment the scanner's agent analyzes offline.
#
# The scan runs with NO network, so EVERYTHING the agent might exercise —
# runtime *and* dev dependencies (pytest + the adversarial SQL-kernel suites,
# see threat_model.md "How to exercise it") — is installed here, during the
# build, where network is still available.
#
# Scanner contract: the repository checkout must live at /src, and the build
# must succeed.
FROM python:3.14-slim-bookworm

# uv is MCPg's package manager / lockfile resolver. Installed via pip to stay
# version-robust (no pinned external image tag to go stale).
RUN pip install --no-cache-dir uv

# git for the agent; build-essential covers the rare transitive sdist that
# needs a C extension at install time (psycopg[binary], pglast, cryptography
# all ship wheels, so this is usually unused); postgresql-client provides the
# psql / pg_dump / pg_restore binaries MCPg's shell tools resolve on PATH
# (src/mcpg/shell.py:run_pg_binary, which rejects a tool whose binary can't be
# found), so the scanner can exercise that command-execution surface.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Scanner contract: the checkout lives at /src. This build uses its own ignore
# rules (.oss-scanner/Dockerfile.dockerignore) in preference to the product
# image's root .dockerignore, so tests/ — the adversarial SQL-kernel suites the
# threat model points the scanner at — is present in /src.
COPY . /src
WORKDIR /src

# Install the full environment from the committed lockfile (default groups,
# which include `dev` → pytest + the test suites). `--frozen` installs from
# uv.lock as-is for a reproducible tree. Network is available now; it is
# removed after this build completes.
ENV UV_PROJECT_ENVIRONMENT=/src/.venv
RUN uv sync --frozen

# Smoke-check: the package imports and the version resolves without a
# database (MCPG_DATABASE_URL is only needed to actually serve traffic, and
# there is no PostgreSQL in the no-network scan VM — the SQL-safety kernel
# validates by parsing, so it is exercisable without a live DB).
RUN uv run python -c "import mcpg; print('mcpg', mcpg.__version__)"

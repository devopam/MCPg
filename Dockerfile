# --- Stage 1: Build virtual environment ---
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install dependencies and sync packages
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

# --- Stage 2: Runtime environment ---
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Copy virtual environment and source code
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/README.md /app/README.md

# Create unprivileged secure user UID 10001 / GID 10001
RUN groupadd -g 10001 mcpg && \
    useradd -r -u 10001 -g mcpg -d /app -s /sbin/nologin mcpg && \
    chown -R mcpg:mcpg /app

USER mcpg

# Expose PATH to use virtual environment packages
ENV PATH="/app/.venv/bin:$PATH" \
    MCPG_TRANSPORT=streamable-http \
    MCPG_HTTP_HOST=0.0.0.0 \
    MCPG_HTTP_PORT=8000

EXPOSE 8000

ENTRYPOINT ["python", "-m", "mcpg"]

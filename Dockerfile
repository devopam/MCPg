# MCPg — PostgreSQL MCP server.
# Build:  docker build -t mcpg .
# Run:    docker run -e MCPG_DATABASE_URL=postgresql://... -p 8000:8000 mcpg
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies and the package itself, without dev tooling.
# uv.lock keeps the build reproducible (--frozen).
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 mcpg && chown -R mcpg /app
USER mcpg

# Containers serve over HTTP; MCPG_DATABASE_URL must be supplied at runtime.
ENV MCPG_TRANSPORT=streamable-http \
    MCPG_HTTP_HOST=0.0.0.0 \
    MCPG_HTTP_PORT=8000
EXPOSE 8000

ENTRYPOINT ["uv", "run", "--no-dev", "mcpg"]

# Read-only MCPg demo for Hugging Face Spaces.
# Wraps the published image unchanged; only overrides env so it serves
# streamable-HTTP on the port HF Spaces routes to (7860), read-only.
FROM ghcr.io/devopam/mcpg:latest

ENV MCPG_TRANSPORT=streamable-http \
    MCPG_HTTP_HOST=0.0.0.0 \
    MCPG_HTTP_PORT=7860 \
    MCPG_ACCESS_MODE=read-only

EXPOSE 7860
# ENTRYPOINT ["python","-m","mcpg"] and USER mcpg come from the base image.

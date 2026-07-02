"""MCPg — a PostgreSQL Model Context Protocol server."""

import warnings

__version__ = "0.6.6"

# ``schema`` is the natural field name for "which Postgres schema" across
# ~180 tool-return dataclasses. The ``mcp`` SDK dynamically builds a
# pydantic model from each of those dataclasses to publish an output
# schema (mcp.server.fastmcp.utilities.func_metadata), and pydantic's
# BaseModel still carries a deprecated v1-compat ``.schema()`` method —
# so every one of those dataclasses trips this warning the first time a
# client calls ``tools/list``. Renaming the field would break the JSON
# shape of every affected tool's output; suppressing this specific,
# known-benign message is the narrower fix. Exported so the regression
# test can apply the exact same pattern rather than a hand-copied one.
PYDANTIC_SCHEMA_FIELD_SHADOW_WARNING = r'Field name ".*" in ".*" shadows an attribute in parent "BaseModel"'

warnings.filterwarnings(
    "ignore",
    message=PYDANTIC_SCHEMA_FIELD_SHADOW_WARNING,
    category=UserWarning,
)

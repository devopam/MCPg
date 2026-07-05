"""MCPB entry point — a thin launcher around the installed mcpg package.

The bundle's pyproject.toml pins the mcpg release; the host installs it
with uv and runs this file. All real logic lives in the package —
configuration arrives via the environment variables the manifest maps
from user_config (MCPG_DATABASE_URL, MCPG_ACCESS_MODE).
"""

import sys

from mcpg.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

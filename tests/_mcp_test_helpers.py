"""Drop-in replacement for mcp.shared.memory.create_connected_server_and_client_session,
removed in mcp 2.0 (only create_client_server_memory_streams remains there).
Ported from mcp 1.29.0's implementation with the two changes mcp 2.0 requires:
FastMCP -> MCPServer, and the private attribute FastMCP used to reach the
lowlevel Server was renamed from _mcp_server to _lowlevel_server.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import anyio
import mcp.types as types
from mcp.client.session import ClientSession, ElicitationFnT, ListRootsFnT, LoggingFnT, MessageHandlerFnT, SamplingFnT
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def create_connected_server_and_client_session(
    server: Server[Any] | MCPServer[Any],
    read_timeout_seconds: timedelta | None = None,
    sampling_callback: SamplingFnT | None = None,
    list_roots_callback: ListRootsFnT | None = None,
    logging_callback: LoggingFnT | None = None,
    message_handler: MessageHandlerFnT | None = None,
    client_info: types.Implementation | None = None,
    *,
    raise_exceptions: bool = False,
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Creates a ClientSession that is connected to a running MCP server."""
    if isinstance(server, MCPServer):
        server = server._lowlevel_server  # type: ignore[reportPrivateUsage]

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=raise_exceptions,
                )
            )

            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    read_timeout_seconds=read_timeout_seconds,
                    sampling_callback=sampling_callback,
                    list_roots_callback=list_roots_callback,
                    logging_callback=logging_callback,
                    message_handler=message_handler,
                    client_info=client_info,
                    elicitation_callback=elicitation_callback,
                ) as client_session:
                    await client_session.initialize()
                    yield client_session
            finally:
                tg.cancel_scope.cancel()

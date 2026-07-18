"""End-to-end measurement paths — through the real MCP protocol.

Where ``ServerSideRunner`` (paths.py) calls the tool *function* in-process,
these runners drive the ``run_select`` tool through a real
:class:`mcp.ClientSession`, so the timing includes what an agent actually pays
on top of the server-side cost: **JSON-RPC encode/decode + transport** — the
``t_protocol`` band of the waterfall.

Three transports, cheapest first:

* :class:`E2EInMemoryRunner` — client and server share in-memory streams
  (``create_connected_server_and_client_session``). Isolates the protocol
  (serialize/deserialize + FastMCP dispatch) with **no** OS transport, so it is
  the cleanest attribution of MCPg's own protocol overhead. The server runs its
  real lifespan, so the tool executes against the real database.
* :class:`E2EStdioRunner` — spawns ``python -m mcpg`` as a subprocess and talks
  to it over stdio (``stdio_client``). Adds pipe + subprocess transport — what a
  locally-launched MCP client experiences.
* :class:`E2EHttpRunner` — connects to an **operator-started** streamable-HTTP
  server (``--e2e-http-url``). Adds the HTTP/JSON-RPC transport. We connect to a
  separately-run server rather than managing uvicorn's blocking lifecycle
  in-process, which keeps the harness honest and robust.

All three are opt-in (``runner.py --e2e`` / ``--e2e-http-url``) and, like the
rest of the DB-touching harness, are operator tools — not unit-tested. The pure
helper (:func:`row_count_of`) is.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult

from mcpg.config import Settings
from mcpg.server import create_server

# Generous per-call ceiling so a heavy TPC-H query over the protocol doesn't
# trip the client's default read timeout mid-measurement.
_READ_TIMEOUT = timedelta(seconds=120)


def _check(result: CallToolResult) -> CallToolResult:
    """Raise if the tool call errored, so a broken setup fails loudly.

    A silent error path would otherwise report suspiciously fast timings (an
    immediate error round-trip) as if they were real query latencies.
    """
    if result.isError:
        text = result.content[0].text if result.content and hasattr(result.content[0], "text") else str(result.content)
        raise RuntimeError(f"run_select failed over the MCP protocol: {text}")
    return result


class E2ERunner(Protocol):
    """An end-to-end path with an explicit async lifecycle.

    ``start`` / ``close`` bracket a long-lived client session that ``run_once``
    (the :class:`~benchmarks.perf.paths.PathRunner` timing method) reuses across
    every query.
    """

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def run_once(self, sql: str, *, max_rows: int) -> int: ...


def row_count_of(result: CallToolResult) -> int | None:
    """Best-effort row count from a ``run_select`` CallToolResult.

    Reads ``structuredContent['row_count']`` when present (typed-output tools
    carry it); returns ``None`` when the shape doesn't expose it. Pure — unit
    tested — so the sanity check doesn't depend on a live server.
    """
    sc: Any = result.structuredContent
    if isinstance(sc, dict):
        count = sc.get("row_count")
        if isinstance(count, int):
            return count
    return None


class _SessionRunner:
    """Shared timing body: call ``run_select`` over an initialized session."""

    _session: ClientSession | None = None

    async def run_once(self, sql: str, *, max_rows: int) -> int:
        assert self._session is not None, "runner not started"
        start = time.perf_counter_ns()
        result = await self._session.call_tool("run_select", {"sql": sql, "max_rows": max_rows})
        elapsed = time.perf_counter_ns() - start
        _check(result)
        return elapsed


class E2EInMemoryRunner(_SessionRunner):
    """Client + server over in-memory streams — isolates ``t_protocol``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stack = AsyncExitStack()

    async def start(self) -> None:
        # The server runs its real lifespan here (connecting the DB), so the
        # tool executes for real; only the transport is in-memory.
        server = create_server(self._settings)
        self._session = await self._stack.enter_async_context(
            create_connected_server_and_client_session(server, read_timeout_seconds=_READ_TIMEOUT)
        )

    async def close(self) -> None:
        self._session = None
        await self._stack.aclose()


class E2EStdioRunner(_SessionRunner):
    """Talks to a ``python -m mcpg`` subprocess over stdio."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._stack = AsyncExitStack()

    async def start(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcpg"],
            # Inherit the environment so the child finds its interpreter/packages,
            # then point it at the benchmark database.
            env={**os.environ, "MCPG_DATABASE_URL": self._database_url},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT))
        await session.initialize()
        self._session = session

    async def close(self) -> None:
        self._session = None
        await self._stack.aclose()


class E2EHttpRunner(_SessionRunner):
    """Connects to an operator-started streamable-HTTP MCPg server."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._stack = AsyncExitStack()

    async def start(self) -> None:
        # streamablehttp_client yields (read, write, get_session_id); we only
        # need the two streams for a ClientSession.
        read, write, _ = await self._stack.enter_async_context(streamablehttp_client(self._url))
        session = await self._stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT))
        await session.initialize()
        self._session = session

    async def close(self) -> None:
        self._session = None
        await self._stack.aclose()

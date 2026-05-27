"""Unit tests for Apache AGE graph management module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver

from mcpg.config import AccessMode, Settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.graph_mgmt import create_graph, drop_graph
from mcpg.listen import ListenManager
from mcpg.policy import PermissionError


async def test_create_graph_validates_inputs() -> None:
    fake_routing = FakeRoutingDriver({})
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(ValueError, match="invalid graph name"):
        await create_graph(context, "my-graph-with-dashes")


async def test_create_graph_requires_ddl_capability() -> None:
    fake_routing = FakeRoutingDriver({})
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.READ_ONLY)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(PermissionError, match="requires DDL capability"):
        await create_graph(context, "my_graph")


async def test_create_graph_returns_exists_if_already_exists() -> None:
    fake_routing = FakeRoutingDriver(
        {
            "ag_graph": [{"present": 1}],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.UNRESTRICTED, allow_ddl=True)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    res = await create_graph(context, "my_graph")
    assert res["status"] == "exists"
    assert "already exists" in res["message"]


async def test_create_graph_creates_successfully() -> None:
    fake_routing = FakeRoutingDriver(
        {
            "ag_graph": [],  # does not exist
            "create_graph": [{"result": "created"}],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.UNRESTRICTED, allow_ddl=True)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    res = await create_graph(context, "my_graph")
    assert res["status"] == "created"
    assert "created successfully" in res["message"]
    assert any("create_graph('my_graph')" in str(c[0]) or "create_graph" in str(c[0]) for c in fake_routing.calls)


async def test_drop_graph_returns_not_found_if_does_not_exist() -> None:
    fake_routing = FakeRoutingDriver(
        {
            "ag_graph": [],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.UNRESTRICTED, allow_ddl=True)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    res = await drop_graph(context, "my_graph")
    assert res["status"] == "not_found"
    assert "does not exist" in res["message"]


async def test_drop_graph_drops_successfully() -> None:
    fake_routing = FakeRoutingDriver(
        {
            "ag_graph": [{"present": 1}],
            "drop_graph": [{"result": "dropped"}],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.UNRESTRICTED, allow_ddl=True)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    res = await drop_graph(context, "my_graph", cascade=True)
    assert res["status"] == "dropped"
    assert "deleted successfully" in res["message"]
    assert any("drop_graph" in str(c[0]) for c in fake_routing.calls)

"""Tests for pg_partman partition-management wrappers."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.partman import (
    PartmanError,
    PartmanResult,
    partman_create_parent,
    partman_drop_partition,
    partman_run_maintenance,
)
from mcpg.server import create_server

_UNRESTRICTED_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)
_UNRESTRICTED_NO_DDL = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- partman_create_parent -------------------------------------------------


async def test_partman_create_parent_succeeds_when_extension_returns_true() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "partman.create_parent": [{"created": True}],
        }
    )

    result = await partman_create_parent(driver, "app.event", "created", "1 day")  # type: ignore[arg-type]

    assert result == PartmanResult(parent_table="app.event", detail="created")


async def test_partman_create_parent_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PartmanError, match="not installed"):
        await partman_create_parent(driver, "app.event", "created", "1 day")  # type: ignore[arg-type]


async def test_partman_create_parent_raises_on_unsupported_type() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PartmanError, match="unsupported partition_type"):
        await partman_create_parent(  # type: ignore[arg-type]
            driver, "app.event", "created", "1 day", partition_type="hash"
        )


async def test_partman_create_parent_raises_when_create_returns_false() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "partman.create_parent": [{"created": False}],
        }
    )

    with pytest.raises(PartmanError, match="returned false"):
        await partman_create_parent(driver, "app.event", "created", "1 day")  # type: ignore[arg-type]


# --- partman_run_maintenance ----------------------------------------------


async def test_partman_run_maintenance_runs_for_all_parents_when_table_omitted() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "partman.run_maintenance": []})

    result = await partman_run_maintenance(driver)  # type: ignore[arg-type]

    assert result.parent_table == "*"


async def test_partman_run_maintenance_scoped_to_one_parent() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "partman.run_maintenance": []})

    result = await partman_run_maintenance(driver, "app.event")  # type: ignore[arg-type]

    assert result.parent_table == "app.event"


async def test_partman_run_maintenance_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PartmanError, match="not installed"):
        await partman_run_maintenance(driver)  # type: ignore[arg-type]


# --- partman_drop_partition -----------------------------------------------


async def test_partman_drop_partition_returns_dropped_names_for_time_retention() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "partman.drop_partition_time": [{"dropped": "app.event_2024_01"}, {"dropped": "app.event_2024_02"}],
        }
    )

    dropped = await partman_drop_partition(driver, "app.event", "30 days")  # type: ignore[arg-type]

    assert dropped == ["app.event_2024_01", "app.event_2024_02"]


async def test_partman_drop_partition_filters_empty_dropped_rows() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "partman.drop_partition_time": [{"dropped": None}]})

    assert await partman_drop_partition(driver, "app.event", "30 days") == []  # type: ignore[arg-type]


async def test_partman_drop_partition_uses_id_branch_when_control_is_not_time() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "partman.drop_partition_id": [{"dropped": "app.event_id_900000"}],
        }
    )

    dropped = await partman_drop_partition(  # type: ignore[arg-type]
        driver, "app.event", "1000000", control_is_time=False
    )

    assert dropped == ["app.event_id_900000"]


async def test_partman_drop_partition_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PartmanError, match="not installed"):
        await partman_drop_partition(driver, "app.event", "30 days")  # type: ignore[arg-type]


# --- tool wiring -----------------------------------------------------------


_PARTMAN_TOOLS = {"partman_create_parent", "partman_run_maintenance", "partman_drop_partition"}


async def test_partman_tools_hidden_in_read_only_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert _PARTMAN_TOOLS.isdisjoint(listed)


async def test_partman_tools_hidden_in_unrestricted_without_allow_ddl() -> None:
    # Partman creates and drops partitions — DDL. Per project policy
    # (run_ddl, enable_extension) DDL tools require MCPG_ALLOW_DDL even
    # in unrestricted mode. This test locks the gating in.
    server = create_server(_UNRESTRICTED_NO_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert _PARTMAN_TOOLS.isdisjoint(listed)


async def test_partman_tools_registered_in_unrestricted_mode_with_allow_ddl() -> None:
    server = create_server(_UNRESTRICTED_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert _PARTMAN_TOOLS <= listed

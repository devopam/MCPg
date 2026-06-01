import asyncio
from unittest.mock import patch

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.server import create_server


@pytest.mark.asyncio
async def test_concurrent_load_and_caching_integration(connected_database: Database, database_url: str) -> None:
    # 1. Load settings with caching enabled
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": database_url,
            "MCPG_CACHE_ENABLED": "true",
            "MCPG_CACHE_TTL_SECONDS": "60",
        }
    )
    server = create_server(settings, database=connected_database)

    # 2. Get the sql driver to spy on catalog queries
    spied_driver = connected_database.driver()

    # Wrap execute_query to spy on database calls
    original_execute = spied_driver.execute_query
    call_count = 0

    async def spy_execute(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        return await original_execute(*args, **kwargs)

    spied_driver.execute_query = spy_execute

    # Patch Database.driver to return our spied driver for every call inside the server
    async with create_connected_server_and_client_session(server) as client:
        with patch.object(connected_database, "driver", return_value=spied_driver):
            # A. Warm up the cache for a specific schema query
            warmup_res = await client.call_tool("list_tables", {"schema": "public"})
            assert warmup_res.isError is False

            # Record the execute calls for the warmup
            warmup_calls = call_count
            assert warmup_calls > 0, "Warmup should have queried the database"

            # B. Now run 30 concurrent requests for the exact same tool and arguments
            async def run_one(i: int) -> object:
                return await client.call_tool("list_tables", {"schema": "public"})

            tasks = [run_one(i) for i in range(30)]
            results = await asyncio.gather(*tasks)

            # C. Verify all 30 parallel requests returned successfully and were cached
            for idx, res in enumerate(results):
                assert res.isError is False, f"Task {idx} failed: {res}"

            # D. Crucial check: Assert that the database was NOT queried again!
            # All 30 concurrent tasks must have hit the cache populated by the warmup.
            assert call_count == warmup_calls, (
                f"Database query count increased from {warmup_calls} to {call_count}. "
                "Caching failed to prevent database pool query saturation!"
            )

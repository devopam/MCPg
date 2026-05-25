"""Integration tests for the LISTEN/NOTIFY bridge against a real PG."""

from __future__ import annotations

import asyncio

import pytest

from mcpg.database import Database
from mcpg.listen import ListenManager


async def test_subscribe_receives_a_pg_notify_round_trip(connected_database: Database) -> None:
    """End-to-end: subscribe → another connection issues NOTIFY → poll receives it."""
    settings = connected_database._settings
    manager = ListenManager(database_url=settings.database_url)
    try:
        sub_id = await manager.subscribe("mcpg_listen_it")

        # Publish from the regular driver pool — a separate connection
        # from the listener, exactly as a real publisher would be.
        driver = connected_database.driver()
        # pg_notify takes a text payload; SELECT returns a one-row
        # result so the SafeSqlDriver-less driver is happy.
        await driver.execute_query("SELECT pg_notify('mcpg_listen_it', 'hello-from-pg')")

        # Wait up to a second for the listener loop to deliver.
        msgs = await manager.poll(sub_id, timeout_ms=1000, max_messages=10)
        assert [m.payload for m in msgs] == ["hello-from-pg"]
        assert msgs[0].channel == "mcpg_listen_it"
        assert msgs[0].delivered_at > 0
    finally:
        await manager.close()


async def test_unsubscribe_stops_delivery(connected_database: Database) -> None:
    settings = connected_database._settings
    manager = ListenManager(database_url=settings.database_url)
    try:
        sub_id = await manager.subscribe("mcpg_listen_it2")
        await manager.unsubscribe(sub_id)

        # Even though we publish, the queue is gone — there's nothing
        # to poll. The publish itself must still succeed.
        driver = connected_database.driver()
        await driver.execute_query("SELECT pg_notify('mcpg_listen_it2', 'lost')")
        # Give the loop a moment to (not) deliver anything.
        await asyncio.sleep(0.05)
        # poll on a removed sub raises rather than returning silently.
        from mcpg.listen import ListenError

        with pytest.raises(ListenError, match="no such subscription"):
            await manager.poll(sub_id, timeout_ms=10)
    finally:
        await manager.close()

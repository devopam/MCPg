"""Tests for the redis_fdw coverage module."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.redis_fdw import (
    RecommendRedisCacheTargetsResult,
    RedisCacheRecommendation,
    RedisCacheStats,
    RedisCacheTableInfo,
    RedisFdwError,
    RedisForeignServer,
    create_redis_cache_server,
    create_redis_cache_table,
    create_redis_user_mapping,
    describe_redis_cache_table,
    get_redis_cache_stats,
    list_redis_foreign_servers,
    recommend_redis_cache_targets,
)


class _StaticSecrets:
    """Trivial secrets provider double — returns a fixed value for one name."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def get(self, name: str) -> str | None:
        return self._mapping.get(name)


# --- list_redis_foreign_servers --------------------------------------------


async def test_list_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    assert await list_redis_foreign_servers(driver) == []  # type: ignore[arg-type]


async def test_list_parses_server_options_and_flags_password() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "FROM pg_foreign_server s ": [
                {
                    "name": "redis_primary",
                    "options": ["address=redis.internal", "port=6379", "database=0", "tls=true"],
                },
                {
                    "name": "redis_secondary",
                    "options": ["address=10.0.0.5", "port=6380", "database=1", "tls=false"],
                },
            ],
            "FROM pg_user_mappings WHERE srvname": [
                {"srvname": "redis_primary"},
            ],
        }
    )
    servers = await list_redis_foreign_servers(driver)  # type: ignore[arg-type]
    assert servers == [
        RedisForeignServer(
            name="redis_primary",
            address="redis.internal",
            port=6379,
            database=0,
            tls=True,
            password_configured=True,
            options={"address": "redis.internal", "port": "6379", "database": "0", "tls": "true"},
        ),
        RedisForeignServer(
            name="redis_secondary",
            address="10.0.0.5",
            port=6380,
            database=1,
            tls=False,
            password_configured=False,
            options={"address": "10.0.0.5", "port": "6380", "database": "1", "tls": "false"},
        ),
    ]


# --- describe_redis_cache_table --------------------------------------------


async def test_describe_returns_table_shape_with_columns() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_foreign_table ft ": [
                {
                    "name": "sessions_cache",
                    "server": "redis_primary",
                    "options": ["tabletype=hash", "keyprefix=session:", "ttl=3600"],
                }
            ],
            "FROM pg_attribute a ": [
                {"name": "key", "data_type": "text"},
                {"name": "value", "data_type": "text"},
            ],
        }
    )
    info = await describe_redis_cache_table(driver, "public", "sessions_cache")  # type: ignore[arg-type]
    assert info == RedisCacheTableInfo(
        schema="public",
        name="sessions_cache",
        server="redis_primary",
        key_type="hash",
        key_prefix="session:",
        ttl_seconds=3600,
        columns=[{"name": "key", "data_type": "text"}, {"name": "value", "data_type": "text"}],
        options={"tabletype": "hash", "keyprefix": "session:", "ttl": "3600"},
    )


async def test_describe_raises_for_missing_table() -> None:
    driver = FakeRoutingDriver({"FROM pg_foreign_table ft ": []})
    with pytest.raises(RedisFdwError, match="no redis_fdw foreign table"):
        await describe_redis_cache_table(driver, "public", "ghost")  # type: ignore[arg-type]


# --- get_redis_cache_stats -------------------------------------------------


async def test_get_stats_reports_unavailable_when_server_exists() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_foreign_server s ": [{"srvname": "redis_primary"}],
        }
    )
    stats = await get_redis_cache_stats(driver, "redis_primary")  # type: ignore[arg-type]
    assert stats == RedisCacheStats(
        server="redis_primary",
        available=False,
        key_count=None,
        used_memory_bytes=None,
        detail=("redis_fdw does not expose uniform stats SQL — query Redis directly (INFO / DBSIZE) for live metrics"),
    )


async def test_get_stats_raises_for_missing_server() -> None:
    driver = FakeRoutingDriver({"FROM pg_foreign_server s ": []})
    with pytest.raises(RedisFdwError, match="no redis_fdw foreign server"):
        await get_redis_cache_stats(driver, "ghost")  # type: ignore[arg-type]


# --- create_redis_cache_server ---------------------------------------------


async def test_create_server_emits_create_server_with_options() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    result = await create_redis_cache_server(
        driver,  # type: ignore[arg-type]
        name="redis_primary",
        address="redis.internal",
        port=6379,
        database=0,
        tls=True,
    )
    assert result.created is True
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'CREATE SERVER IF NOT EXISTS "redis_primary" FOREIGN DATA WRAPPER redis_fdw' in queries
    assert "address 'redis.internal'" in queries
    assert "port '6379'" in queries
    assert "tls 'true'" in queries


async def test_create_server_rejects_bad_identifier() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="not a valid unquoted SQL identifier"):
        await create_redis_cache_server(driver, name="redis; DROP", address="redis.internal")  # type: ignore[arg-type]


async def test_create_server_rejects_address_with_quotes() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="address contains characters"):
        await create_redis_cache_server(driver, name="r", address="evil'; DROP --")  # type: ignore[arg-type]


async def test_create_server_rejects_insecure_tls_on_remote_host() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="refusing tls=False"):
        await create_redis_cache_server(
            driver,  # type: ignore[arg-type]
            name="redis_primary",
            address="redis.internal",
            tls=False,
        )


async def test_create_server_allows_insecure_tls_on_loopback() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    result = await create_redis_cache_server(
        driver,  # type: ignore[arg-type]
        name="local_redis",
        address="127.0.0.1",
        tls=False,
    )
    assert result.created is True


async def test_create_server_rejects_bad_port() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="port must be"):
        await create_redis_cache_server(driver, name="r", address="127.0.0.1", port=0)  # type: ignore[arg-type]


async def test_create_server_requires_extension_installed() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    with pytest.raises(RedisFdwError, match="redis_fdw extension is not installed"):
        await create_redis_cache_server(driver, name="r", address="127.0.0.1")  # type: ignore[arg-type]


# --- create_redis_user_mapping ---------------------------------------------


async def test_create_user_mapping_resolves_secret_and_escapes_quotes() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    secrets = _StaticSecrets({"REDIS_PASSWORD": "s3'cret"})
    result = await create_redis_user_mapping(
        driver,  # type: ignore[arg-type]
        server="redis_primary",
        user="public",
        secret_ref="REDIS_PASSWORD",
        secrets=secrets,  # type: ignore[arg-type]
    )
    assert result.created is True
    assert result.secret_ref == "REDIS_PASSWORD"
    queries = " | ".join(call[0] for call in driver.calls)
    assert "CREATE USER MAPPING IF NOT EXISTS FOR PUBLIC" in queries
    assert 'SERVER "redis_primary"' in queries
    # Quotes inside the secret are doubled (SQL string escape).
    assert "password 's3''cret'" in queries


async def test_create_user_mapping_rejects_unresolved_secret() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    secrets = _StaticSecrets({})
    with pytest.raises(RedisFdwError, match="did not resolve"):
        await create_redis_user_mapping(
            driver,  # type: ignore[arg-type]
            server="redis_primary",
            user="public",
            secret_ref="MISSING",
            secrets=secrets,  # type: ignore[arg-type]
        )


async def test_create_user_mapping_rejects_control_characters_in_password() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    secrets = _StaticSecrets({"BAD": "abc\nDROP"})
    with pytest.raises(RedisFdwError, match="control characters"):
        await create_redis_user_mapping(
            driver,  # type: ignore[arg-type]
            server="redis_primary",
            user="public",
            secret_ref="BAD",
            secrets=secrets,  # type: ignore[arg-type]
        )


async def test_create_user_mapping_validates_user_identifier() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    secrets = _StaticSecrets({"P": "x"})
    with pytest.raises(RedisFdwError, match="user name"):
        await create_redis_user_mapping(
            driver,  # type: ignore[arg-type]
            server="redis_primary",
            user="bad; user",
            secret_ref="P",
            secrets=secrets,  # type: ignore[arg-type]
        )


# --- create_redis_cache_table ----------------------------------------------


async def test_create_table_emits_create_foreign_table_with_options() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    result = await create_redis_cache_table(
        driver,  # type: ignore[arg-type]
        schema="public",
        name="sessions_cache",
        server="redis_primary",
        key_type="hash",
        columns=[{"name": "key", "type": "text"}, {"name": "value", "type": "text"}],
        key_prefix="session:",
        ttl_seconds=3600,
    )
    assert result.created is True
    assert result.columns == ("key", "value")
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'CREATE FOREIGN TABLE IF NOT EXISTS "public"."sessions_cache"' in queries
    assert '"key" text, "value" text' in queries
    assert "tabletype 'hash'" in queries
    assert "keyprefix 'session:'" in queries
    assert "ttl '3600'" in queries


async def test_create_table_rejects_unknown_key_type() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="key_type must be one of"):
        await create_redis_cache_table(
            driver,  # type: ignore[arg-type]
            schema="public",
            name="t",
            server="redis_primary",
            key_type="json",
            columns=[{"name": "k", "type": "text"}],
        )


async def test_create_table_rejects_bad_column_type() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="column 'k' type"):
        await create_redis_cache_table(
            driver,  # type: ignore[arg-type]
            schema="public",
            name="t",
            server="redis_primary",
            key_type="hash",
            columns=[{"name": "k", "type": "text'); DROP --"}],
        )


async def test_create_table_requires_at_least_one_column() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(RedisFdwError, match="at least one column"):
        await create_redis_cache_table(
            driver,  # type: ignore[arg-type]
            schema="public",
            name="t",
            server="redis_primary",
            key_type="hash",
            columns=[],
        )


# --- recommend_redis_cache_targets -----------------------------------------


async def test_recommend_filters_by_ratio_reads_and_size() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_stat_user_tables s ": [
                # 1000 reads + 0 writes → infinite ratio, qualifies.
                {"schema": "public", "table_name": "ref_data", "reads": 5000, "writes": 0, "est_rows": 500},
                # 100 reads → below min_reads_per_day default.
                {"schema": "public", "table_name": "cold_table", "reads": 100, "writes": 0, "est_rows": 100},
                # 10M rows → exceeds max_rows.
                {"schema": "public", "table_name": "big", "reads": 100_000, "writes": 0, "est_rows": 10_000_000},
                # ratio = 5 → below min_read_write_ratio default.
                {"schema": "public", "table_name": "even", "reads": 5000, "writes": 1000, "est_rows": 1000},
                # ratio = 50, reads = 50000 → qualifies, small.
                {"schema": "public", "table_name": "lookup", "reads": 50_000, "writes": 1000, "est_rows": 5_000},
            ],
        }
    )
    result = await recommend_redis_cache_targets(driver, server="redis_primary")  # type: ignore[arg-type]
    assert isinstance(result, RecommendRedisCacheTargetsResult)
    names = [c.table for c in result.candidates]
    assert names == ["lookup", "ref_data"]
    # Sanity-check the read_only_lookup_table classifier on the writes=0 row.
    assert any(c.reason == "read_only_lookup_table" and c.table == "ref_data" for c in result.candidates)
    # Generated stub uses the supplied server name.
    assert all('SERVER "redis_primary"' in c.ready_to_run_sql for c in result.candidates)


async def test_recommend_uses_placeholder_when_server_not_given() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_stat_user_tables s ": [
                {"schema": "public", "table_name": "ref", "reads": 10_000, "writes": 0, "est_rows": 100},
            ],
        }
    )
    result = await recommend_redis_cache_targets(driver)  # type: ignore[arg-type]
    assert result.server is None
    assert "<configure-redis-server>" in result.candidates[0].ready_to_run_sql


async def test_recommend_rejects_bad_thresholds() -> None:
    driver = FakeRoutingDriver({"FROM pg_stat_user_tables s ": []})
    with pytest.raises(RedisFdwError, match="min_read_write_ratio"):
        await recommend_redis_cache_targets(driver, min_read_write_ratio=0)  # type: ignore[arg-type]


def test_recommendation_dataclass_shape() -> None:
    rec = RedisCacheRecommendation(
        schema="public",
        table="users",
        reads=10,
        writes=1,
        read_write_ratio=10.0,
        estimated_row_count=100,
        reason="small_hot_relation",
        ready_to_run_sql="CREATE FOREIGN TABLE ...",
    )
    assert rec.reason == "small_hot_relation"

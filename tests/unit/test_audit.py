"""Tests for the DBA Database Performance Auditor and its MCP tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.audit import audit_database
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_audit_database_with_clean_metrics_yields_score_100() -> None:
    # Set up FakeRoutingDriver with normal/optimal values
    driver = FakeRoutingDriver(
        {
            "SELECT version()": [{"version": "PostgreSQL 16.4 on x86_64", "dbname": "prod_db"}],
            "blks_hit": [
                {"hits": 999000, "reads": 1000}  # Hit ratio = 99.9%
            ],
            "checkpoints_timed": [
                {
                    "checkpoints_timed": 95,
                    "checkpoints_req": 5,
                    "buffers_checkpoint": 1000,
                    "buffers_clean": 2000,
                    "maxwritten_clean": 10,
                    "buffers_backend": 50,
                    "buffers_backend_fsync": 0,
                }
            ],
            "temp_files": [
                {"temp_files": 0, "temp_bytes": 0}  # Zero temp files
            ],
            "xact_commit": [
                {"commits": 100000, "rollbacks": 10}  # Rollback = 0.01%
            ],
            "long_active": [{"long_active": 0, "long_idle": 0}],
            "prepared_count": [{"prepared_count": 0}],
            "used": [
                {"used": 15, "maximum": 100}  # 15% saturation
            ],
            "count_waiting": [{"count_waiting": 0}],
            "max_wait": [{"max_wait": 0.0}],
            "deadlocks": [{"deadlocks": 0}],
            "bloated_tables": [
                {"bloated_tables": 0, "max_bloat_pct": 2.1}  # No bloated tables
            ],
            "invalid": [{"invalid": 0}],
            "long_queries": [{"long_queries": 0}],
            "pg_stat_statements": [
                # Emulating pg_stat_statements being enabled but clean
                {
                    "query": "SELECT 1",
                    "calls": 100,
                    "total_exec_time": 200.0,
                    "mean_exec_time": 2.0,
                }
            ],
        }
    )

    report = await audit_database(driver, "public")  # type: ignore[arg-type]

    assert report.database == "prod_db"
    assert "PostgreSQL 16.4" in report.version
    assert report.overall_health == "GOOD"
    assert report.health_score == 100
    assert len(report.categories) == 5

    # Check Cache Hit metric
    cache_cat = next(c for c in report.categories if c.category == "Memory & I/O Efficiency")
    assert cache_cat.score == 100
    assert cache_cat.status == "GOOD"
    hit_metric = next(m for m in cache_cat.metrics if m.name == "Buffer Cache Hit Ratio")
    assert hit_metric.value == 99.9
    assert hit_metric.status == "GOOD"
    assert hit_metric.severity == 0


async def test_audit_database_with_failing_metrics_drops_scores() -> None:
    # Set up FakeRoutingDriver with warning and critical values
    driver = FakeRoutingDriver(
        {
            "SELECT version()": [{"version": "PostgreSQL 16.4", "dbname": "prod_db"}],
            "blks_hit": [
                {"hits": 95000, "reads": 5000}  # Hit ratio = 95% -> warning (-10)
            ],
            "checkpoints_timed": [
                {
                    "checkpoints_timed": 50,
                    "checkpoints_req": 50,  # 50% requested -> warning (-10)
                    "buffers_checkpoint": 1000,
                    "buffers_clean": 100,
                    "maxwritten_clean": 10,
                    "buffers_backend": 500,  # backend writes > bgwriter -> warning
                    "buffers_backend_fsync": 10,  # backend syncs -> warning
                }
            ],
            "temp_files": [
                {"temp_files": 47, "temp_bytes": 2450000000}  # CRITICAL temp file spills (-15)
            ],
            "xact_commit": [
                {"commits": 1000, "rollbacks": 12}  # Rollback = 1.2% -> warning (-10)
            ],
            "long_active": [{"long_active": 5, "long_idle": 2}],
            "prepared_count": [
                {"prepared_count": 2}  # Dangling prepared -> CRITICAL (-15)
            ],
            "used": [
                {"used": 85, "maximum": 100}  # 85% saturation -> CRITICAL (-15)
            ],
            "count_waiting": [
                {"count_waiting": 17}  # lock wait count -> CRITICAL (-20)
            ],
            "max_wait": [
                {"max_wait": 1240.0}  # lock wait duration -> CRITICAL (-15)
            ],
            "deadlocks": [
                {"deadlocks": 3}  # Deadlocks -> warning (-5)
            ],
            "bloated_tables": [
                {"bloated_tables": 2, "max_bloat_pct": 24.1}  # bloated tables -> CRITICAL (-15)
            ],
            "invalid": [
                {"invalid": 2}  # invalid indexes -> CRITICAL (-15)
            ],
            "long_queries": [
                {"long_queries": 8}  # queries > 60s -> CRITICAL (-15)
            ],
            "pg_stat_statements": [
                # High query calls
                {
                    "query": "SELECT * FROM orders JOIN line_items ON orders.id = line_items.order_id",
                    "calls": 5000,
                    "total_exec_time": 450000.0,
                    "mean_exec_time": 90.0,
                }
            ],
        }
    )

    report = await audit_database(driver, "public")  # type: ignore[arg-type]

    # Scores should decline heavily due to warnings/critical metrics
    assert report.health_score < 70
    assert report.overall_health == "CRITICAL"

    # Memory & I/O score should drop due to hit ratio and temp files
    mem_cat = next(c for c in report.categories if c.category == "Memory & I/O Efficiency")
    assert mem_cat.status == "CRITICAL"
    assert mem_cat.score < 80

    # Ensure critical findings are reported in top issues
    assert len(report.top_issues) > 0
    assert any("Buffer Cache Hit Ratio" in issue.issue for issue in report.top_issues)
    assert any("Temporary File Usage" in issue.issue for issue in report.top_issues)
    assert any("Prepared Transactions" in issue.issue for issue in report.top_issues)
    assert any("Lock Wait Count" in issue.issue for issue in report.top_issues)
    assert any("Invalid Index Count" in issue.issue for issue in report.top_issues)

    # Recommmendations should suggest clean-ups
    assert len(report.recommendations) > 0
    assert any("Kill" in rec.action or "autovacuum" in rec.action.lower() for rec in report.recommendations)


async def test_audit_database_log_scanning_skips_if_not_present() -> None:
    driver = FakeRoutingDriver(
        {
            "SELECT version()": [{"version": "PostgreSQL 16.4", "dbname": "prod"}],
            "SELECT 1 FROM pg_tables": [],  # Simulate log table doesn't exist in catalogs
        }
    )

    report = await audit_database(driver, "public", log_table="public.missing_log_table")  # type: ignore[arg-type]
    # Should run successfully without throwing a database error
    assert report.database == "prod"
    # No log-related issues in top issues list
    assert not any("Logs" in issue.affected_component for issue in report.top_issues)


async def test_audit_database_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "audit_database" in listed

        result = await client.call_tool("audit_database", {"schema": "public"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["overall_health"] in ("GOOD", "WARNING", "CRITICAL")


async def test_audit_database_log_scanning_escapes_identifiers() -> None:
    driver = FakeRoutingDriver(
        {
            "SELECT version()": [{"version": "PostgreSQL 16.4", "dbname": "prod"}],
            "SELECT 1 FROM pg_tables": [{"exists": 1}],
            "SELECT error_severity": [{"error_severity": "ERROR", "count": 2}],
        }
    )

    report = await audit_database(driver, "public", log_table='public."injected;--')  # type: ignore[arg-type]
    assert report.database == "prod"

    # Verify that the pg_tables check ran with unescaped identifiers in parameters
    pg_tables_call = next(c for c in driver.calls if "pg_tables" in c[0])
    assert pg_tables_call[1] == ["public", '"injected;--']

    # Verify that the actual log query is safely double-quoted and escaped
    log_query_call = next(c for c in driver.calls if "error_severity" in c[0])
    assert 'FROM "public"."""injected;--"' in log_query_call[0]


async def test_audit_database_with_pg17_checkpointer_compatibility() -> None:
    # Set up FakeRoutingDriver with checkpointer views present
    driver = FakeRoutingDriver(
        {
            "SELECT version()": [{"version": "PostgreSQL 17.1", "dbname": "prod"}],
            "pg_views": [{"exists": 1}],  # Simulate view exists
            "pg_stat_checkpointer": [
                {
                    "checkpoints_timed": 95,
                    "checkpoints_req": 5,
                    "buffers_checkpoint": 1000,
                    "buffers_clean": 2000,
                    "maxwritten_clean": 10,
                    "buffers_backend": 0,
                    "buffers_backend_fsync": 0,
                }
            ],
            "blks_hit": [{"hits": 999000, "reads": 1000}],
            "temp_files": [{"temp_files": 0, "temp_bytes": 0}],
            "xact_commit": [{"commits": 100000, "rollbacks": 10}],
            "long_active": [{"long_active": 0, "long_idle": 0}],
            "prepared_count": [{"prepared_count": 0}],
            "used": [{"used": 15, "maximum": 100}],
            "count_waiting": [{"count_waiting": 0}],
            "max_wait": [{"max_wait": 0.0}],
            "deadlocks": [{"deadlocks": 0}],
            "bloated_tables": [{"bloated_tables": 0, "max_bloat_pct": 2.1}],
            "invalid": [{"invalid": 0}],
            "long_queries": [{"long_queries": 0}],
            "pg_stat_statements": [
                {
                    "query": "SELECT 1",
                    "calls": 100,
                    "total_exec_time": 200.0,
                    "mean_exec_time": 2.0,
                }
            ],
        }
    )

    report = await audit_database(driver, "public")  # type: ignore[arg-type]
    assert report.database == "prod"
    assert report.overall_health == "GOOD"
    assert report.health_score == 100

    # Verify that it queried pg_views to find pg_stat_checkpointer
    assert any("pg_views" in c[0] for c in driver.calls)
    # Verify that it queried the combined pg_stat_checkpointer query
    assert any("pg_stat_checkpointer" in c[0] for c in driver.calls)

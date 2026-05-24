"""Tests for the schema-advisor rules and their MCP tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.advisors import (
    RULE_DUPLICATE_INDEXES,
    RULE_MISSING_PRIMARY_KEY,
    RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
    RULE_UNINDEXED_FOREIGN_KEY,
    AdvisorReport,
    Finding,
    _duplicate_indexes,
    _missing_primary_keys,
    _nullable_timestamps_without_tz,
    _unindexed_foreign_keys,
    run_advisors,
)
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- _missing_primary_keys -------------------------------------------------


async def test_missing_primary_keys_flags_each_table_without_a_pk() -> None:
    driver = FakeRoutingDriver({"FROM pg_class c": [{"table_name": "orphan_a"}, {"table_name": "orphan_b"}]})

    findings = await _missing_primary_keys(driver, "app")  # type: ignore[arg-type]

    assert findings == [
        Finding(
            rule=RULE_MISSING_PRIMARY_KEY,
            severity="warning",
            object="app.orphan_a",
            message="table has no PRIMARY KEY constraint",
        ),
        Finding(
            rule=RULE_MISSING_PRIMARY_KEY,
            severity="warning",
            object="app.orphan_b",
            message="table has no PRIMARY KEY constraint",
        ),
    ]


async def test_missing_primary_keys_returns_empty_list_when_every_table_has_a_pk() -> None:
    assert await _missing_primary_keys(FakeRoutingDriver({}), "app") == []  # type: ignore[arg-type]


# --- _unindexed_foreign_keys -----------------------------------------------


async def test_unindexed_foreign_keys_flags_fk_without_a_leading_index() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_constraint con": [
                {"fk_name": "order_widget_fk", "table_name": "order_item", "first_column": "widget_id"}
            ]
        }
    )

    findings = await _unindexed_foreign_keys(driver, "app")  # type: ignore[arg-type]

    assert len(findings) == 1
    assert findings[0].rule == RULE_UNINDEXED_FOREIGN_KEY
    assert findings[0].severity == "warning"
    assert findings[0].object == "app.order_item.widget_id"
    assert "order_widget_fk" in findings[0].message
    assert "widget_id" in findings[0].message


async def test_unindexed_foreign_keys_returns_empty_when_every_fk_is_indexed() -> None:
    assert await _unindexed_foreign_keys(FakeRoutingDriver({}), "app") == []  # type: ignore[arg-type]


# --- _duplicate_indexes ----------------------------------------------------


async def test_duplicate_indexes_flags_each_redundant_index_pair() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_index ix1": [
                {"index_a": "widget_name_idx", "index_b": "widget_name_dup_idx", "table_name": "widget"}
            ]
        }
    )

    findings = await _duplicate_indexes(driver, "app")  # type: ignore[arg-type]

    assert len(findings) == 1
    assert findings[0].rule == RULE_DUPLICATE_INDEXES
    assert findings[0].object == "app.widget_name_idx vs app.widget_name_dup_idx"
    assert "identical columns" in findings[0].message


# --- _nullable_timestamps_without_tz --------------------------------------


async def test_nullable_timestamps_without_tz_flags_each_column() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_attribute att": [
                {"table_name": "widget", "column_name": "created"},
                {"table_name": "widget", "column_name": "updated"},
            ]
        }
    )

    findings = await _nullable_timestamps_without_tz(driver, "app")  # type: ignore[arg-type]

    assert {finding.object for finding in findings} == {"app.widget.created", "app.widget.updated"}
    assert all(finding.rule == RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ for finding in findings)
    assert all(finding.severity == "info" for finding in findings)


# --- run_advisors aggregator + tool wiring --------------------------------


async def test_run_advisors_aggregates_every_rule_and_records_them_in_rules_run() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"table_name": "orphan"}],
            "FROM pg_constraint con": [
                {"fk_name": "order_widget_fk", "table_name": "order_item", "first_column": "widget_id"}
            ],
            "FROM pg_index ix1": [{"index_a": "a", "index_b": "b", "table_name": "widget"}],
            "FROM pg_attribute att": [{"table_name": "widget", "column_name": "created"}],
        }
    )

    report = await run_advisors(driver, "app")  # type: ignore[arg-type]

    assert isinstance(report, AdvisorReport)
    assert report.schema == "app"
    assert set(report.rules_run) == {
        RULE_MISSING_PRIMARY_KEY,
        RULE_UNINDEXED_FOREIGN_KEY,
        RULE_DUPLICATE_INDEXES,
        RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
    }
    rules_in_findings = {finding.rule for finding in report.findings}
    assert rules_in_findings == set(report.rules_run)


async def test_run_advisors_returns_an_empty_findings_list_for_a_clean_schema() -> None:
    report = await run_advisors(FakeRoutingDriver({}), "app")  # type: ignore[arg-type]

    assert report.schema == "app"
    assert report.findings == []
    assert len(report.rules_run) == 4


async def test_run_advisors_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "run_advisors" in listed

        result = await client.call_tool("run_advisors", {"schema": "public"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["schema"] == "public"
    assert result.structuredContent["findings"] == []
    assert len(result.structuredContent["rules_run"]) == 4

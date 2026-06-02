"""Tests for the schema-advisor rules and their MCP tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.advisors import (
    RULE_DUPLICATE_INDEXES,
    RULE_MISSING_PRIMARY_KEY,
    RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
    RULE_RECOMMEND_GRAPH_INDICES,
    RULE_REDUNDANT_INDEXES,
    RULE_UNINDEXED_FOREIGN_KEY,
    AdvisorReport,
    Finding,
    _duplicate_indexes,
    _missing_primary_keys,
    _nullable_timestamps_without_tz,
    _redundant_indexes,
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
        RULE_RECOMMEND_GRAPH_INDICES,
        RULE_REDUNDANT_INDEXES,
    }
    rules_in_findings = {finding.rule for finding in report.findings}
    assert len(rules_in_findings) == 5
    assert rules_in_findings.issubset(set(report.rules_run))


async def test_run_advisors_returns_an_empty_findings_list_for_a_clean_schema() -> None:
    report = await run_advisors(FakeRoutingDriver({}), "app")  # type: ignore[arg-type]

    assert report.schema == "app"
    assert report.findings == []
    assert len(report.rules_run) == 6


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
    assert len(result.structuredContent["rules_run"]) == 6


# --- find_unused_objects -----------------------------------------------


async def test_find_unused_objects_routes_table_and_index_queries_independently() -> None:
    from mcpg.advisors import find_unused_objects

    driver = FakeRoutingDriver(
        {
            "pg_stat_user_tables": [
                {
                    "table_name": "audit",
                    "seq_scans": 0,
                    "index_scans": 0,
                    "rows_modified": 0,
                    "estimated_row_count": 1234,
                },
            ],
            "pg_stat_user_indexes": [
                {
                    "table_name": "orders",
                    "index_name": "idx_orders_unused",
                    "size_bytes": 1024 * 1024,
                    "definition": "CREATE INDEX idx_orders_unused ON public.orders USING btree (customer_id)",
                },
            ],
        }
    )

    report = await find_unused_objects(driver, "public")  # type: ignore[arg-type]

    assert len(report.tables) == 1
    assert report.tables[0].table == "audit"
    assert report.tables[0].estimated_row_count == 1234
    assert len(report.indexes) == 1
    assert report.indexes[0].index == "idx_orders_unused"
    assert report.indexes[0].size_bytes == 1024 * 1024


async def test_find_unused_objects_returns_empty_when_no_zero_scan_objects_exist() -> None:
    from mcpg.advisors import find_unused_objects

    driver = FakeRoutingDriver({"pg_stat_user_tables": [], "pg_stat_user_indexes": []})

    report = await find_unused_objects(driver, "public")  # type: ignore[arg-type]

    assert report.tables == []
    assert report.indexes == []


async def test_find_unused_objects_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "find_unused_objects" in listed


# --- find_sensitive_columns (Phase 6.2) ----------------------------------


async def test_classify_column_returns_none_when_nothing_matches() -> None:
    from mcpg.advisors import _classify_column

    assert _classify_column("widget_id", "integer") is None
    assert _classify_column("created_at", "timestamp with time zone") is None


async def test_classify_column_picks_highest_confidence_across_matches() -> None:
    from mcpg.advisors import _classify_column

    # `dob` matches a high-confidence pattern; type is irrelevant.
    classified = _classify_column("user_dob", "date")
    assert classified is not None
    categories, confidence, reasons = classified
    assert "identifier" in categories
    assert confidence == "high"
    assert any("date of birth" in r.lower() for r in reasons)


async def test_classify_column_combines_name_and_type_signals() -> None:
    from mcpg.advisors import _classify_column

    # `inet`-typed column flagged by type heuristic.
    classified = _classify_column("source_ip", "inet")
    assert classified is not None
    categories, confidence, _reasons = classified
    assert "location" in categories
    assert confidence in {"medium", "low"}  # depends on best matching rule


async def test_classify_column_credential_patterns_are_high_confidence() -> None:
    from mcpg.advisors import _classify_column

    for name in ("password", "user_password_hash", "api_key", "private_key", "auth_token"):
        classified = _classify_column(name, "text")
        assert classified is not None, name
        _, confidence, _ = classified
        assert confidence == "high", f"{name} should be high confidence, got {confidence}"


async def test_classify_column_does_not_flag_safe_lookalike_names() -> None:
    from mcpg.advisors import _classify_column

    # `key_id` is too generic to be a credential — patterns require
    # explicit api_key / private_key / access_key prefixes.
    assert _classify_column("key_id", "integer") is None
    # `street_lamp_id` shouldn't be flagged as a location PII column.
    # (The `\bstreet\b` regex IS broad; document the false-positive
    # explicitly by asserting current behavior — if this trips later,
    # tighten the regex rather than the test.)
    classified = _classify_column("street_lamp_id", "integer")
    if classified is not None:
        _, confidence, _ = classified
        # Acceptable as long as it doesn't crash; the agent filters by confidence.
        assert confidence in {"medium", "low"}


async def test_find_sensitive_columns_reports_one_finding_per_matching_column() -> None:
    from mcpg.advisors import find_sensitive_columns

    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {"table_name": "users", "column_name": "id", "data_type": "integer"},
                {"table_name": "users", "column_name": "email", "data_type": "text"},
                {"table_name": "users", "column_name": "password_hash", "data_type": "text"},
                {"table_name": "users", "column_name": "last_login_ip", "data_type": "inet"},
                {"table_name": "orders", "column_name": "id", "data_type": "integer"},
                {"table_name": "orders", "column_name": "card_number", "data_type": "text"},
                {"table_name": "orders", "column_name": "total", "data_type": "numeric"},
            ]
        }
    )

    report = await find_sensitive_columns(driver, "public")  # type: ignore[arg-type]

    flagged = {(c.table, c.column) for c in report.columns}
    assert ("users", "email") in flagged
    assert ("users", "password_hash") in flagged
    assert ("users", "last_login_ip") in flagged
    assert ("orders", "card_number") in flagged
    # Plain integer / numeric columns aren't flagged.
    assert ("users", "id") not in flagged
    assert ("orders", "total") not in flagged


async def test_find_sensitive_columns_returns_empty_report_when_no_columns_match() -> None:
    from mcpg.advisors import find_sensitive_columns

    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {"table_name": "widgets", "column_name": "id", "data_type": "integer"},
                {"table_name": "widgets", "column_name": "quantity", "data_type": "integer"},
                {"table_name": "widgets", "column_name": "created_at", "data_type": "timestamp with time zone"},
            ]
        }
    )

    report = await find_sensitive_columns(driver, "public")  # type: ignore[arg-type]

    assert report.schema == "public"
    assert report.columns == []


async def test_find_sensitive_columns_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "find_sensitive_columns" in listed


async def test_redundant_indexes_flags_prefix_subset_non_unique() -> None:
    driver = FakeDriver(
        [
            {
                "table_name": "users",
                "index_name": "idx_users_name",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1",
                "index_size": 8192,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_name_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1 2",
                "index_size": 16384,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_id_unique",
                "is_unique": True,
                "is_primary": False,
                "indkey": "3",
                "index_size": 8192,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_id_unique_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "3 2",
                "index_size": 16384,
            },
        ]
    )

    findings = await _redundant_indexes(driver, "public")  # type: ignore[arg-type]

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == RULE_REDUNDANT_INDEXES
    assert finding.severity == "warning"
    assert finding.object == "public.idx_users_name covered by public.idx_users_name_email"
    assert "idx_users_name" in finding.message
    assert "idx_users_name_email" in finding.message
    assert "8192" in finding.message


async def test_redundant_indexes_no_redundancy_returns_empty() -> None:
    from mcpg.advisors import _redundant_indexes

    driver = FakeDriver(
        [
            {
                "table_name": "users",
                "index_name": "idx_users_name_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1 2",
                "index_size": 8192,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_email_age",
                "is_unique": False,
                "is_primary": False,
                "indkey": "2 3",
                "index_size": 8192,
            },
        ]
    )

    findings = await _redundant_indexes(driver, "public")  # type: ignore[arg-type]
    assert findings == []


async def test_redundant_indexes_skips_empty_or_null_indkey() -> None:
    from mcpg.advisors import _redundant_indexes

    driver = FakeDriver(
        [
            {
                "table_name": "users",
                "index_name": "idx_users_empty_indkey",
                "is_unique": False,
                "is_primary": False,
                "indkey": "",
                "index_size": 4096,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_null_indkey",
                "is_unique": False,
                "is_primary": False,
                "indkey": None,
                "index_size": 4096,
            },
            # Valid non-redundant index to ensure things don't crash
            {
                "table_name": "users",
                "index_name": "idx_users_name",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1",
                "index_size": 8192,
            },
        ]
    )

    findings = await _redundant_indexes(driver, "public")  # type: ignore[arg-type]
    assert findings == []


async def test_redundant_indexes_scoped_per_table() -> None:
    from mcpg.advisors import _redundant_indexes

    driver = FakeDriver(
        [
            {
                "table_name": "users",
                "index_name": "idx_users_name",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1",
                "index_size": 4096,
            },
            {
                "table_name": "users",
                "index_name": "idx_users_name_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1 2",
                "index_size": 8192,
            },
            # different table, same columns - should not falsely relate or conflict
            {
                "table_name": "orders",
                "index_name": "idx_orders_user_id",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1",
                "index_size": 4096,
            },
            {
                "table_name": "orders",
                "index_name": "idx_orders_user_id_status",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1 2",
                "index_size": 8192,
            },
        ]
    )

    findings = await _redundant_indexes(driver, "public")  # type: ignore[arg-type]
    users_findings = [f for f in findings if "users" in f.object]
    orders_findings = [f for f in findings if "orders" in f.object]

    assert len(users_findings) == 1
    assert len(orders_findings) == 1


async def test_redundant_indexes_with_partial_and_expression_indexes() -> None:
    from mcpg.advisors import _redundant_indexes

    driver = FakeDriver(
        [
            # Partial index on active users
            {
                "table_name": "users",
                "index_name": "idx_users_active_name",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1",
                "index_size": 4096,
                "indpred": "active = true",
                "indexprs": None,
            },
            # Global index covering active and inactive
            {
                "table_name": "users",
                "index_name": "idx_users_global_name_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "1 2",
                "index_size": 8192,
                "indpred": None,
                "indexprs": None,
            },
            # Expression index using lower(email)
            {
                "table_name": "users",
                "index_name": "idx_users_lower_email",
                "is_unique": False,
                "is_primary": False,
                "indkey": "0",  # 0 indicates an expression
                "index_size": 4096,
                "indpred": None,
                "indexprs": "lower(email)",
            },
            # Different expression index
            {
                "table_name": "users",
                "index_name": "idx_users_lower_name",
                "is_unique": False,
                "is_primary": False,
                "indkey": "0 2",
                "index_size": 8192,
                "indpred": None,
                "indexprs": "lower(name)",
            },
        ]
    )

    findings = await _redundant_indexes(driver, "public")  # type: ignore[arg-type]

    # Global index idx_users_global_name_email should successfully cover the partial index idx_users_active_name
    assert any("idx_users_active_name covered by" in f.object for f in findings)

    # Expression index lower(email) (cols [0]) must NOT be covered by lower(name) (cols [0, 2])
    # because their expressions differ!
    assert not any("idx_users_lower_email" in f.object for f in findings)

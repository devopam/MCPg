"""Tests for schema-introspection queries and their MCP tools."""

from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import (
    AvailableExtension,
    ColumnInfo,
    ConstraintInfo,
    ExtensionInfo,
    FunctionInfo,
    GrantInfo,
    IndexInfo,
    PartitionInfo,
    PartitionSet,
    PolicyInfo,
    PolicySet,
    RoleInfo,
    SchemaInfo,
    SequenceInfo,
    TableInfo,
    TriggerInfo,
    ViewInfo,
    describe_table,
    list_available_extensions,
    list_constraints,
    list_extensions,
    list_functions,
    list_grants,
    list_indexes,
    list_partitions,
    list_policies,
    list_roles,
    list_schemas,
    list_sequences,
    list_tables,
    list_triggers,
    list_views,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- query logic, exercised with a fake driver -----------------------------


async def test_list_schemas_excludes_system_schemas_by_default() -> None:
    driver = FakeDriver(
        [
            {"schema_name": "public"},
            {"schema_name": "pg_catalog"},
            {"schema_name": "information_schema"},
            {"schema_name": "pg_temp_3"},
            {"schema_name": "app"},
        ]
    )

    assert await list_schemas(driver) == [SchemaInfo("public"), SchemaInfo("app")]


async def test_list_schemas_includes_system_schemas_when_requested() -> None:
    driver = FakeDriver([{"schema_name": "public"}, {"schema_name": "pg_catalog"}])

    assert await list_schemas(driver, include_system=True) == [
        SchemaInfo("public"),
        SchemaInfo("pg_catalog"),
    ]


async def test_list_tables_maps_rows_and_passes_schema_as_a_parameter() -> None:
    driver = FakeDriver([{"name": "widget", "relkind": "r", "is_partition": False}])

    result = await list_tables(driver, "app")

    assert result == [TableInfo("widget", "BASE TABLE", partitioned=False, is_partition=False)]
    # The schema must be bound as a parameter, never interpolated into SQL.
    assert driver.calls[0][1] == ["app"]


async def test_list_tables_flags_partitioned_tables_and_partitions() -> None:
    driver = FakeDriver(
        [
            {"name": "event", "relkind": "p", "is_partition": False},
            {"name": "event_2026", "relkind": "r", "is_partition": True},
        ]
    )

    assert await list_tables(driver, "app") == [
        TableInfo("event", "BASE TABLE", partitioned=True, is_partition=False),
        TableInfo("event_2026", "BASE TABLE", partitioned=False, is_partition=True),
    ]


def _column_row(name: str, data_type: str, **overrides: object) -> dict[str, object]:
    """A describe_table catalog row with sensible defaults."""
    row: dict[str, object] = {
        "column_name": name,
        "data_type": data_type,
        "nullable": True,
        "column_default": None,
        "type_name": "text",
        "type_mod": -1,
    }
    row.update(overrides)
    return row


async def test_describe_table_maps_columns_and_nullability() -> None:
    driver = FakeDriver(
        [
            _column_row("id", "integer", nullable=False, column_default="0", type_name="int4"),
            _column_row("note", "text", nullable=True, type_name="text"),
        ]
    )

    result = await describe_table(driver, "app", "widget")

    assert result == [
        ColumnInfo("id", "integer", nullable=False, default="0", vector_dimension=None),
        ColumnInfo("note", "text", nullable=True, default=None, vector_dimension=None),
    ]


async def test_describe_table_reports_pgvector_dimension() -> None:
    driver = FakeDriver([_column_row("embedding", "vector(384)", type_name="vector", type_mod=384)])

    result = await describe_table(driver, "app", "docs")

    assert result == [ColumnInfo("embedding", "vector(384)", nullable=True, default=None, vector_dimension=384)]


async def test_list_indexes_maps_rows_including_the_access_method() -> None:
    driver = FakeDriver(
        [
            {
                "name": "widget_pkey",
                "method": "btree",
                "relkind": "i",
                "definition": "CREATE UNIQUE INDEX widget_pkey ...",
            },
            {
                "name": "widget_doc_idx",
                "method": "gin",
                "relkind": "i",
                "definition": "CREATE INDEX widget_doc_idx ...",
            },
        ]
    )

    assert await list_indexes(driver, "app", "widget") == [
        IndexInfo("widget_pkey", "btree", "CREATE UNIQUE INDEX widget_pkey ...", partitioned=False),
        IndexInfo("widget_doc_idx", "gin", "CREATE INDEX widget_doc_idx ...", partitioned=False),
    ]


async def test_list_indexes_flags_a_partitioned_index() -> None:
    driver = FakeDriver(
        [{"name": "event_created_idx", "method": "btree", "relkind": "I", "definition": "CREATE INDEX ..."}]
    )

    assert await list_indexes(driver, "app", "event") == [
        IndexInfo("event_created_idx", "btree", "CREATE INDEX ...", partitioned=True)
    ]


async def test_list_views_maps_views_and_materialized_views() -> None:
    driver = FakeDriver(
        [
            {"name": "active_users", "materialized": False, "definition": "SELECT * FROM users"},
            {"name": "user_stats", "materialized": True, "definition": "SELECT count(*) FROM users"},
        ]
    )

    assert await list_views(driver, "app") == [
        ViewInfo("active_users", materialized=False, definition="SELECT * FROM users"),
        ViewInfo("user_stats", materialized=True, definition="SELECT count(*) FROM users"),
    ]


async def test_list_functions_maps_routines() -> None:
    driver = FakeDriver(
        [
            {
                "name": "widget_count",
                "kind_code": "f",
                "arguments": "",
                "returns": "bigint",
                "language": "sql",
            },
            {
                "name": "do_thing",
                "kind_code": "p",
                "arguments": "x integer",
                "returns": None,
                "language": "plpgsql",
            },
        ]
    )

    assert await list_functions(driver, "app") == [
        FunctionInfo("widget_count", "function", "", "bigint", "sql"),
        FunctionInfo("do_thing", "procedure", "x integer", None, "plpgsql"),
    ]


async def test_list_triggers_maps_rows() -> None:
    driver = FakeDriver(
        [{"name": "widget_bi", "function": "widget_touch", "definition": "CREATE TRIGGER widget_bi ..."}]
    )

    assert await list_triggers(driver, "app", "widget") == [
        TriggerInfo("widget_bi", "widget_touch", "CREATE TRIGGER widget_bi ...")
    ]


async def test_list_constraints_maps_constraint_types() -> None:
    driver = FakeDriver(
        [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"},
            {"name": "widget_cat_fk", "type_code": "f", "definition": "FOREIGN KEY (cat) REFERENCES c(id)"},
            {"name": "widget_price_chk", "type_code": "c", "definition": "CHECK (price > 0)"},
        ]
    )

    assert await list_constraints(driver, "app", "widget") == [
        ConstraintInfo("widget_pkey", "primary_key", "PRIMARY KEY (id)"),
        ConstraintInfo("widget_cat_fk", "foreign_key", "FOREIGN KEY (cat) REFERENCES c(id)"),
        ConstraintInfo("widget_price_chk", "check", "CHECK (price > 0)"),
    ]


async def test_list_constraints_maps_an_unknown_type_code_to_other() -> None:
    driver = FakeDriver([{"name": "x", "type_code": "z", "definition": "..."}])

    assert (await list_constraints(driver, "app", "t"))[0].type == "other"


async def test_list_partitions_describes_a_range_partitioned_table() -> None:
    driver = FakeDriver(
        [
            {
                "strategy_code": "r",
                "partition_name": "event_2026",
                "bounds": "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
            },
            {
                "strategy_code": "r",
                "partition_name": "event_2027",
                "bounds": "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')",
            },
        ]
    )

    result = await list_partitions(driver, "app", "event")

    assert result == PartitionSet(
        partitioned=True,
        strategy="range",
        partitions=[
            PartitionInfo("event_2026", "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"),
            PartitionInfo("event_2027", "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')"),
        ],
    )
    assert driver.calls[0][1] == ["app", "event"]


async def test_list_partitions_reports_a_partitioned_table_with_no_partitions() -> None:
    driver = FakeDriver([{"strategy_code": "l", "partition_name": None, "bounds": None}])

    result = await list_partitions(driver, "app", "event")

    assert result == PartitionSet(partitioned=True, strategy="list", partitions=[])


async def test_list_partitions_reports_a_plain_table_as_not_partitioned() -> None:
    driver = FakeDriver([])

    assert await list_partitions(driver, "app", "widget") == PartitionSet(
        partitioned=False, strategy=None, partitions=[]
    )


async def test_list_policies_maps_rows() -> None:
    driver = FakeDriver(
        [
            {
                "rls_enabled": True,
                "name": "widget_select",
                "command": "SELECT",
                "permissive": "PERMISSIVE",
                "roles": ["app_reader"],
                "using_expression": "(owner = current_user)",
                "check_expression": None,
            }
        ]
    )

    result = await list_policies(driver, "app", "widget")

    assert result == PolicySet(
        rls_enabled=True,
        policies=[
            PolicyInfo(
                name="widget_select",
                command="SELECT",
                permissive=True,
                roles=["app_reader"],
                using_expression="(owner = current_user)",
                check_expression=None,
            )
        ],
    )
    assert driver.calls[0][1] == ["app", "widget"]


async def test_list_policies_reports_a_restrictive_policy() -> None:
    driver = FakeDriver(
        [
            {
                "rls_enabled": True,
                "name": "widget_block",
                "command": "ALL",
                "permissive": "RESTRICTIVE",
                "roles": ["public"],
                "using_expression": "false",
                "check_expression": None,
            }
        ]
    )

    assert (await list_policies(driver, "app", "widget")).policies[0].permissive is False


async def test_list_policies_reports_a_table_with_no_policies() -> None:
    driver = FakeDriver(
        [
            {
                "rls_enabled": False,
                "name": None,
                "command": None,
                "permissive": None,
                "roles": None,
                "using_expression": None,
                "check_expression": None,
            }
        ]
    )

    assert await list_policies(driver, "app", "widget") == PolicySet(rls_enabled=False, policies=[])


async def test_list_policies_reports_a_missing_table_as_unsecured() -> None:
    assert await list_policies(FakeDriver([]), "app", "missing") == PolicySet(rls_enabled=False, policies=[])


async def test_list_grants_maps_rows() -> None:
    driver = FakeDriver(
        [
            {"grantee": "app_user", "privilege": "SELECT", "is_grantable": "NO", "grantor": "app_owner"},
            {"grantee": "app_admin", "privilege": "UPDATE", "is_grantable": "YES", "grantor": "app_owner"},
        ]
    )

    assert await list_grants(driver, "app", "widget") == [
        GrantInfo("app_user", "SELECT", grantable=False, grantor="app_owner"),
        GrantInfo("app_admin", "UPDATE", grantable=True, grantor="app_owner"),
    ]
    assert driver.calls[0][1] == ["app", "widget"]


def _role_row(name: str, **overrides: object) -> dict[str, object]:
    """A pg_roles catalog row with sensible non-privileged defaults."""
    row: dict[str, object] = {
        "name": name,
        "superuser": False,
        "create_role": False,
        "create_db": False,
        "can_login": True,
        "replication": False,
        "bypass_rls": False,
        "connection_limit": -1,
        "member_of": [],
    }
    row.update(overrides)
    return row


async def test_list_roles_maps_attributes_and_membership() -> None:
    driver = FakeDriver([_role_row("app_user", create_db=True, member_of=["app_readers"])])

    assert await list_roles(driver) == [
        RoleInfo(
            name="app_user",
            superuser=False,
            create_role=False,
            create_db=True,
            can_login=True,
            replication=False,
            bypass_rls=False,
            connection_limit=-1,
            member_of=["app_readers"],
        )
    ]


async def test_list_roles_excludes_predefined_roles_by_default() -> None:
    driver = FakeDriver([_role_row("app_user"), _role_row("pg_read_all_data")])

    assert [role.name for role in await list_roles(driver)] == ["app_user"]


async def test_list_roles_includes_predefined_roles_when_requested() -> None:
    driver = FakeDriver([_role_row("app_user"), _role_row("pg_read_all_data")])

    assert [role.name for role in await list_roles(driver, include_system=True)] == [
        "app_user",
        "pg_read_all_data",
    ]


async def test_list_sequences_maps_rows() -> None:
    driver = FakeDriver(
        [
            {
                "name": "widget_id_seq",
                "data_type": "bigint",
                "start_value": 1,
                "min_value": 1,
                "max_value": 9223372036854775807,
                "increment": 1,
                "cycle": False,
                "last_value": 42,
            }
        ]
    )

    assert await list_sequences(driver, "app") == [
        SequenceInfo("widget_id_seq", "bigint", 1, 1, 9223372036854775807, 1, cycle=False, last_value=42)
    ]
    assert driver.calls[0][1] == ["app"]


async def test_list_sequences_allows_a_null_last_value() -> None:
    driver = FakeDriver(
        [
            {
                "name": "fresh_seq",
                "data_type": "integer",
                "start_value": 1,
                "min_value": 1,
                "max_value": 2147483647,
                "increment": 1,
                "cycle": False,
                "last_value": None,
            }
        ]
    )

    assert (await list_sequences(driver, "app"))[0].last_value is None


async def test_list_extensions_maps_rows() -> None:
    driver = FakeDriver([{"extname": "plpgsql", "extversion": "1.0"}])

    assert await list_extensions(driver) == [ExtensionInfo("plpgsql", "1.0")]


async def test_list_available_extensions_reports_install_status() -> None:
    driver = FakeDriver(
        [
            {"name": "plpgsql", "default_version": "1.0", "installed_version": "1.0"},
            {"name": "pgvector", "default_version": "0.7.0", "installed_version": None},
        ]
    )

    assert await list_available_extensions(driver) == [
        AvailableExtension("plpgsql", "1.0", "1.0", installed=True),
        AvailableExtension("pgvector", "0.7.0", None, installed=False),
    ]


# --- MCP tool registration -------------------------------------------------

_INTROSPECTION_TOOLS = {
    "list_schemas",
    "list_tables",
    "describe_table",
    "list_indexes",
    "list_constraints",
    "list_views",
    "list_functions",
    "list_triggers",
    "list_partitions",
    "list_policies",
    "list_roles",
    "list_grants",
    "list_sequences",
    "list_extensions",
    "list_available_extensions",
}


async def test_introspection_tools_are_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}

    assert _INTROSPECTION_TOOLS <= listed


async def test_every_introspection_tool_is_callable_from_a_client() -> None:
    cases: dict[str, tuple[dict[str, str], list[dict[str, object]]]] = {
        "list_schemas": ({}, [{"schema_name": "app"}]),
        "list_tables": ({"schema": "app"}, [{"name": "w", "relkind": "r", "is_partition": False}]),
        "describe_table": (
            {"schema": "app", "table": "w"},
            [_column_row("id", "integer", nullable=False, type_name="int4")],
        ),
        "list_indexes": (
            {"schema": "app", "table": "w"},
            [{"name": "i", "method": "btree", "relkind": "i", "definition": "d"}],
        ),
        "list_constraints": (
            {"schema": "app", "table": "w"},
            [{"name": "w_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}],
        ),
        "list_views": (
            {"schema": "app"},
            [{"name": "v", "materialized": False, "definition": "SELECT 1"}],
        ),
        "list_functions": (
            {"schema": "app"},
            [{"name": "f", "kind_code": "f", "arguments": "", "returns": "void", "language": "sql"}],
        ),
        "list_triggers": (
            {"schema": "app", "table": "w"},
            [{"name": "t", "function": "fn", "definition": "CREATE TRIGGER t ..."}],
        ),
        "list_partitions": (
            {"schema": "app", "table": "event"},
            [{"strategy_code": "r", "partition_name": "event_2026", "bounds": "FOR VALUES ..."}],
        ),
        "list_policies": (
            {"schema": "app", "table": "w"},
            [
                {
                    "rls_enabled": True,
                    "name": "p",
                    "command": "SELECT",
                    "permissive": "PERMISSIVE",
                    "roles": ["public"],
                    "using_expression": "true",
                    "check_expression": None,
                }
            ],
        ),
        "list_roles": ({}, [_role_row("app_user")]),
        "list_grants": (
            {"schema": "app", "table": "w"},
            [{"grantee": "app_user", "privilege": "SELECT", "is_grantable": "NO", "grantor": "app_owner"}],
        ),
        "list_sequences": (
            {"schema": "app"},
            [
                {
                    "name": "s",
                    "data_type": "bigint",
                    "start_value": 1,
                    "min_value": 1,
                    "max_value": 100,
                    "increment": 1,
                    "cycle": False,
                    "last_value": None,
                }
            ],
        ),
        "list_extensions": ({}, [{"extname": "plpgsql", "extversion": "1.0"}]),
        "list_available_extensions": (
            {},
            [{"name": "plpgsql", "default_version": "1.0", "installed_version": "1.0"}],
        ),
    }

    for name, (args, rows) in cases.items():
        server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver(rows)))  # type: ignore[arg-type]
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(name, args)
        assert result.isError is False, name

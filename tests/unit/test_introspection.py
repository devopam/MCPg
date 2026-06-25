"""Tests for schema-introspection queries and their MCP tools."""

from typing import Any

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import (
    AvailableExtension,
    ColumnInfo,
    CompositeAttribute,
    CompositeTypeInfo,
    ConstraintInfo,
    DomainInfo,
    EnumInfo,
    ExtensionInfo,
    ForeignDataWrapperInfo,
    ForeignKeyInfo,
    ForeignServerInfo,
    ForeignTableInfo,
    FunctionInfo,
    GrantInfo,
    IndexInfo,
    PartitionInfo,
    PartitionSet,
    PolicyInfo,
    PolicySet,
    PublicationInfo,
    RoleInfo,
    SchemaInfo,
    SequenceInfo,
    SubscriptionInfo,
    TableInfo,
    TriggerInfo,
    UserMappingInfo,
    ViewInfo,
    describe_table,
    list_available_extensions,
    list_composite_types,
    list_constraints,
    list_domains,
    list_enums,
    list_extensions,
    list_foreign_data_wrappers,
    list_foreign_keys,
    list_foreign_servers,
    list_foreign_tables,
    list_functions,
    list_grants,
    list_indexes,
    list_partitions,
    list_policies,
    list_publications,
    list_roles,
    list_schemas,
    list_sequences,
    list_subscriptions,
    list_tables,
    list_triggers,
    list_user_mappings,
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


async def test_list_foreign_keys_maps_rows_aligned_by_ordinal() -> None:
    driver = FakeDriver(
        [
            {
                "name": "order_widget_fk",
                "from_table": "order",
                "to_schema": "app",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            },
            {
                "name": "order_composite_fk",
                "from_table": "order",
                "to_schema": "app",
                "to_table": "shard",
                "from_columns": ["tenant", "shard_no"],
                "to_columns": ["tenant_id", "no"],
            },
        ]
    )

    assert await list_foreign_keys(driver, "app") == [
        ForeignKeyInfo("order_widget_fk", "order", ["widget_id"], "app", "widget", ["id"]),
        ForeignKeyInfo("order_composite_fk", "order", ["tenant", "shard_no"], "app", "shard", ["tenant_id", "no"]),
    ]
    assert driver.calls[0][1] == ["app"]


async def test_list_foreign_keys_returns_an_empty_list_when_no_foreign_keys() -> None:
    assert await list_foreign_keys(FakeDriver([]), "app") == []


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

    result = await list_grants(driver, "app", "widget")
    # First call carries the (schema, table) params; FakeDriver returns
    # the same rows for both calls (info_schema and pg_get_acl). The
    # acl field falls through as None because the canned rows don't
    # carry an `acl` key.
    assert result == [
        GrantInfo("app_user", "SELECT", grantable=False, grantor="app_owner"),
        GrantInfo("app_admin", "UPDATE", grantable=True, grantor="app_owner"),
    ]
    assert driver.calls[0][1] == ["app", "widget"]


async def test_list_grants_attaches_pg_get_acl_string_on_pg19() -> None:
    """On PG 19+ ``pg_get_acl()`` returns the canonical ``\\dp`` ACL
    string; every returned :class:`GrantInfo` should carry it on the
    ``acl`` field (one ACL per relation, repeated per privilege row)."""
    routes = {
        "information_schema.table_privileges": [
            {"grantee": "app_user", "privilege": "SELECT", "is_grantable": "NO", "grantor": "app_owner"},
            {"grantee": "app_admin", "privilege": "UPDATE", "is_grantable": "YES", "grantor": "app_owner"},
        ],
        "pg_get_acl": [{"acl": "{app_owner=arwdDxt/app_owner,app_user=r/app_owner,app_admin=rw*/app_owner}"}],
    }
    driver = FakeRoutingDriver(routes)
    result = await list_grants(driver, "app", "widget")
    assert len(result) == 2
    expected_acl = "{app_owner=arwdDxt/app_owner,app_user=r/app_owner,app_admin=rw*/app_owner}"
    assert all(grant.acl == expected_acl for grant in result)
    # The information_schema rows still surface unchanged.
    assert result[0].grantee == "app_user"
    assert result[0].privilege == "SELECT"
    assert result[1].grantable is True


async def test_list_grants_acl_is_none_when_pg_get_acl_does_not_exist() -> None:
    """PG ≤ 18 doesn't ship ``pg_get_acl``; the call raises. The
    information_schema rows must still surface, with ``acl=None``
    on every row — distinguishes "PG 19 with no ACL set" (where
    ``pg_get_acl`` returns NULL → empty list) from "PG ≤ 18
    no-such-function"."""

    class _Pg18Driver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[Any] | None]] = []

        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del force_readonly
            self.calls.append((query, params))
            if "pg_get_acl" in query:
                raise RuntimeError("function pg_get_acl(regclass, oid, integer) does not exist")
            from mcpg._vendor.sql import SqlDriver

            return [
                SqlDriver.RowResult(
                    cells={
                        "grantee": "app_user",
                        "privilege": "SELECT",
                        "is_grantable": "NO",
                        "grantor": "app_owner",
                    }
                )
            ]

    driver = _Pg18Driver()
    result = await list_grants(driver, "app", "widget")  # type: ignore[arg-type]
    assert len(result) == 1
    assert result[0].acl is None
    # Both queries were attempted — the function tries pg_get_acl
    # but swallows its failure.
    assert len(driver.calls) == 2


async def test_list_grants_acl_stays_none_when_pg_get_acl_returns_no_rows() -> None:
    """``to_regclass`` returning NULL → empty rows from the ACL probe;
    the acl field stays None rather than KeyError-ing on the cells dict."""
    routes = {
        "information_schema.table_privileges": [
            {"grantee": "app_user", "privilege": "SELECT", "is_grantable": "NO", "grantor": "app_owner"},
        ],
        "pg_get_acl": [],  # empty result — relation no longer exists by ACL probe time
    }
    driver = FakeRoutingDriver(routes)
    result = await list_grants(driver, "app", "widget")
    assert len(result) == 1
    assert result[0].acl is None


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


async def test_list_enums_maps_rows() -> None:
    driver = FakeDriver(
        [
            {"name": "status", "values": ["draft", "live", "archived"]},
            {"name": "tier", "values": ["bronze", "silver", "gold"]},
        ]
    )

    assert await list_enums(driver, "app") == [
        EnumInfo("status", ["draft", "live", "archived"]),
        EnumInfo("tier", ["bronze", "silver", "gold"]),
    ]
    assert driver.calls[0][1] == ["app"]


async def test_list_enums_returns_empty_for_a_schema_with_no_enums() -> None:
    assert await list_enums(FakeDriver([]), "app") == []


async def test_list_domains_maps_rows_including_constraints() -> None:
    driver = FakeDriver(
        [
            {
                "name": "positive_int",
                "base_type": "integer",
                "nullable": False,
                "default_value": "0",
                "constraints": ["CHECK ((VALUE > 0))"],
            },
            {
                "name": "free_text",
                "base_type": "text",
                "nullable": True,
                "default_value": None,
                "constraints": [],
            },
        ]
    )

    assert await list_domains(driver, "app") == [
        DomainInfo("positive_int", "integer", False, "0", ["CHECK ((VALUE > 0))"]),
        DomainInfo("free_text", "text", True, None, []),
    ]


async def test_list_composite_types_groups_attributes_by_type() -> None:
    driver = FakeDriver(
        [
            {"type_name": "address", "attr_name": "street", "attr_type": "text", "attr_num": 1},
            {"type_name": "address", "attr_name": "city", "attr_type": "text", "attr_num": 2},
            {"type_name": "money_range", "attr_name": "low", "attr_type": "numeric", "attr_num": 1},
            {"type_name": "money_range", "attr_name": "high", "attr_type": "numeric", "attr_num": 2},
        ]
    )

    assert await list_composite_types(driver, "app") == [
        CompositeTypeInfo(
            "address",
            [CompositeAttribute("street", "text"), CompositeAttribute("city", "text")],
        ),
        CompositeTypeInfo(
            "money_range",
            [CompositeAttribute("low", "numeric"), CompositeAttribute("high", "numeric")],
        ),
    ]


async def test_list_composite_types_returns_empty_for_a_schema_with_no_types() -> None:
    assert await list_composite_types(FakeDriver([]), "app") == []


async def test_list_foreign_data_wrappers_maps_rows() -> None:
    driver = FakeDriver(
        [
            {
                "name": "postgres_fdw",
                "handler": "public.postgres_fdw_handler",
                "validator": "public.postgres_fdw_validator",
                "options": ["debug=true"],
            },
            {"name": "no_handler_fdw", "handler": None, "validator": None, "options": None},
        ]
    )

    assert await list_foreign_data_wrappers(driver) == [
        ForeignDataWrapperInfo(
            "postgres_fdw", "public.postgres_fdw_handler", "public.postgres_fdw_validator", {"debug": "true"}
        ),
        ForeignDataWrapperInfo("no_handler_fdw", None, None, {}),
    ]


async def test_options_parser_tolerates_catalog_quirks() -> None:
    # text[] catalog columns can contain NULL elements and entries without
    # a separator; duplicate keys collapse to the last value seen.
    driver = FakeDriver(
        [
            {
                "name": "quirky_fdw",
                "handler": None,
                "validator": None,
                "options": ["debug=true", "no_equal_sign", None, "debug=false", "work_mem=64MB"],
            }
        ]
    )

    assert await list_foreign_data_wrappers(driver) == [
        ForeignDataWrapperInfo("quirky_fdw", None, None, {"debug": "false", "work_mem": "64MB"}),
    ]


async def test_list_foreign_servers_maps_rows() -> None:
    driver = FakeDriver(
        [
            {
                "name": "remote_db",
                "wrapper": "postgres_fdw",
                "type": None,
                "version": None,
                "options": ["host=remote", "dbname=app"],
            }
        ]
    )

    assert await list_foreign_servers(driver) == [
        ForeignServerInfo("remote_db", "postgres_fdw", None, None, {"host": "remote", "dbname": "app"}),
    ]


async def test_list_foreign_tables_maps_rows() -> None:
    driver = FakeDriver(
        [
            {"name": "remote_widget", "server": "remote_db", "options": ["schema_name=public", "table_name=widget"]},
        ]
    )

    assert await list_foreign_tables(driver, "app") == [
        ForeignTableInfo("remote_widget", "remote_db", {"schema_name": "public", "table_name": "widget"}),
    ]
    assert driver.calls[0][1] == ["app"]


async def test_list_user_mappings_maps_rows() -> None:
    driver = FakeDriver(
        [
            {"user_name": "public", "server": "remote_db", "options": []},
            {"user_name": "app_user", "server": "remote_db", "options": ["user=app", "password=secret"]},
        ]
    )

    assert await list_user_mappings(driver) == [
        UserMappingInfo("public", "remote_db", {}),
        UserMappingInfo("app_user", "remote_db", {"user": "app", "password": "secret"}),
    ]


async def test_list_publications_maps_rows_including_tables() -> None:
    driver = FakeDriver(
        [
            {
                "name": "widget_pub",
                "owner": "app_owner",
                "all_tables": False,
                "publishes_insert": True,
                "publishes_update": True,
                "publishes_delete": False,
                "publishes_truncate": False,
                "tables": ["app.widget", "app.event"],
            },
            {
                "name": "everything_pub",
                "owner": "postgres",
                "all_tables": True,
                "publishes_insert": True,
                "publishes_update": True,
                "publishes_delete": True,
                "publishes_truncate": True,
                "tables": [],
            },
        ]
    )

    assert await list_publications(driver) == [
        PublicationInfo("widget_pub", "app_owner", False, True, True, False, False, ["app.widget", "app.event"]),
        PublicationInfo("everything_pub", "postgres", True, True, True, True, True, []),
    ]


async def test_list_subscriptions_maps_rows() -> None:
    driver = FakeDriver(
        [
            {
                "name": "widget_sub",
                "owner": "app_owner",
                "enabled": True,
                "connection": "host=upstream dbname=app",
                "publications": ["widget_pub"],
            }
        ]
    )

    assert await list_subscriptions(driver) == [
        SubscriptionInfo("widget_sub", "app_owner", True, "host=upstream dbname=app", ["widget_pub"]),
    ]


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
    "list_foreign_keys",
    "list_views",
    "list_functions",
    "list_triggers",
    "list_partitions",
    "list_policies",
    "list_roles",
    "list_grants",
    "list_sequences",
    "list_enums",
    "list_domains",
    "list_composite_types",
    "list_foreign_data_wrappers",
    "list_foreign_servers",
    "list_foreign_tables",
    "list_user_mappings",
    "list_publications",
    "list_subscriptions",
    "list_extensions",
    "list_available_extensions",
}


async def test_introspection_tools_are_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}

    assert _INTROSPECTION_TOOLS <= listed


# --- list_generated_columns (Phase 4.7) ---------------------------------


async def test_list_generated_columns_returns_typed_rows() -> None:
    from mcpg.introspection import GeneratedColumnInfo, list_generated_columns

    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {
                    "table_name": "widget",
                    "column_name": "fullname",
                    "data_type": "text",
                    "expression": "(first || ' ' || last)",
                    "kind": "s",
                },
                {
                    "table_name": "widget",
                    "column_name": "search_doc",
                    "data_type": "tsvector",
                    "expression": "to_tsvector('english', body)",
                    "kind": "s",
                },
            ]
        }
    )

    rows = await list_generated_columns(driver, "public")  # type: ignore[arg-type]

    assert len(rows) == 2
    assert isinstance(rows[0], GeneratedColumnInfo)
    assert rows[0].column == "fullname"
    assert rows[0].kind == "stored"
    assert "first" in rows[0].expression


async def test_list_generated_columns_returns_empty_when_none_present() -> None:
    from mcpg.introspection import list_generated_columns

    driver = FakeRoutingDriver({"pg_attribute": []})
    rows = await list_generated_columns(driver, "public")  # type: ignore[arg-type]
    assert rows == []

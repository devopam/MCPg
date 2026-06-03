"""Tests for the structural schema-diff and its MCP tool."""

from typing import Any

from _fakes import FakeDatabase, FakeDriver, FakeParamRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import ColumnInfo
from mcpg.schema_diff import (
    ColumnChange,
    ConstraintChange,
    ForeignKeyChange,
    IndexChange,
    _column_fields_changed,
    _diff_by_name,
    _normalize_index_def,
    _table_diff_is_empty,
    compare_schemas,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- internal helpers ------------------------------------------------------


def test_diff_by_name_returns_added_removed_changed() -> None:
    added, removed, changed = _diff_by_name(
        ["a", "b", "c"],
        ["b", "c", "d"],
        name_of=lambda x: x,
        is_changed=lambda before, after: False,
        make_change=lambda before, after: (before, after),
    )
    assert added == ["d"]
    assert removed == ["a"]
    assert changed == []


def test_diff_by_name_detects_changed_items_via_predicate() -> None:
    added, removed, changed = _diff_by_name(
        [("a", 1), ("b", 2)],
        [("a", 1), ("b", 99)],
        name_of=lambda item: item[0],
        is_changed=lambda before, after: before[1] != after[1],
        make_change=lambda before, after: (before, after),
    )
    assert added == []
    assert removed == []
    assert changed == [(("b", 2), ("b", 99))]


def test_column_fields_changed_lists_only_differing_fields() -> None:
    before = ColumnInfo(name="id", data_type="integer", nullable=False, default=None, vector_dimension=None)
    after = ColumnInfo(name="id", data_type="bigint", nullable=True, default=None, vector_dimension=None)

    assert _column_fields_changed(before, after) == ["data_type", "nullable"]
    assert _column_fields_changed(before, before) == []


# --- full compare_schemas via a parameter-routing fake driver --------------


def _column_row(
    name: str,
    data_type: str = "integer",
    *,
    nullable: bool = False,
    default: str | None = None,
    type_name: str = "int4",
    type_mod: int = -1,
) -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": data_type,
        "nullable": nullable,
        "column_default": default,
        "type_name": type_name,
        "type_mod": type_mod,
    }


_LIST_TABLES = "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind"
_DESCRIBE = "format_type(a.atttypid, a.atttypmod) AS data_type"
_LIST_INDEXES = "FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_index"
_LIST_CONSTRAINTS = "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid"
_LIST_FKS = "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid"


async def test_compare_schemas_reports_added_removed_and_changed_tables() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        # left schema has widget + dropped_table
        (_LIST_TABLES, ("left",)): [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "dropped_table", "relkind": "r", "is_partition": False},
        ],
        # right schema has widget (modified) + new_table
        (_LIST_TABLES, ("right",)): [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "new_table", "relkind": "r", "is_partition": False},
        ],
        # widget columns: left has (id integer NOT NULL, name text NULL)
        (_DESCRIBE, ("left", "widget")): [
            _column_row("id", "integer", nullable=False),
            _column_row("name", "text", nullable=True, type_name="text"),
        ],
        # right widget changes name nullability and adds a created_at column
        (_DESCRIBE, ("right", "widget")): [
            _column_row("id", "integer", nullable=False),
            _column_row("name", "text", nullable=False, type_name="text"),
            _column_row("created_at", "timestamp", nullable=False, type_name="timestamp"),
        ],
        # indexes / constraints / FKs unchanged for widget
        (_LIST_INDEXES, None): [{"name": "widget_pkey", "method": "btree", "relkind": "i", "definition": "..."}],
        (_LIST_CONSTRAINTS, None): [{"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}],
        (_LIST_FKS, None): [],
    }
    driver = FakeParamRoutingDriver(routes)

    diff = await compare_schemas(driver, "left", "right")

    assert diff.left_schema == "left"
    assert diff.right_schema == "right"
    assert [table.name for table in diff.tables_added] == ["new_table"]
    assert [table.name for table in diff.tables_removed] == ["dropped_table"]
    assert len(diff.tables_changed) == 1

    widget = diff.tables_changed[0]
    assert widget.table == "widget"
    assert [c.name for c in widget.columns_added] == ["created_at"]
    assert widget.columns_removed == []
    assert len(widget.columns_changed) == 1
    assert widget.columns_changed[0].name == "name"
    assert widget.columns_changed[0].fields_changed == ["nullable"]


async def test_compare_schemas_returns_empty_diff_for_identical_schemas() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("left",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_LIST_TABLES, ("right",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, None): [_column_row("id", "integer", nullable=False)],
        (_LIST_INDEXES, None): [],
        (_LIST_CONSTRAINTS, None): [],
        (_LIST_FKS, None): [],
    }
    driver = FakeParamRoutingDriver(routes)

    diff = await compare_schemas(driver, "left", "right")

    assert diff.tables_added == []
    assert diff.tables_removed == []
    assert diff.tables_changed == []


async def test_compare_schemas_diffs_indexes_constraints_and_foreign_keys() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("left",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_LIST_TABLES, ("right",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, None): [_column_row("id", "integer")],
        # left: btree, right: hash on same name (changed definition)
        (_LIST_INDEXES, ("left", "widget")): [
            {"name": "widget_name_idx", "method": "btree", "relkind": "i", "definition": "btree(name)"},
            {"name": "to_drop_idx", "method": "btree", "relkind": "i", "definition": "..."},
        ],
        (_LIST_INDEXES, ("right", "widget")): [
            {"name": "widget_name_idx", "method": "hash", "relkind": "i", "definition": "hash(name)"},
            {"name": "fresh_idx", "method": "btree", "relkind": "i", "definition": "..."},
        ],
        # constraint definition changed
        (_LIST_CONSTRAINTS, ("left", "widget")): [
            {"name": "widget_check", "type_code": "c", "definition": "CHECK ((id > 0))"},
        ],
        (_LIST_CONSTRAINTS, ("right", "widget")): [
            {"name": "widget_check", "type_code": "c", "definition": "CHECK ((id > 10))"},
        ],
        # FK: same name, target columns change
        (_LIST_FKS, ("left",)): [
            {
                "name": "widget_owner_fk",
                "from_table": "widget",
                "to_schema": "left",
                "to_table": "owner",
                "from_columns": ["owner_id"],
                "to_columns": ["id"],
            }
        ],
        (_LIST_FKS, ("right",)): [
            {
                "name": "widget_owner_fk",
                "from_table": "widget",
                "to_schema": "right",
                "to_table": "owner",
                "from_columns": ["owner_id"],
                "to_columns": ["uuid"],
            }
        ],
    }
    driver = FakeParamRoutingDriver(routes)

    diff = await compare_schemas(driver, "left", "right")

    assert len(diff.tables_changed) == 1
    widget = diff.tables_changed[0]

    assert [i.name for i in widget.indexes_added] == ["fresh_idx"]
    assert [i.name for i in widget.indexes_removed] == ["to_drop_idx"]
    assert isinstance(widget.indexes_changed[0], IndexChange)
    assert widget.indexes_changed[0].name == "widget_name_idx"

    assert isinstance(widget.constraints_changed[0], ConstraintChange)
    assert widget.constraints_changed[0].after.definition == "CHECK ((id > 10))"

    assert isinstance(widget.foreign_keys_changed[0], ForeignKeyChange)
    assert widget.foreign_keys_changed[0].after.to_schema == "right"
    assert widget.foreign_keys_changed[0].after.to_columns == ["uuid"]


def test_table_diff_is_empty_recognises_an_unchanged_table() -> None:
    from mcpg.schema_diff import TableDiff

    empty = TableDiff(
        table="x",
        columns_added=[],
        columns_removed=[],
        columns_changed=[],
        indexes_added=[],
        indexes_removed=[],
        indexes_changed=[],
        constraints_added=[],
        constraints_removed=[],
        constraints_changed=[],
        foreign_keys_added=[],
        foreign_keys_removed=[],
        foreign_keys_changed=[],
    )
    populated = TableDiff(
        table="x",
        columns_added=[ColumnInfo("c", "int", False, None, None)],
        columns_removed=[],
        columns_changed=[],
        indexes_added=[],
        indexes_removed=[],
        indexes_changed=[],
        constraints_added=[],
        constraints_removed=[],
        constraints_changed=[],
        foreign_keys_added=[],
        foreign_keys_removed=[],
        foreign_keys_changed=[],
    )

    assert _table_diff_is_empty(empty) is True
    assert _table_diff_is_empty(populated) is False


# --- MCP tool wiring -------------------------------------------------------


async def test_compare_schemas_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "compare_schemas" in listed

        result = await client.call_tool("compare_schemas", {"left_schema": "a", "right_schema": "b"})

    assert result.isError is False
    # Empty FakeDriver returns no tables on either side — diff is a zeroed
    # structure with the schema names preserved.
    payload = result.structuredContent
    assert payload is not None
    assert payload["left_schema"] == "a"
    assert payload["right_schema"] == "b"
    assert payload["tables_added"] == []
    assert payload["tables_removed"] == []
    assert payload["tables_changed"] == []


# --- field exports referenced by other code -------------------------------


def test_column_change_dataclass_shape() -> None:
    # Constructing a ColumnChange should not throw for the canonical shape;
    # this catches accidental field renames the wiring depends on.
    change = ColumnChange(
        name="id",
        before=ColumnInfo("id", "integer", False, None, None),
        after=ColumnInfo("id", "bigint", False, None, None),
        fields_changed=["data_type"],
    )
    assert change.fields_changed == ["data_type"]


def test_normalize_index_def_strips_qualifiers_except_in_literals() -> None:
    # Standard index definitions
    assert _normalize_index_def("CREATE INDEX idx ON public.tbl (col)", "public") == "CREATE INDEX idx ON tbl (col)"
    assert _normalize_index_def('CREATE INDEX idx ON "public".tbl (col)', "public") == "CREATE INDEX idx ON tbl (col)"

    # Ignored schema name in other identifiers
    assert (
        _normalize_index_def("CREATE INDEX idx ON mypublic.tbl (col)", "public")
        == "CREATE INDEX idx ON mypublic.tbl (col)"
    )

    # Crucially, schema names inside string literals must NOT be modified
    assert (
        _normalize_index_def("CREATE INDEX idx ON public.tbl (col) WHERE col = 'public.val'", "public")
        == "CREATE INDEX idx ON tbl (col) WHERE col = 'public.val'"
    )
    assert (
        _normalize_index_def("CREATE INDEX idx ON public.tbl (col) WHERE col = 'don\\'t touch public.val'", "public")
        == "CREATE INDEX idx ON tbl (col) WHERE col = 'don\\'t touch public.val'"
    )

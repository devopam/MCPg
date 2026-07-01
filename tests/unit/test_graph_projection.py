"""Tests for the relational → AGE graph-projection generator."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg._vendor.sql import SqlDriver
from mcpg.graph_projection import (
    HARD_ROW_CAP,
    EdgeType,
    GraphProjectionError,
    NodeLabel,
    generate_graph_projection,
)


class _AgeAwareDriver(FakeRoutingDriver):
    """Routing driver that raises on the AGE probe when ``age`` is False."""

    def __init__(self, routes: dict[str, list[dict[str, Any]]], *, age: bool = True) -> None:
        super().__init__(routes)
        self._age = age

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[SqlDriver.RowResult]:
        if "ag_catalog.ag_graph" in query:
            self.calls.append((query, params, force_readonly))
            if not self._age:
                raise RuntimeError("relation ag_catalog.ag_graph does not exist")
            return [SqlDriver.RowResult(cells={"?column?": 1})]
        return await super().execute_query(query, params, force_readonly)


# Column-describe rows for two tables: authors(id) and books(id, author_id, title).
_AUTHOR_COLS = [
    {
        "column_name": "id",
        "data_type": "integer",
        "nullable": False,
        "column_default": None,
        "type_name": "int4",
        "type_mod": -1,
    },
    {
        "column_name": "name",
        "data_type": "text",
        "nullable": True,
        "column_default": None,
        "type_name": "text",
        "type_mod": -1,
    },
]
_BOOK_COLS = [
    {
        "column_name": "id",
        "data_type": "integer",
        "nullable": False,
        "column_default": None,
        "type_name": "int4",
        "type_mod": -1,
    },
    {
        "column_name": "author_id",
        "data_type": "integer",
        "nullable": True,
        "column_default": None,
        "type_name": "int4",
        "type_mod": -1,
    },
    {
        "column_name": "title",
        "data_type": "text",
        "nullable": True,
        "column_default": None,
        "type_name": "text",
        "type_mod": -1,
    },
]


class _SchemaDriver(_AgeAwareDriver):
    """Routes describe_table + PK per table param, plus base tables and FKs."""

    def __init__(
        self,
        *,
        age: bool = True,
        author_pk: bool = True,
        book_pk: bool = True,
        rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__({}, age=age)
        self._author_pk = author_pk
        self._book_pk = book_pk
        self._table_rows = rows or {}

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[SqlDriver.RowResult]:
        if "ag_catalog.ag_graph" in query:
            return await super().execute_query(query, params, force_readonly)
        self.calls.append((query, params, force_readonly))
        p = params or []
        table = p[1] if len(p) > 1 else None
        if "WHERE c.relkind = 'r'" in query:
            return _rows([{"name": "authors"}, {"name": "books"}])
        if "i.indisprimary" in query:
            if table == "authors" and self._author_pk:
                return _rows([{"column_name": "id"}])
            if table == "books" and self._book_pk:
                return _rows([{"column_name": "id"}])
            return _rows([])
        if "FROM pg_attribute a " in query and "attnum > 0" in query:
            cols = _AUTHOR_COLS if table == "authors" else _BOOK_COLS
            return _rows(cols)
        if "c.contype = 'f'" in query:
            return _rows(
                [
                    {
                        "name": "books_author_id_fkey",
                        "from_table": "books",
                        "to_schema": "public",
                        "to_table": "authors",
                        "from_columns": ["author_id"],
                        "to_columns": ["id"],
                    }
                ]
            )
        if query.startswith("SELECT * FROM"):
            # sample rows in row mode
            if '"authors"' in query:
                return _rows(self._table_rows.get("authors", []))
            if '"books"' in query:
                return _rows(self._table_rows.get("books", []))
        return _rows([])


def _rows(rows: list[dict[str, Any]]) -> list[SqlDriver.RowResult]:
    return [SqlDriver.RowResult(cells=dict(r)) for r in rows]


async def test_schema_level_plan_no_row_reads() -> None:
    driver = _SchemaDriver()
    result = await generate_graph_projection(driver, "public", row_limit=0)  # type: ignore[arg-type]

    assert result.node_labels == [
        NodeLabel(label="authors", source_table="authors", key_columns=["id"], property_columns=["id", "name"]),
        NodeLabel(
            label="books",
            source_table="books",
            key_columns=["id"],
            property_columns=["id", "author_id", "title"],
        ),
    ]
    assert result.edge_types == [
        EdgeType(
            edge_type="books_author_id_fkey",
            from_label="books",
            to_label="authors",
            from_key=["author_id"],
            to_key=["id"],
            fk_name="books_author_id_fkey",
        )
    ]
    # Template statements present, placeholders used.
    joined = "\n".join(result.cypher_statements)
    assert "CREATE (:authors {id: $id, name: $name})" in joined
    assert "MERGE (a)-[:books_author_id_fkey]->(b)" in joined
    assert "$from_author_id" in joined and "$to_id" in joined
    # No table row reads were issued.
    assert not any(q.startswith("SELECT * FROM") for q, _, _ in driver.calls)


async def test_row_mode_binds_values_escapes_and_omits_nulls() -> None:
    driver = _SchemaDriver(
        rows={
            "authors": [
                {"id": 1, "name": "O'Brien"},
                {"id": 2, "name": None},
                {"id": 3, "name": "Ann"},  # 3rd row — must be capped away at row_limit=2
            ],
            "books": [
                {"id": 10, "author_id": 1, "title": "A"},
                {"id": 11, "author_id": None, "title": "B"},
            ],
        }
    )
    result = await generate_graph_projection(driver, "public", row_limit=2)  # type: ignore[arg-type]

    joined = "\n".join(result.cypher_statements)
    # Escaped single quote.
    assert "CREATE (:authors {id: 1, name: 'O''Brien'})" in joined
    # NULL property omitted (row 2 has no name).
    assert "CREATE (:authors {id: 2})" in joined
    # Capped at 2 rows — the 3rd author is absent.
    assert "'Ann'" not in joined
    # Edge merge binds concrete values.
    assert "MATCH (a:books {author_id: 1}), (b:authors {id: 1}) MERGE (a)-[:books_author_id_fkey]->(b)" in joined
    # Book with NULL FK produces no edge.
    assert joined.count("MERGE (a)-[:books_author_id_fkey]->(b)") == 1


async def test_keyless_table_warns_and_skips_its_edges() -> None:
    driver = _SchemaDriver(book_pk=False)
    result = await generate_graph_projection(driver, "public", row_limit=0)  # type: ignore[arg-type]

    # books still gets a node label (empty key_columns).
    labels = {n.label: n for n in result.node_labels}
    assert labels["books"].key_columns == []
    # The FK books->authors touches keyless books, so no edge.
    assert result.edge_types == []
    assert any("no primary key" in w and "books" in w for w in result.warnings)


async def test_table_subset_excludes_out_of_scope_fk() -> None:
    driver = _SchemaDriver()
    result = await generate_graph_projection(driver, "public", tables=["authors"], row_limit=0)  # type: ignore[arg-type]

    assert [n.label for n in result.node_labels] == ["authors"]
    # books is out of scope, so the FK is dropped.
    assert result.edge_types == []


async def test_age_absent_still_generates_statements() -> None:
    driver = _SchemaDriver(age=False)
    result = await generate_graph_projection(driver, "public", row_limit=0)  # type: ignore[arg-type]

    assert result.available is False
    assert result.cypher_statements  # still emitted
    assert any("AGE does not appear to be installed" in w for w in result.warnings)


async def test_available_true_when_age_present() -> None:
    driver = _SchemaDriver(age=True)
    result = await generate_graph_projection(driver, "public", row_limit=0)  # type: ignore[arg-type]
    assert result.available is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema": "bad-schema"},
        {"schema": "public", "graph_name": "bad graph"},
    ],
)
async def test_identifier_rejection(kwargs: dict[str, Any]) -> None:
    driver = _SchemaDriver()
    with pytest.raises(GraphProjectionError, match="invalid"):
        await generate_graph_projection(driver, **kwargs)  # type: ignore[arg-type]


async def test_bad_table_name_rejected() -> None:
    driver = _SchemaDriver()
    with pytest.raises(GraphProjectionError, match="invalid"):
        await generate_graph_projection(driver, "public", tables=["bad;drop"])  # type: ignore[arg-type]


async def test_out_of_set_table_rejected() -> None:
    driver = _SchemaDriver()
    with pytest.raises(GraphProjectionError, match="not a base table"):
        await generate_graph_projection(driver, "public", tables=["ghost"])  # type: ignore[arg-type]


async def test_row_limit_bounds() -> None:
    driver = _SchemaDriver()
    with pytest.raises(GraphProjectionError, match="row_limit must be"):
        await generate_graph_projection(driver, "public", row_limit=-1)  # type: ignore[arg-type]
    # An over-cap row_limit is clamped (not rejected) — matching the "capped"
    # contract — and the clamp is surfaced in warnings.
    clamped = await generate_graph_projection(driver, "public", row_limit=99999)  # type: ignore[arg-type]
    assert clamped.row_limit == HARD_ROW_CAP
    assert any("clamped to the hard cap" in w for w in clamped.warnings)


async def test_ordering_and_materialise_warnings_present() -> None:
    driver = _SchemaDriver()
    result = await generate_graph_projection(driver, "public", row_limit=0)  # type: ignore[arg-type]
    assert any("materializes" in w for w in result.warnings)
    assert any("node CREATE statements before the edge MERGE" in w for w in result.warnings)

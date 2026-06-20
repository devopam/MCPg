"""Tests for the SQL/PGQ (PG 19 property graph queries) coverage module."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.pgq import (
    CreatePropertyGraphResult,
    DropPropertyGraphResult,
    PgqError,
    PgqRunResult,
    PgqStatus,
    PropertyGraphInfo,
    create_property_graph,
    describe_property_graph,
    drop_property_graph,
    get_pgq_status,
    list_property_graphs,
    run_pgq,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the server-version probe to a specific version."""
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_pgq_status --------------------------------------------------------


async def test_status_available_on_pg19() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    status = await get_pgq_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, PgqStatus)
    assert status.available is True
    assert status.server_version_num == 190001
    assert "available" in status.detail.lower()


async def test_status_unavailable_on_pg18_with_diagnostic() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_pgq_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.server_version_num == 180003
    # Diagnostic must point the agent at the AGE fallback.
    assert "run_cypher" in status.detail


async def test_status_handles_missing_version_row() -> None:
    driver = FakeRoutingDriver({})
    status = await get_pgq_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.server_version_num == 0


# --- list_property_graphs --------------------------------------------------


async def test_list_returns_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    assert await list_property_graphs(driver) == []  # type: ignore[arg-type]


async def test_list_returns_graphs_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM information_schema.sql_property_graphs"] = [
        {"schema": "public", "name": "org_chart"},
        {"schema": "ops", "name": "fk_cascade"},
    ]
    routes["FROM information_schema.sql_property_graph_tables"] = [
        {"qname": "public.employees", "kind": "VERTEX"},
        {"qname": "public.reports_to", "kind": "EDGE"},
    ]
    driver = FakeRoutingDriver(routes)
    graphs = await list_property_graphs(driver)  # type: ignore[arg-type]
    assert len(graphs) == 2
    assert graphs[0] == PropertyGraphInfo(
        schema="public",
        name="org_chart",
        vertex_tables=["public.employees"],
        edge_tables=["public.reports_to"],
    )


# --- describe_property_graph -----------------------------------------------


async def test_describe_returns_graph_with_membership() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM information_schema.sql_property_graphs"] = [
        {"schema": "public", "name": "org_chart"},
    ]
    routes["FROM information_schema.sql_property_graph_tables"] = [
        {"qname": "public.employees", "kind": "VERTEX"},
        {"qname": "public.reports_to", "kind": "EDGE"},
    ]
    driver = FakeRoutingDriver(routes)
    info = await describe_property_graph(driver, "public", "org_chart")  # type: ignore[arg-type]
    assert info.schema == "public"
    assert info.name == "org_chart"
    assert info.vertex_tables == ["public.employees"]
    assert info.edge_tables == ["public.reports_to"]


async def test_describe_raises_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    with pytest.raises(PgqError, match="SQL/PGQ requires PostgreSQL 19"):
        await describe_property_graph(driver, "public", "org_chart")  # type: ignore[arg-type]


async def test_describe_raises_for_missing_graph() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM information_schema.sql_property_graphs"] = []
    driver = FakeRoutingDriver(routes)
    with pytest.raises(PgqError, match="no property graph"):
        await describe_property_graph(driver, "public", "ghost")  # type: ignore[arg-type]


async def test_describe_rejects_bad_identifier() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="not a valid"):
        await describe_property_graph(driver, "public", "bad; DROP")  # type: ignore[arg-type]


# --- run_pgq ---------------------------------------------------------------


async def test_run_pgq_executes_graph_table_select() -> None:
    routes = _version_route(190001, "19beta1")
    routes["SELECT * FROM GRAPH_TABLE"] = [
        {"employee": "Ada", "manager": "Babbage"},
        {"employee": "Babbage", "manager": "Lovelace"},
    ]
    driver = FakeRoutingDriver(routes)
    result = await run_pgq(  # type: ignore[arg-type]
        driver,
        "SELECT * FROM GRAPH_TABLE (org_chart MATCH (e)-[r]->(m) COLUMNS (e.name AS employee, m.name AS manager))",
    )
    assert isinstance(result, PgqRunResult)
    assert result.columns == ["employee", "manager"]
    assert result.row_count == 2
    assert result.truncated is False


async def test_run_pgq_truncates_to_max_rows() -> None:
    routes = _version_route(190001, "19beta1")
    routes["GRAPH_TABLE"] = [{"v": i} for i in range(10)]
    driver = FakeRoutingDriver(routes)
    result = await run_pgq(  # type: ignore[arg-type]
        driver,
        "SELECT v FROM GRAPH_TABLE (org_chart MATCH (n) COLUMNS (n.v AS v))",
        max_rows=3,
    )
    assert result.row_count == 3
    assert result.truncated is True


async def test_run_pgq_rejects_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    with pytest.raises(PgqError, match="SQL/PGQ requires PostgreSQL 19"):
        await run_pgq(driver, "SELECT * FROM GRAPH_TABLE (g MATCH (n) COLUMNS (n.x AS x))")  # type: ignore[arg-type]


async def test_run_pgq_rejects_non_select() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="single SELECT"):
        await run_pgq(driver, "UPDATE x SET y=1")  # type: ignore[arg-type]


async def test_run_pgq_rejects_query_without_graph_table() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="single SELECT"):
        await run_pgq(driver, "SELECT 1")  # type: ignore[arg-type]


async def test_run_pgq_rejects_chained_statements() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="single SELECT"):
        await run_pgq(  # type: ignore[arg-type]
            driver,
            "SELECT * FROM GRAPH_TABLE (g MATCH (n) COLUMNS (n.x AS x)); DROP TABLE t",
        )


async def test_run_pgq_accepts_trailing_semicolon() -> None:
    routes = _version_route(190001, "19beta1")
    routes["GRAPH_TABLE"] = []
    driver = FakeRoutingDriver(routes)
    result = await run_pgq(  # type: ignore[arg-type]
        driver,
        "SELECT * FROM GRAPH_TABLE (g MATCH (n) COLUMNS (n.x AS x));",
    )
    assert result.row_count == 0


async def test_run_pgq_accepts_with_cte_prefix() -> None:
    routes = _version_route(190001, "19beta1")
    routes["GRAPH_TABLE"] = [{"x": 1}]
    driver = FakeRoutingDriver(routes)
    result = await run_pgq(  # type: ignore[arg-type]
        driver,
        "WITH ct AS (SELECT 1) SELECT * FROM GRAPH_TABLE (g MATCH (n) COLUMNS (n.x AS x))",
    )
    assert result.row_count == 1


async def test_run_pgq_rejects_zero_max_rows() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="max_rows must be positive"):
        await run_pgq(  # type: ignore[arg-type]
            driver,
            "SELECT * FROM GRAPH_TABLE (g MATCH (n) COLUMNS (n.x AS x))",
            max_rows=0,
        )


# --- create_property_graph -------------------------------------------------


async def test_create_emits_property_graph_ddl() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    result = await create_property_graph(  # type: ignore[arg-type]
        driver,
        schema="public",
        name="org_chart",
        definition_body=("VERTEX TABLES (employees KEY (id) LABEL Employee PROPERTIES (id, name))"),
    )
    assert result == CreatePropertyGraphResult(schema="public", name="org_chart", created=True)
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'CREATE PROPERTY GRAPH "public"."org_chart"' in queries
    assert "VERTEX TABLES" in queries


async def test_create_rejects_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    with pytest.raises(PgqError, match="SQL/PGQ requires PostgreSQL 19"):
        await create_property_graph(  # type: ignore[arg-type]
            driver, schema="public", name="g", definition_body="VERTEX TABLES (t KEY (id))"
        )


async def test_create_rejects_body_without_vertex_tables_prefix() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="VERTEX TABLES"):
        await create_property_graph(  # type: ignore[arg-type]
            driver,
            schema="public",
            name="g",
            definition_body="DROP TABLE employees",
        )


async def test_create_rejects_body_with_semicolon() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="';'"):
        await create_property_graph(  # type: ignore[arg-type]
            driver,
            schema="public",
            name="g",
            definition_body="VERTEX TABLES (t KEY (id)); DROP TABLE t",
        )


async def test_create_rejects_bad_identifier() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    with pytest.raises(PgqError, match="not a valid"):
        await create_property_graph(  # type: ignore[arg-type]
            driver,
            schema="public",
            name="bad; DROP",
            definition_body="VERTEX TABLES (t KEY (id))",
        )


# --- drop_property_graph ---------------------------------------------------


async def test_drop_emits_drop_property_graph_ddl_with_if_exists() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    result = await drop_property_graph(driver, schema="public", name="org_chart")  # type: ignore[arg-type]
    assert result == DropPropertyGraphResult(schema="public", name="org_chart", dropped=True)
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'DROP PROPERTY GRAPH IF EXISTS "public"."org_chart"' in queries


async def test_drop_emits_without_if_exists_when_disabled() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    await drop_property_graph(  # type: ignore[arg-type]
        driver, schema="public", name="org_chart", if_exists=False
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'DROP PROPERTY GRAPH "public"."org_chart"' in queries
    assert "IF EXISTS" not in queries


async def test_drop_rejects_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    with pytest.raises(PgqError, match="SQL/PGQ requires PostgreSQL 19"):
        await drop_property_graph(driver, schema="public", name="g")  # type: ignore[arg-type]


# --- Dataclass shape -------------------------------------------------------


def test_dataclass_shapes() -> None:
    status = PgqStatus(available=True, server_version_num=190001, server_version="19beta1", detail="ok")
    assert status.available is True
    run = PgqRunResult(columns=["x"], rows=[{"x": 1}], row_count=1, truncated=False)
    assert run.row_count == 1

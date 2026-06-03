"""Tests for pgvector tuning advisors."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.vector_tuning import (
    RecallReport,
    TuningRecommendation,
    VectorTuningError,
    _recommend_hnsw,
    _recommend_ivfflat,
    _rewrite_opclass_to_halfvec,
    migrate_vector_to_halfvec,
    tune_vector_index,
    vector_recall_at_k,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- heuristic helpers -----------------------------------------------------


def test_recommend_ivfflat_uses_rows_over_1000_for_small_tables() -> None:
    params, rationale = _recommend_ivfflat(50_000)
    assert params == {"lists": 100}  # 50_000 // 1000 = 50, clamped to 100
    assert "row_count=50,000" in rationale


def test_recommend_ivfflat_uses_rows_over_1000_at_moderate_size() -> None:
    params, _ = _recommend_ivfflat(500_000)
    assert params == {"lists": 500}


def test_recommend_ivfflat_uses_sqrt_above_one_million() -> None:
    params, rationale = _recommend_ivfflat(4_000_000)
    assert params == {"lists": 2000}  # sqrt(4M) = 2000
    assert "sqrt" in rationale


def test_recommend_ivfflat_boundary_at_exactly_one_million() -> None:
    # row_count == 1M sits on the "<=" branch — rows/1000 = 1000 lists.
    params, _ = _recommend_ivfflat(1_000_000)
    assert params == {"lists": 1000}
    # The first row above the boundary flips to the sqrt branch.
    params_above, rationale_above = _recommend_ivfflat(1_000_001)
    assert params_above["lists"] == int(1_000_001**0.5)
    assert "sqrt" in rationale_above


def test_recommend_hnsw_uses_baseline_under_one_million() -> None:
    params, rationale = _recommend_hnsw(500_000)
    assert params == {"m": 16, "ef_construction": 128}  # 500k > 100k → ef bumped
    assert "baseline" in rationale


def test_recommend_hnsw_denser_graph_for_large_tables() -> None:
    params, _ = _recommend_hnsw(5_000_000)
    assert params == {"m": 24, "ef_construction": 128}


def test_recommend_hnsw_default_ef_for_tiny_tables() -> None:
    params, _ = _recommend_hnsw(10_000)
    assert params == {"m": 16, "ef_construction": 64}


def test_recommend_hnsw_boundary_at_exactly_100k_rows() -> None:
    # row_count == 100k sits on the "<=" branch — ef_construction stays at 64.
    params, _ = _recommend_hnsw(100_000)
    assert params == {"m": 16, "ef_construction": 64}
    # First row above flips the construction bump.
    params_above, _ = _recommend_hnsw(100_001)
    assert params_above == {"m": 16, "ef_construction": 128}


def test_recommend_hnsw_boundary_at_exactly_one_million_rows() -> None:
    # row_count == 1M sits on the "<=" branch — m stays at 16.
    params, _ = _recommend_hnsw(1_000_000)
    assert params == {"m": 16, "ef_construction": 128}
    # First row above flips m to 24.
    params_above, _ = _recommend_hnsw(1_000_001)
    assert params_above == {"m": 24, "ef_construction": 128}


# --- tune_vector_index -----------------------------------------------------


def _vector_column_row(name: str, dimension: int, nullable: bool = True) -> dict[str, object]:
    return {
        "column_name": name,
        "data_type": f"vector({dimension})",
        "nullable": nullable,
        "column_default": None,
        "type_name": "vector",
        "type_mod": dimension,
    }


async def test_tune_vector_index_emits_hnsw_with_ready_to_run_sql() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [{"estimate": 250_000}],
            "format_type(a.atttypid": [_vector_column_row("embedding", 384, nullable=False)],
        }
    )

    rec = await tune_vector_index(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert isinstance(rec, TuningRecommendation)
    assert rec.index_type == "hnsw"
    assert rec.parameters == {"m": 16, "ef_construction": 128}
    assert rec.row_count == 250_000
    assert rec.dimension == 384
    assert "vector_l2_ops" in rec.create_index_sql
    assert "m = 16" in rec.create_index_sql and "ef_construction = 128" in rec.create_index_sql


async def test_tune_vector_index_supports_ivfflat_and_alternative_metric() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [{"estimate": 8_000_000}],
            "format_type(a.atttypid": [_vector_column_row("v", 1536)],
        }
    )

    rec = await tune_vector_index(  # type: ignore[arg-type]
        driver, "app", "items", "v", index_type="ivfflat", metric="cosine"
    )

    assert rec.index_type == "ivfflat"
    assert rec.parameters == {"lists": int(8_000_000**0.5)}  # 2828
    assert "vector_cosine_ops" in rec.create_index_sql


async def test_tune_vector_index_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(VectorTuningError, match="not installed"):
        await tune_vector_index(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_tune_vector_index_rejects_unknown_index_type() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="unsupported index_type"):
        await tune_vector_index(driver, "app", "docs", "embedding", index_type="bogus")  # type: ignore[arg-type]


async def test_tune_vector_index_rejects_unknown_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="unknown metric"):
        await tune_vector_index(driver, "app", "docs", "embedding", metric="bogus")  # type: ignore[arg-type]


async def test_tune_vector_index_raises_when_table_missing() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [],
        }
    )
    with pytest.raises(VectorTuningError, match="not found"):
        await tune_vector_index(driver, "app", "missing", "embedding")  # type: ignore[arg-type]


async def test_tune_vector_index_raises_when_column_not_vector() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [{"estimate": 1000}],
            "format_type(a.atttypid": [
                {
                    "column_name": "embedding",
                    "data_type": "text",
                    "nullable": True,
                    "column_default": None,
                    "type_name": "text",
                    "type_mod": -1,
                }
            ],
        }
    )
    with pytest.raises(VectorTuningError, match="not a pgvector"):
        await tune_vector_index(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_tune_vector_index_raises_when_column_missing() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [{"estimate": 1000}],
            "format_type(a.atttypid": [],
        }
    )
    with pytest.raises(VectorTuningError, match="not found"):
        await tune_vector_index(driver, "app", "docs", "missing")  # type: ignore[arg-type]


# --- vector_recall_at_k ----------------------------------------------------


async def test_vector_recall_at_k_perfect_recall_when_ann_matches_truth() -> None:
    # Both the ANN (operator) and brute-force (function) paths return the
    # same id sets for every sample row → recall = 1.0.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE": [
                {"id": 1, "vec": "[0.1,0.2,0.3]"},
                {"id": 2, "vec": "[0.4,0.5,0.6]"},
            ],
            "<->": [{"id": 10}, {"id": 11}, {"id": 12}],
            "l2_distance": [{"id": 10}, {"id": 11}, {"id": 12}],
        }
    )

    report = await vector_recall_at_k(driver, "app", "docs", "embedding", "id", k=3, sample_size=2)  # type: ignore[arg-type]

    assert isinstance(report, RecallReport)
    assert report.sample_size == 2
    assert report.k == 3
    assert report.mean_recall == 1.0


async def test_vector_recall_at_k_reports_partial_recall_for_imperfect_index() -> None:
    # ANN returns {10, 11, 99}; truth is {10, 11, 12} → 2/3 overlap.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE": [{"id": 1, "vec": "[1,2,3]"}],
            "<->": [{"id": 10}, {"id": 11}, {"id": 99}],
            "l2_distance": [{"id": 10}, {"id": 11}, {"id": 12}],
        }
    )

    report = await vector_recall_at_k(driver, "app", "docs", "embedding", "id", k=3, sample_size=1)  # type: ignore[arg-type]

    assert report.mean_recall == pytest.approx(2 / 3)


async def test_vector_recall_at_k_returns_zero_recall_when_no_samples() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "WHERE": []})

    report = await vector_recall_at_k(driver, "app", "docs", "embedding", "id")  # type: ignore[arg-type]

    assert report.sample_size == 0
    assert report.mean_recall == 0.0


async def test_vector_recall_at_k_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    with pytest.raises(VectorTuningError, match="not installed"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", "id")  # type: ignore[arg-type]


async def test_vector_recall_at_k_rejects_unknown_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="unknown metric"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", "id", metric="bogus")  # type: ignore[arg-type]


async def test_vector_recall_at_k_rejects_non_positive_arguments() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="must be positive"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", "id", k=0)  # type: ignore[arg-type]
    with pytest.raises(VectorTuningError, match="must be positive"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", "id", sample_size=0)  # type: ignore[arg-type]


async def test_vector_recall_at_k_caps_sample_size_to_prevent_dos() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="sample_size cannot exceed"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", "id", sample_size=101)  # type: ignore[arg-type]


async def test_tune_vector_index_rejects_invalid_identifier_characters() -> None:
    # Identifier injection guard — anything not matching the [A-Za-z_][...]
    # allowlist is rejected before the SQL is built.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "GREATEST(c.reltuples": [{"estimate": 1000}],
            "format_type(a.atttypid": [_vector_column_row("embedding", 3)],
        }
    )
    with pytest.raises(VectorTuningError, match="invalid schema name"):
        await tune_vector_index(driver, 'app"; DROP TABLE x; --', "docs", "embedding")  # type: ignore[arg-type]


async def test_vector_recall_at_k_rejects_invalid_identifier_characters() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "WHERE": []})
    with pytest.raises(VectorTuningError, match="invalid id_column name"):
        await vector_recall_at_k(driver, "app", "docs", "embedding", 'id"; DROP TABLE x; --')  # type: ignore[arg-type]


# --- tool wiring -----------------------------------------------------------


async def test_vector_tuning_tools_are_registered_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {"tune_vector_index", "vector_recall_at_k", "migrate_vector_to_halfvec"} <= listed


# --- migrate_vector_to_halfvec --------------------------------------------


def _column_type_row(typename: str, dim: int) -> dict[str, object]:
    """Catalog row matching the migrate tool's `_column_pg_type` query."""
    # pg_attribute.atttypmod for vector(N)/halfvec(N) is the declared N.
    return {"type_name": typename, "type_mod": dim}


def _halfvec_routes(
    *,
    column_type: tuple[str, int] | None = ("vector", 384),
    row_count: int = 1000,
    indexes: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Standard FakeRoutingDriver routes for the migrate planner."""
    column_rows: list[dict[str, object]] = (
        [_column_type_row(column_type[0], column_type[1])] if column_type is not None else []
    )
    return {
        "pg_extension": [{"present": 1}],
        # Substring unique to the `_column_pg_type` query — joins on
        # pg_attribute + pg_type with the WHERE on attname.
        "AND a.attname = %s AND a.attnum > 0": column_rows,
        "GREATEST(c.reltuples": [{"estimate": row_count}],
        # Substring unique to the `_indexes_on_column` query.
        "pg_get_indexdef(ix.indexrelid)": indexes or [],
    }


def test_rewrite_opclass_to_halfvec_replaces_known_opclasses() -> None:
    src = "CREATE INDEX foo ON app.docs USING hnsw (embedding vector_cosine_ops)"
    out, supported = _rewrite_opclass_to_halfvec(src)
    assert supported is True
    assert "halfvec_cosine_ops" in out
    assert "vector_cosine_ops" not in out


def test_rewrite_opclass_to_halfvec_reports_unsupported_when_no_opclass_matches() -> None:
    # A plain B-tree index over a vector column would have no
    # `vector_*_ops` opclass marker in its def — caller should skip
    # the rewrite rather than mangle the SQL.
    src = "CREATE INDEX bar ON app.docs USING btree (id)"
    out, supported = _rewrite_opclass_to_halfvec(src)
    assert supported is False
    assert out == src


async def test_migrate_vector_to_halfvec_emits_plan_for_vector_with_hnsw_index() -> None:
    indexes = [
        {
            "index_name": "docs_emb_hnsw",
            "index_method": "hnsw",
            "index_def": "CREATE INDEX docs_emb_hnsw ON app.docs USING hnsw (embedding vector_cosine_ops) WITH (m=16)",
        }
    ]
    driver = FakeRoutingDriver(_halfvec_routes(row_count=10_000, indexes=indexes))

    plan = await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert plan.available is True
    assert plan.already_halfvec is False
    assert plan.column_type == "vector"
    assert plan.dimension == 384
    assert plan.row_count == 10_000
    # 4 bytes/elem (vector) → 2 bytes/elem (halfvec) over 384 dims = 768 bytes saved per row.
    assert plan.estimated_bytes_per_row_before == 384 * 4
    assert plan.estimated_bytes_per_row_after == 384 * 2
    assert plan.estimated_total_bytes_saved == 10_000 * 384 * 2
    # Plan: DROP INDEX, ALTER COLUMN, CREATE INDEX
    assert len(plan.migration_sql) == 3
    assert plan.migration_sql[0].startswith("DROP INDEX IF EXISTS")
    assert "docs_emb_hnsw" in plan.migration_sql[0]
    assert "ALTER TABLE" in plan.migration_sql[1]
    assert f"halfvec({plan.dimension})" in plan.migration_sql[1]
    assert f'USING "embedding"::halfvec({plan.dimension})' in plan.migration_sql[1]
    assert "halfvec_cosine_ops" in plan.migration_sql[2]
    # Rollback: DROP halfvec index, ALTER back to vector, recreate original
    assert plan.rollback_sql[1].startswith("ALTER TABLE")
    assert f"vector({plan.dimension})" in plan.rollback_sql[1]
    assert "vector_cosine_ops" in plan.rollback_sql[2]


async def test_migrate_vector_to_halfvec_returns_noop_when_already_halfvec() -> None:
    driver = FakeRoutingDriver(_halfvec_routes(column_type=("halfvec", 768)))

    plan = await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert plan.available is True
    assert plan.already_halfvec is True
    assert plan.column_type == "halfvec"
    assert plan.dimension == 768
    assert plan.migration_sql == []
    assert plan.rollback_sql == []
    assert "already halfvec" in plan.notes


async def test_migrate_vector_to_halfvec_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    with pytest.raises(VectorTuningError, match="vector extension is not installed"):
        await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_migrate_vector_to_halfvec_raises_when_column_missing() -> None:
    driver = FakeRoutingDriver(_halfvec_routes(column_type=None))
    with pytest.raises(VectorTuningError, match="not a pgvector column"):
        await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_migrate_vector_to_halfvec_raises_when_column_is_other_type() -> None:
    # int4 column with no atttypmod dimension — caught by the
    # "no declared dimension" guard.
    driver = FakeRoutingDriver(_halfvec_routes(column_type=("int4", 0)))
    with pytest.raises(VectorTuningError, match="not a pgvector column"):
        await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_migrate_vector_to_halfvec_handles_multiple_indexes() -> None:
    indexes = [
        {
            "index_name": "docs_emb_hnsw",
            "index_method": "hnsw",
            "index_def": "CREATE INDEX docs_emb_hnsw ON app.docs USING hnsw (embedding vector_l2_ops)",
        },
        {
            "index_name": "docs_emb_ivfflat",
            "index_method": "ivfflat",
            "index_def": (
                "CREATE INDEX docs_emb_ivfflat ON app.docs USING ivfflat (embedding vector_ip_ops) WITH (lists=100)"
            ),
        },
    ]
    driver = FakeRoutingDriver(_halfvec_routes(indexes=indexes))

    plan = await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert len(plan.indexes) == 2
    # 2 drops + 1 alter + 2 recreates = 5 statements
    assert len(plan.migration_sql) == 5
    drops = [s for s in plan.migration_sql if s.startswith("DROP")]
    creates = [s for s in plan.migration_sql if s.startswith("CREATE INDEX")]
    assert len(drops) == 2
    assert len(creates) == 2
    assert any("halfvec_l2_ops" in c for c in creates)
    assert any("halfvec_ip_ops" in c for c in creates)


async def test_migrate_vector_to_halfvec_refuses_unconvertible_ann_index() -> None:
    # An HNSW / IVFFlat index built on a custom opclass we don't know
    # the halfvec equivalent of — refuse the migration rather than
    # silently leave the column un-indexed.
    indexes = [
        {
            "index_name": "docs_custom",
            "index_method": "hnsw",
            "index_def": "CREATE INDEX docs_custom ON app.docs USING hnsw (embedding mycustom_ops)",
        }
    ]
    driver = FakeRoutingDriver(_halfvec_routes(indexes=indexes))

    with pytest.raises(VectorTuningError, match="no halfvec sibling"):
        await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]


async def test_migrate_vector_to_halfvec_with_no_indexes_emits_only_alter() -> None:
    driver = FakeRoutingDriver(_halfvec_routes(indexes=[]))

    plan = await migrate_vector_to_halfvec(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert plan.indexes == []
    assert len(plan.migration_sql) == 1
    assert plan.migration_sql[0].startswith("ALTER TABLE")
    assert "halfvec" in plan.migration_sql[0]
    assert plan.rollback_sql[0].startswith("ALTER TABLE")


async def test_migrate_vector_to_halfvec_rejects_unsafe_identifiers() -> None:
    driver = FakeRoutingDriver(_halfvec_routes())
    with pytest.raises(VectorTuningError, match="invalid schema name"):
        await migrate_vector_to_halfvec(driver, 'app"; DROP TABLE x; --', "docs", "embedding")  # type: ignore[arg-type]


async def test_migrate_vector_to_halfvec_tool_is_callable_from_a_client() -> None:
    indexes = [
        {
            "index_name": "docs_emb_hnsw",
            "index_method": "hnsw",
            "index_def": "CREATE INDEX docs_emb_hnsw ON app.docs USING hnsw (embedding vector_cosine_ops)",
        }
    ]
    database = FakeDatabase(FakeRoutingDriver(_halfvec_routes(indexes=indexes)))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "migrate_vector_to_halfvec" in listed
        result = await client.call_tool(
            "migrate_vector_to_halfvec",
            {"schema": "app", "table": "docs", "column": "embedding"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["already_halfvec"] is False
    assert "halfvec_cosine_ops" in " ".join(result.structuredContent["migration_sql"])

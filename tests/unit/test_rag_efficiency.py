"""Tests for the RAG efficiency suite — Phase A.

The test surface is in two layers:

* Pure functions (stat helpers, knob mapping, rule evaluation) get
  direct unit tests on known inputs. The rule-table evaluator takes
  a plain ``dict`` of metrics so each rule can be exercised without
  any driver setup.
* The end-to-end ``analyze_vector_search_efficiency`` is tested with
  scripted ``FakeRoutingDriver`` fixtures that return deterministic
  ids / distances per query type.
"""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.rag_efficiency import (
    RerankLiftPoint,
    VectorEfficiencyError,
    _evaluate_rules,
    _kendall_tau,
    _knob_value_for_multiplier,
    _percentile,
    _rank,
    _recall_at_k,
    _spearman,
    analyze_vector_search_efficiency,
)
from mcpg.server import create_server

_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------------


def test_rank_handles_ties() -> None:
    # 10, 20, 20, 30 → ranks 1, 2.5, 2.5, 4
    assert _rank([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]


def test_rank_preserves_input_order() -> None:
    # Output's i-th entry is rank of input's i-th entry, not sorted.
    assert _rank([30.0, 10.0, 20.0]) == [3.0, 1.0, 2.0]


def test_spearman_monotonic_series_gives_one() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [10.0, 20.0, 30.0, 40.0]
    assert _spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_reversed_series_gives_minus_one() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [40.0, 30.0, 20.0, 10.0]
    assert _spearman(xs, ys) == pytest.approx(-1.0)


def test_spearman_returns_zero_for_constant_input() -> None:
    # All ties on one axis → no signal to correlate against.
    assert _spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0


def test_kendall_tau_monotonic_series_gives_one() -> None:
    assert _kendall_tau([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


def test_kendall_tau_reversed_gives_minus_one() -> None:
    assert _kendall_tau([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_kendall_tau_zero_for_random_pattern() -> None:
    # One concordant + one discordant + one mix → tau near 0.
    tau = _kendall_tau([1.0, 2.0, 3.0, 4.0], [2.0, 1.0, 4.0, 3.0])
    assert abs(tau) < 0.5


def test_recall_at_k_perfect_overlap_is_one() -> None:
    assert _recall_at_k([1, 2, 3], [1, 2, 3]) == 1.0


def test_recall_at_k_partial_overlap() -> None:
    # 2 of 3 truth-set rows recovered.
    assert _recall_at_k([1, 2, 99], [1, 2, 3]) == pytest.approx(2 / 3)


def test_recall_at_k_zero_when_truth_empty() -> None:
    assert _recall_at_k([1, 2, 3], []) == 0.0


def test_percentile_linear_interpolation() -> None:
    assert _percentile([10.0, 20.0, 30.0, 40.0], 0.5) == pytest.approx(25.0)
    assert _percentile([10.0, 20.0, 30.0, 40.0], 0.0) == 10.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 1.0) == 40.0


def test_percentile_empty_returns_zero() -> None:
    assert _percentile([], 0.5) == 0.0


# --- knob axis -------------------------------------------------------------


def test_knob_hnsw_floors_at_k_plus_10() -> None:
    # ef_search must be >= k; multiplier=1 with k=10 floors to k+10=20.
    assert _knob_value_for_multiplier("hnsw", k=10, multiplier=1) == 20
    assert _knob_value_for_multiplier("hnsw", k=10, multiplier=4) == 40


def test_knob_ivfflat_uses_multiplier_directly() -> None:
    assert _knob_value_for_multiplier("ivfflat", k=10, multiplier=1) == 1
    assert _knob_value_for_multiplier("ivfflat", k=10, multiplier=10) == 10


def test_knob_turboquant_multiplies_k() -> None:
    assert _knob_value_for_multiplier("turboquant", k=10, multiplier=4) == 40


def test_knob_rejects_invalid_multiplier() -> None:
    with pytest.raises(VectorEfficiencyError, match="candidate_multiplier"):
        _knob_value_for_multiplier("hnsw", k=10, multiplier=0)


def test_knob_rejects_unknown_backend() -> None:
    with pytest.raises(VectorEfficiencyError, match="unsupported backend"):
        _knob_value_for_multiplier("brin", k=10, multiplier=2)


# --- rule evaluation -------------------------------------------------------


def _partial(  # type: ignore[no-untyped-def]
    *,
    backend="hnsw",
    knob_name="ef_search",
    baseline=0.95,
    curve=None,
    spearman=0.9,
    pages_pruned=None,
    k=10,
):
    """Helper to build the ``report_partial`` dict the rules consume."""
    if curve is None:
        curve = [RerankLiftPoint(1, knob_name, 20, baseline, 1.0, 1.5)]
    return {
        "k": k,
        "backend": backend,
        "knob_name": knob_name,
        "recall_at_k_baseline": baseline,
        "rerank_lift_curve": curve,
        "score_rank_correlation_spearman": spearman,
        "pages_pruned_ratio_p50": pages_pruned,
    }


def test_rule_baseline_recall_low_fires_when_below_threshold() -> None:
    findings = _evaluate_rules(_partial(baseline=0.5))
    [finding] = findings
    assert finding.code == "baseline_recall_low"
    assert finding.severity == "CRITICAL"


def test_rule_baseline_recall_low_silent_at_threshold() -> None:
    findings = _evaluate_rules(_partial(baseline=0.8))
    assert all(f.code != "baseline_recall_low" for f in findings)


def test_rule_rerank_lift_flat_fires_when_curve_is_flat() -> None:
    curve = [
        RerankLiftPoint(1, "ef_search", 20, 0.92, 1.0, 1.5),
        RerankLiftPoint(2, "ef_search", 40, 0.93, 1.0, 1.5),
        RerankLiftPoint(4, "ef_search", 80, 0.93, 1.0, 1.5),
        RerankLiftPoint(10, "ef_search", 200, 0.93, 1.0, 1.5),  # only 0.01 above baseline
    ]
    findings = _evaluate_rules(_partial(baseline=0.92, curve=curve))
    by_code = {f.code: f for f in findings}
    assert "rerank_lift_flat" in by_code


def test_rule_rerank_lift_flat_silent_when_baseline_already_bad() -> None:
    # baseline below 0.80 → baseline_recall_low fires instead; the
    # flat-lift surface would be confusingly suggesting "knob already
    # saturated".
    curve = [
        RerankLiftPoint(1, "ef_search", 20, 0.50, 1.0, 1.5),
        RerankLiftPoint(10, "ef_search", 200, 0.51, 1.0, 1.5),
    ]
    findings = _evaluate_rules(_partial(baseline=0.50, curve=curve))
    codes = {f.code for f in findings}
    assert "baseline_recall_low" in codes
    assert "rerank_lift_flat" not in codes


def test_rule_rerank_lift_steep_fires_when_knob_too_tight() -> None:
    curve = [
        RerankLiftPoint(1, "ef_search", 20, 0.60, 1.0, 1.5),
        RerankLiftPoint(2, "ef_search", 40, 0.85, 1.0, 1.5),
        RerankLiftPoint(4, "ef_search", 80, 0.96, 1.0, 1.5),  # the 4x point that triggers
        RerankLiftPoint(10, "ef_search", 200, 0.97, 1.0, 1.5),
    ]
    findings = _evaluate_rules(_partial(baseline=0.60, curve=curve))
    by_code = {f.code: f for f in findings}
    assert "rerank_lift_steep" in by_code
    # baseline_recall_low ALSO fires (0.60 < 0.80), and that's fine —
    # they're independent signals.


def test_rule_ranking_degraded_fires_when_recall_high_but_spearman_low() -> None:
    findings = _evaluate_rules(_partial(baseline=0.95, spearman=0.3))
    by_code = {f.code: f for f in findings}
    assert "ranking_degraded" in by_code


def test_rule_ranking_degraded_silent_when_recall_below_threshold() -> None:
    findings = _evaluate_rules(_partial(baseline=0.85, spearman=0.3))
    by_code = {f.code: f for f in findings}
    assert "ranking_degraded" not in by_code


def test_rule_pruning_ineffective_turboquant_only() -> None:
    # Turboquant + low pruning ratio → fires.
    findings = _evaluate_rules(_partial(backend="turboquant", pages_pruned=0.05))
    by_code = {f.code: f for f in findings}
    assert "pruning_ineffective" in by_code
    # HNSW with the same metric value → does not fire.
    findings = _evaluate_rules(_partial(backend="hnsw", pages_pruned=0.05))
    assert all(f.code != "pruning_ineffective" for f in findings)


def test_rule_pruning_ineffective_silent_when_metric_missing() -> None:
    findings = _evaluate_rules(_partial(backend="turboquant", pages_pruned=None))
    assert all(f.code != "pruning_ineffective" for f in findings)


# --- analyze_vector_search_efficiency: input validation -------------------


async def test_analyze_rejects_unknown_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorEfficiencyError, match="unsupported metric"):
        await analyze_vector_search_efficiency(
            driver,
            "public",
            "embeddings",
            "embedding",
            "id",
            metric="inner_product",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["schema", "table", "column", "id_column"])
async def test_analyze_rejects_unsafe_identifiers(field: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    kwargs: dict[str, Any] = {
        "schema": "public",
        "table": "embeddings",
        "column": "embedding",
        "id_column": "id",
    }
    kwargs[field] = "bad; DROP"
    with pytest.raises(VectorEfficiencyError, match="invalid"):
        await analyze_vector_search_efficiency(driver, **kwargs)  # type: ignore[arg-type]


async def test_analyze_rejects_oversize_sample() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorEfficiencyError, match="sample_size"):
        await analyze_vector_search_efficiency(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "embedding",
            "id",
            sample_size=101,
        )


async def test_analyze_rejects_empty_multipliers() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorEfficiencyError, match="candidate_multipliers cannot be empty"):
        await analyze_vector_search_efficiency(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "embedding",
            "id",
            candidate_multipliers=(),
        )


async def test_analyze_raises_when_vector_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    with pytest.raises(VectorEfficiencyError, match="vector extension"):
        await analyze_vector_search_efficiency(
            driver,
            "public",
            "embeddings",
            "embedding",
            "id",  # type: ignore[arg-type]
        )


async def test_analyze_raises_when_no_ann_index_found() -> None:
    # pgvector present, but no HNSW / IVFFlat / turboquant on the column.
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorEfficiencyError, match="no HNSW"):
        await analyze_vector_search_efficiency(
            driver,
            "public",
            "embeddings",
            "embedding",
            "id",  # type: ignore[arg-type]
        )


async def test_analyze_raises_when_named_index_is_on_wrong_column() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            # Detect-by-name lookup returns the index, but on a
            # different column than what the caller asked about.
            "i.relname = %s AND am.amname IN": [
                {
                    "schema": "public",
                    "index": "my_idx",
                    "table": "embeddings",
                    "backend": "hnsw",
                    "column": "other_column",
                }
            ],
        }
    )
    with pytest.raises(VectorEfficiencyError, match="not on column"):
        await analyze_vector_search_efficiency(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "embedding",
            "id",
            index_name="my_idx",
        )


async def test_analyze_raises_when_named_index_is_on_wrong_table() -> None:
    # An index named `my_idx` in schema `public` could exist on any
    # number of tables. Without a table check, an index on a
    # different table that happens to share a column name would slip
    # through and the brute-force baseline below would run against
    # the wrong relation.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "i.relname = %s AND am.amname IN": [
                {
                    "schema": "public",
                    "index": "my_idx",
                    "table": "other_table",  # NOT "embeddings"
                    "backend": "hnsw",
                    "column": "embedding",  # same column name as the caller's request
                }
            ],
        }
    )
    with pytest.raises(VectorEfficiencyError, match="not 'embeddings'"):
        await analyze_vector_search_efficiency(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "embedding",
            "id",
            index_name="my_idx",
        )


# --- end-to-end happy path (HNSW arm) --------------------------------------


async def test_analyze_returns_empty_report_when_table_is_empty() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "t.relname = %s AND a.attname": [{"index": "idx_hnsw", "backend": "hnsw"}],
            "ORDER BY ": [],  # empty sample set
        }
    )
    report = await analyze_vector_search_efficiency(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "embedding",
        "id",
        sample_size=5,
        candidate_multipliers=(1, 2),
    )
    assert report.sample_size == 0
    assert report.recall_at_k_baseline == 0.0
    assert report.rerank_lift_curve == []
    assert report.findings == []


# --- MCP layer wiring ------------------------------------------------------


async def test_analyze_vector_search_efficiency_tool_registered() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "analyze_vector_search_efficiency" in listed

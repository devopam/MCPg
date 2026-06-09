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


# --- Phase E: adaptive thresholds -----------------------------------------


def test_adaptive_threshold_overrides_default_baseline_recall() -> None:
    # Default threshold 0.80 — baseline 0.75 fires. With adaptive
    # threshold 0.60 supplied, the same baseline is now "above the
    # corpus" and does NOT fire.
    partial = _partial(baseline=0.75)
    partial["thresholds"] = {"baseline_recall_low": 0.60}
    findings = _evaluate_rules(partial)
    assert all(f.code != "baseline_recall_low" for f in findings)


def test_adaptive_threshold_can_tighten_baseline_recall() -> None:
    # Tighter adaptive threshold makes more baselines fire — recall
    # 0.85 passes the default 0.80 but fails an adaptive 0.90.
    partial = _partial(baseline=0.85)
    partial["thresholds"] = {"baseline_recall_low": 0.90}
    findings = _evaluate_rules(partial)
    assert any(f.code == "baseline_recall_low" for f in findings)


def test_adaptive_threshold_partial_override_falls_back_to_defaults() -> None:
    # Only baseline_recall_low overridden; the other rules continue
    # to consult their hardcoded defaults. ranking_degraded should
    # still fire on recall=0.95 + spearman=0.3 vs default 0.50.
    partial = _partial(baseline=0.95, spearman=0.3)
    partial["thresholds"] = {"baseline_recall_low": 0.50}
    findings = _evaluate_rules(partial)
    by_code = {f.code: f for f in findings}
    assert "baseline_recall_low" not in by_code  # overridden away
    assert "ranking_degraded" in by_code  # still fires on default


def test_adaptive_threshold_pruning_ineffective_uses_corpus_value() -> None:
    # Default 0.10 — pages_pruned=0.15 silent. With corpus-derived
    # 0.20, the same value is now flagged.
    partial = _partial(backend="turboquant", pages_pruned=0.15)
    partial["thresholds"] = {"pruning_ineffective": 0.20}
    findings = _evaluate_rules(partial)
    assert any(f.code == "pruning_ineffective" for f in findings)


def test_adaptive_threshold_non_numeric_value_falls_back_to_default() -> None:
    # Defensive: a bad threshold (str, None, bool) silently falls
    # back to default rather than crashing the rule loop.
    partial = _partial(baseline=0.5)
    partial["thresholds"] = {"baseline_recall_low": "not-a-number"}
    findings = _evaluate_rules(partial)
    # Default 0.80 still applies; recall=0.5 below → fires.
    assert any(f.code == "baseline_recall_low" for f in findings)


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


# --- audit_vector_indexes (Phase B) ----------------------------------------


async def test_audit_vector_indexes_returns_none_when_pgvector_absent() -> None:
    from mcpg.rag_efficiency import audit_vector_indexes

    driver = FakeRoutingDriver({"pg_extension": []})
    assert await audit_vector_indexes(driver) is None  # type: ignore[arg-type]


async def test_audit_vector_indexes_returns_none_when_no_ann_indexes() -> None:
    from mcpg.rag_efficiency import audit_vector_indexes

    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname IN ('hnsw', 'ivfflat', 'turboquant')": [],
        }
    )
    assert await audit_vector_indexes(driver) is None  # type: ignore[arg-type]


async def test_audit_vector_indexes_skips_table_without_single_col_pk() -> None:
    # ANN index exists but the table has no single-column PK → audit
    # walker skips the index, surfaces it as a GOOD baseline metric
    # so the operator sees it, score stays at 100.
    from mcpg.rag_efficiency import audit_vector_indexes

    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname IN ('hnsw', 'ivfflat', 'turboquant')": [
                {
                    "schema": "public",
                    "table": "embeddings",
                    "column": "embedding",
                    "index": "embeddings_hnsw_idx",
                    "backend": "hnsw",
                }
            ],
            "AND i.indisprimary = true": [],  # no single-col PK
        }
    )

    category = await audit_vector_indexes(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.score == 100
    assert category.status == "GOOD"
    [skip_metric] = category.metrics
    assert "skipped" in skip_metric.value
    assert "single-column primary key" in skip_metric.evidence


async def test_audit_vector_indexes_emits_good_baseline_when_no_findings() -> None:
    # ANN index audit completes with no findings → GOOD baseline.
    from mcpg.rag_efficiency import audit_vector_indexes

    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname IN ('hnsw', 'ivfflat', 'turboquant')": [
                {
                    "schema": "public",
                    "table": "embeddings",
                    "column": "embedding",
                    "index": "embeddings_hnsw_idx",
                    "backend": "hnsw",
                }
            ],
            "AND i.indisprimary = true": [{"pk_column": "id"}],
            # Detect-by-name confirms the index is on the right table+col.
            "AND i.relname = %s AND am.amname IN": [
                {
                    "schema": "public",
                    "index": "embeddings_hnsw_idx",
                    "table": "embeddings",
                    "backend": "hnsw",
                    "column": "embedding",
                }
            ],
            # Empty sample → empty report → no findings.
            "ORDER BY ": [],
        }
    )

    category = await audit_vector_indexes(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.score == 100
    assert category.status == "GOOD"
    # The "no findings" metric is emitted per index that audited cleanly.
    assert any("no_findings" in m.name for m in category.metrics)


# --- MCP layer wiring ------------------------------------------------------


async def test_analyze_vector_search_efficiency_tool_registered() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "analyze_vector_search_efficiency" in listed


# --- RAG-D: stat helpers ---------------------------------------------------


def test_jaccard_identical_sets() -> None:
    from mcpg.rag_efficiency import _jaccard

    assert _jaccard([1, 2, 3], [1, 2, 3]) == 1.0


def test_jaccard_disjoint_sets() -> None:
    from mcpg.rag_efficiency import _jaccard

    assert _jaccard([1, 2, 3], [4, 5, 6]) == 0.0


def test_jaccard_partial_overlap() -> None:
    from mcpg.rag_efficiency import _jaccard

    # |{1,2}| / |{1,2,3,4}| = 2/4
    assert _jaccard([1, 2, 3], [1, 2, 4]) == pytest.approx(2 / 4)


def test_jaccard_empty_inputs_returns_zero() -> None:
    from mcpg.rag_efficiency import _jaccard

    assert _jaccard([], []) == 0.0


def test_ndcg_perfect_order_is_one() -> None:
    from mcpg.rag_efficiency import _ndcg_at_k

    # Already in descending grade order → NDCG = 1.0.
    assert _ndcg_at_k([3.0, 2.0, 1.0, 0.0], 4) == pytest.approx(1.0)


def test_ndcg_reversed_order_is_below_perfect() -> None:
    from mcpg.rag_efficiency import _ndcg_at_k

    # Worst possible non-zero ordering.
    val = _ndcg_at_k([0.0, 1.0, 2.0, 3.0], 4)
    assert 0.0 < val < 1.0


def test_ndcg_all_zero_grades_returns_zero() -> None:
    from mcpg.rag_efficiency import _ndcg_at_k

    assert _ndcg_at_k([0.0, 0.0, 0.0], 3) == 0.0


def test_ndcg_respects_k() -> None:
    from mcpg.rag_efficiency import _ndcg_at_k

    # k=1 only looks at the first item; if it's the highest grade,
    # NDCG@1 = 1.0 regardless of what follows.
    assert _ndcg_at_k([3.0, 0.0, 0.0], 1) == pytest.approx(1.0)


def test_histogram_uniform_distribution() -> None:
    from mcpg.rag_efficiency import _histogram

    # Values 0..9 in 5 buckets → 2 per bucket.
    counts = _histogram([float(v) for v in range(10)], n_buckets=5)
    assert counts == [2, 2, 2, 2, 2]


def test_histogram_all_identical_lands_in_last_bucket() -> None:
    from mcpg.rag_efficiency import _histogram

    counts = _histogram([0.5, 0.5, 0.5], n_buckets=4)
    assert counts == [0, 0, 0, 3]


def test_histogram_empty_returns_zeroed_buckets() -> None:
    from mcpg.rag_efficiency import _histogram

    assert _histogram([], n_buckets=5) == [0, 0, 0, 0, 0]


# --- RAG-D: analytics functions -------------------------------------------


_NO_EVENTS_TABLE = "pg_class c JOIN pg_namespace n"  # the existence probe substring


async def test_analyze_reranker_lift_returns_no_events_when_table_missing() -> None:
    from mcpg.rag_efficiency import analyze_reranker_lift

    driver = FakeRoutingDriver({})  # no events table
    report = await analyze_reranker_lift(driver)  # type: ignore[arg-type]
    assert report.query_count == 0
    assert report.interpretation == "no events table"


async def test_analyze_reranker_lift_validates_window() -> None:
    from mcpg.rag_efficiency import VectorEfficiencyError, analyze_reranker_lift

    driver = FakeRoutingDriver({})
    with pytest.raises(VectorEfficiencyError, match="window days"):
        await analyze_reranker_lift(driver, days=0)  # type: ignore[arg-type]


async def test_analyze_reranker_lift_fires_reranker_idle_on_high_kendall() -> None:
    # Build per-query events where bi_rank == cross_rank (perfect
    # agreement → Kendall = 1.0) across 5 queries to clear the idle
    # threshold.
    from mcpg.rag_efficiency import analyze_reranker_lift

    rows = []
    for q in range(5):
        for r in range(1, 6):
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "bi_encoder_rank": r,
                    "cross_encoder_rank": r,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_reranker_lift(driver)  # type: ignore[arg-type]
    assert report.query_count == 5
    assert report.mean_kendall == pytest.approx(1.0)
    assert any(f.code == "reranker_idle" for f in report.findings)
    assert report.interpretation == "reranker mostly confirms"


async def test_analyze_reranker_lift_silent_when_reranker_actively_reorders() -> None:
    # bi_rank and cross_rank reversed → Kendall tau = -1 per query.
    from mcpg.rag_efficiency import analyze_reranker_lift

    rows = []
    for q in range(3):
        for r in range(1, 6):
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "bi_encoder_rank": r,
                    "cross_encoder_rank": 6 - r,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_reranker_lift(driver)  # type: ignore[arg-type]
    assert report.mean_kendall == pytest.approx(-1.0)
    assert report.findings == []
    assert report.interpretation == "reranker actively reorders"


async def test_analyze_topk_stability_fires_topk_stable_when_top_k_is_identical() -> None:
    from mcpg.rag_efficiency import analyze_topk_stability

    # Same candidates in top-3 by both orderings across 5 queries.
    rows = []
    for q in range(5):
        for c in (10, 20, 30):
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "candidate_id": c,
                    "bi_encoder_rank": c // 10,
                    "cross_encoder_rank": c // 10,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_topk_stability(driver, k=3)  # type: ignore[arg-type]
    assert report.query_count == 5
    assert report.mean_jaccard == pytest.approx(1.0)
    assert any(f.code == "topk_stable" for f in report.findings)


async def test_analyze_rerank_score_distribution_fires_on_top_decile_clustering() -> None:
    from mcpg.rag_efficiency import analyze_rerank_score_distribution

    # 100 scores, 80 of them at 0.98 (top decile of [0, 1]) → >50%
    # clustering, fires score_clustering.
    rows = [{"score": 0.1} for _ in range(10)]
    rows += [{"score": 0.5} for _ in range(10)]
    rows += [{"score": 0.98} for _ in range(80)]
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_rerank_score_distribution(driver, n_buckets=10)  # type: ignore[arg-type]
    assert report.event_count == 100
    assert report.top_decile_share >= 0.50
    assert any(f.code == "score_clustering" for f in report.findings)


async def test_analyze_rerank_score_distribution_carries_retrieval_index_filter() -> None:
    # Regression for gemini's PR #81 finding: the analytic must
    # accept ``retrieval_index`` and surface it on the report so a
    # caller scoping by index gets per-pipeline metrics.
    from mcpg.rag_efficiency import analyze_rerank_score_distribution

    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": [{"score": 0.5}],
        }
    )
    report = await analyze_rerank_score_distribution(
        driver,  # type: ignore[arg-type]
        retrieval_index="public.embeddings_hnsw_idx",
    )
    assert report.retrieval_index == "public.embeddings_hnsw_idx"
    # And the SQL must have carried the filter as a bound param.
    rel_call = next(c for c in driver.calls if "FROM mcpg_rag.rerank_events" in c[0])
    _query, params, _ro = rel_call
    assert "public.embeddings_hnsw_idx" in (params or [])


async def test_analyze_rerank_ndcg_fires_hurts_when_cross_order_is_worse() -> None:
    from mcpg.rag_efficiency import analyze_rerank_ndcg

    # bi-rank order: high-relevance items first (ideal). Cross-rank
    # order: reverse → NDCG drops dramatically.
    rows = []
    for q in range(5):
        for r in range(1, 6):
            grade = 5 - (r - 1)  # 5, 4, 3, 2, 1 by bi_rank
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "bi_encoder_rank": r,
                    "cross_encoder_rank": 6 - r,
                    "ground_truth_relevance": grade,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_rerank_ndcg(driver, k=5)  # type: ignore[arg-type]
    assert report.labeled_query_count == 5
    assert report.delta < 0
    assert any(f.code == "rerank_hurts_ndcg" for f in report.findings)


async def test_analyze_rerank_ndcg_fires_lifts_when_cross_improves_ordering() -> None:
    from mcpg.rag_efficiency import analyze_rerank_ndcg

    # bi-rank order: ascending grades (worst NDCG). Cross-rank
    # order: descending grades (best NDCG). Cross_rank is the
    # reverse of bi_rank, so grades follow accordingly.
    rows = []
    for q in range(5):
        for r in range(1, 6):
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "bi_encoder_rank": r,
                    "cross_encoder_rank": 6 - r,
                    # Grade aligned with bi_rank → by bi_rank: 1,2,3,4,5
                    # (worst order); by cross_rank: 5,4,3,2,1 (best).
                    "ground_truth_relevance": r,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    report = await analyze_rerank_ndcg(driver, k=5)  # type: ignore[arg-type]
    assert report.delta > 0
    assert any(f.code == "rerank_lifts_ndcg" for f in report.findings)


async def test_recommend_rerank_strategy_summarises_no_findings_state() -> None:
    from mcpg.rag_efficiency import recommend_rerank_strategy

    # Reranker actively reorders (varied ranks per query) but no
    # labeled rows → none of the rules fire → "healthy" summary.
    rows = []
    for q in range(3):
        for r in range(1, 4):
            rows.append(
                {
                    "query_hash": bytes([q]),
                    "candidate_id": r,
                    "bi_encoder_rank": r,
                    "cross_encoder_rank": 4 - r,
                    "score": 0.5 + 0.1 * r,
                    "ground_truth_relevance": None,
                }
            )
    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": rows,
        }
    )
    rec = await recommend_rerank_strategy(driver)  # type: ignore[arg-type]
    assert "healthy" in rec.summary or "Mixed" in rec.summary


# --- audit_rag_pipeline (Phase B-style category) --------------------------


async def test_audit_rag_pipeline_returns_none_when_table_missing() -> None:
    from mcpg.rag_efficiency import audit_rag_pipeline

    driver = FakeRoutingDriver({})  # no events table
    assert await audit_rag_pipeline(driver) is None  # type: ignore[arg-type]


async def test_audit_rag_pipeline_emits_good_baseline_when_no_findings() -> None:
    from mcpg.rag_efficiency import audit_rag_pipeline

    driver = FakeRoutingDriver(
        {
            _NO_EVENTS_TABLE: [{"present": 1}],
            "FROM mcpg_rag.rerank_events": [],  # window empty
        }
    )
    category = await audit_rag_pipeline(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.score == 100
    assert category.status == "GOOD"


# --- MCP layer wiring ------------------------------------------------------


async def test_rag_analytics_tools_registered_in_read_only_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {
        "analyze_reranker_lift",
        "analyze_topk_stability",
        "analyze_rerank_score_distribution",
        "analyze_rerank_ndcg",
        "recommend_rerank_strategy",
    } <= listed

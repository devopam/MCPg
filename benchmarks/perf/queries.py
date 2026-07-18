"""The benchmark query set — two independent axes.

* **compute_class** — ultralight / light / heavy (how much work the DB does).
* **result_size** — 1 / ~100 / large (how many rows come back, which drives
  MCPg's serialization cost independently of compute).

Heavy queries are drawn from the standard **TPC-H** analytical set (fixed
parameter literals so every run is identical). Ultralight/light run against the
same TPC-H schema so a single loaded dataset serves the whole suite.

All SQL here is read-only SELECT — it flows through ``run_select`` /
``SafeSqlDriver`` unchanged, exactly as an agent's query would.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ComputeClass = Literal["ultralight", "light", "heavy"]
ResultSize = Literal["1", "~100", "large"]


@dataclass(frozen=True)
class BenchQuery:
    """One benchmark query with its taxonomy labels."""

    id: str
    sql: str
    compute_class: ComputeClass
    result_size: ResultSize
    # run_select truncates at max_rows (default 1000); the large-result cases
    # raise it so the serialization cost is actually exercised end to end.
    max_rows: int = 1000


# --- ultralight: sub-millisecond, index/point access ----------------------

_ULTRALIGHT = [
    BenchQuery("select_1", "SELECT 1 AS one", "ultralight", "1"),
    BenchQuery(
        "pk_lookup_orders",
        "SELECT * FROM orders WHERE o_orderkey = 1",
        "ultralight",
        "1",
    ),
    BenchQuery(
        "pk_lookup_customer",
        "SELECT * FROM customer WHERE c_custkey = 1",
        "ultralight",
        "1",
    ),
]

# --- light: single-digit ms, indexed range + small aggregate --------------

_LIGHT = [
    BenchQuery(
        "orders_status_counts",
        "SELECT o_orderstatus, count(*) AS n FROM orders "
        "WHERE o_orderdate BETWEEN DATE '1995-01-01' AND DATE '1995-03-31' "
        "GROUP BY o_orderstatus",
        "light",
        "~100",
    ),
]

# --- heavy: 100 ms-seconds, standard TPC-H analytical queries -------------
# Fixed parameter substitutions (TPC-H validation-style constants) so the run
# is deterministic. A representative cross-section for the first cut — the full
# Q1-Q22 set is added incrementally.

_TPCH_Q1 = """
SELECT l_returnflag, l_linestatus,
       sum(l_quantity) AS sum_qty,
       sum(l_extendedprice) AS sum_base_price,
       sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
       sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
       avg(l_quantity) AS avg_qty, avg(l_extendedprice) AS avg_price,
       avg(l_discount) AS avg_disc, count(*) AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90 day'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
""".strip()

_TPCH_Q6 = """
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= DATE '1994-01-01'
  AND l_shipdate < DATE '1994-01-01' + INTERVAL '1 year'
  AND l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01
  AND l_quantity < 24
""".strip()

_TPCH_Q3 = """
SELECT l_orderkey,
       sum(l_extendedprice * (1 - l_discount)) AS revenue,
       o_orderdate, o_shippriority
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING'
  AND c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND o_orderdate < DATE '1995-03-15'
  AND l_shipdate > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10
""".strip()

_TPCH_Q5 = """
SELECT n_name, sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM customer, orders, lineitem, supplier, nation, region
WHERE c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND l_suppkey = s_suppkey
  AND c_nationkey = s_nationkey
  AND s_nationkey = n_nationkey
  AND n_regionkey = r_regionkey
  AND r_name = 'ASIA'
  AND o_orderdate >= DATE '1994-01-01'
  AND o_orderdate < DATE '1994-01-01' + INTERVAL '1 year'
GROUP BY n_name
ORDER BY revenue DESC
""".strip()

_HEAVY = [
    BenchQuery("tpch_q1", _TPCH_Q1, "heavy", "~100"),
    BenchQuery("tpch_q6", _TPCH_Q6, "heavy", "1"),
    BenchQuery("tpch_q3", _TPCH_Q3, "heavy", "~100"),
    BenchQuery("tpch_q5", _TPCH_Q5, "heavy", "~100"),
]

# --- result-size axis: decouple serialization cost from compute weight ----
# Same cheap-ish scan, three return sizes, so t_serialize is isolated.

_RESULT_SIZE = [
    BenchQuery(
        "rows_1",
        "SELECT count(*) AS n FROM lineitem WHERE l_shipdate < DATE '1994-01-01'",
        "light",
        "1",
    ),
    BenchQuery(
        "rows_100",
        "SELECT l_orderkey, l_quantity, l_extendedprice FROM lineitem WHERE l_shipdate < DATE '1994-01-01' LIMIT 100",
        "light",
        "~100",
        max_rows=100,
    ),
    BenchQuery(
        "rows_100k",
        "SELECT l_orderkey, l_quantity, l_extendedprice FROM lineitem "
        "WHERE l_shipdate < DATE '1994-01-01' LIMIT 100000",
        "light",
        "large",
        max_rows=100_000,
    ),
]


def all_queries() -> list[BenchQuery]:
    """Every benchmark query, across both axes."""
    return [*_ULTRALIGHT, *_LIGHT, *_HEAVY, *_RESULT_SIZE]

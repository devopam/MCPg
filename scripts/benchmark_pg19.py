"""Measure the PG 19 performance wins MCPg claims, on a real PG 19 server.

Companion to ``scripts/smoke_test_pg19.py`` — that script proves Phase 3
tools work; this one quantifies the headline operational wins. Without
it, every "PG 19 is faster / smaller" claim in the playbook is
unverified marketing.

Three benchmark pairs, all measurable without a server restart:

1. **Skip-scan vs dedicated single-column index.** A composite index
   ``(low_ndv_status, created_at)`` on PG 19 can satisfy a query
   filtering only on ``created_at`` via skip-scan. Pre-19 the planner
   would fall back to a seq scan unless a dedicated ``(created_at)``
   index existed. Benchmark times the same query against (a) the
   composite alone — PG 19 skip-scan, (b) a dedicated single-column
   index — works on every PG.

2. **REPACK CONCURRENTLY vs VACUUM FULL.** Same bloated table rebuilt
   two ways. VACUUM FULL holds ACCESS EXCLUSIVE the entire time; PG 19
   REPACK CONCURRENTLY lets reads + writes continue. The runtime
   numbers are interesting; the lock-window difference is the
   operational win.

3. **LZ4 vs pglz TOAST.** Same compressible payload inserted into two
   tables with explicit per-column compression settings. Compares the
   resulting ``pg_table_size`` and INSERT throughput.

AIO ``io_uring`` vs ``worker`` is intentionally **not** here — it
requires a server restart to change ``io_method``, so it lives as a
manual recipe in ``docs/plans/pg19-operations-playbook.md``.

The script targets a throwaway smoke instance — it creates and drops a
dedicated schema (``mcpg_bench``) and is safe to re-run idempotently.
Numbers come from ``EXPLAIN (ANALYZE, TIMING)`` and ``pg_table_size``
queries, not Python-side ``time.perf_counter`` (which would include
network RTT and psycopg overhead). Each timed query runs
``WARMUP_RUNS`` times to prime the buffer cache, then ``TIMED_RUNS``
times for the reported median.

Run via the launcher: ``scripts/benchmark_pg19.sh``.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import traceback
from typing import Any

from mcpg.config import load_settings
from mcpg.database import Database

# How many runs to discard for cache warm-up vs how many to take median over.
# 3+5 keeps the benchmark fast (each pair under ~20s on a laptop) while
# hiding cold-cache noise.
WARMUP_RUNS = 3
TIMED_RUNS = 5

# Row count for the skip-scan + REPACK fixtures. 100k is large enough
# for the index-strategy difference to dominate, small enough that the
# whole benchmark completes in well under a minute on a laptop.
FIXTURE_ROWS = 100_000

# Bytes per TOAST row for the LZ4/pglz comparison. 8 KiB is comfortably
# above the 2 KiB TOAST threshold so every row gets compressed.
TOAST_PAYLOAD_BYTES = 8 * 1024
TOAST_ROW_COUNT = 5_000

BENCH_SCHEMA = "mcpg_bench"


def _print(label: str, payload: Any) -> None:
    """Pretty-print one benchmark result with a header."""
    body = json.dumps(payload, indent=2, default=str)
    print(f"\n=== {label} ===")
    print(body)


async def _server_version_num(database: Database) -> int:
    """Return ``server_version_num`` so we can skip PG 19-only benchmarks
    on older servers (the script is still useful on PG 14-18 for the
    bits that don't require PG 19)."""
    driver = database.driver()
    rows = await driver.execute_query("SHOW server_version_num", [])
    return int(rows[0].cells["server_version_num"]) if rows else 0


async def _explain_analyze_total_ms(database: Database, sql: str) -> float:
    """Run ``EXPLAIN (ANALYZE, TIMING, FORMAT JSON) <sql>`` and return the
    plan's ``Execution Time`` in milliseconds. Server-side timing
    avoids round-trip + Python-side parse noise."""
    driver = database.driver()
    rows = await driver.execute_query(
        f"EXPLAIN (ANALYZE, TIMING, FORMAT JSON) {sql}",
        [],
    )
    # The plan is returned as a single row with one JSON column whose
    # name varies across drivers — pick the first cell value, which is
    # a list-with-one-dict per the EXPLAIN JSON contract.
    if not rows:
        raise RuntimeError("EXPLAIN returned no rows")
    plan = next(iter(rows[0].cells.values()))
    if isinstance(plan, str):
        plan = json.loads(plan)
    return float(plan[0]["Execution Time"])


async def _median_execution_ms(database: Database, sql: str) -> dict[str, Any]:
    """Run ``sql`` WARMUP+TIMED times under EXPLAIN ANALYZE; return
    median / min / max in milliseconds plus the raw sample list."""
    for _ in range(WARMUP_RUNS):
        await _explain_analyze_total_ms(database, sql)
    samples = [await _explain_analyze_total_ms(database, sql) for _ in range(TIMED_RUNS)]
    return {
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples": [round(s, 3) for s in samples],
    }


async def _table_size_bytes(database: Database, qualified: str) -> int:
    """Total on-disk size of ``qualified`` including its TOAST table."""
    driver = database.driver()
    rows = await driver.execute_query(
        "SELECT pg_table_size($1::regclass) AS bytes",
        [qualified],
    )
    return int(rows[0].cells["bytes"])


async def _setup_schema(database: Database) -> None:
    """Drop+recreate the dedicated benchmark schema so re-runs are clean."""
    await database.run_unmanaged(f"DROP SCHEMA IF EXISTS {BENCH_SCHEMA} CASCADE")
    await database.run_unmanaged(f"CREATE SCHEMA {BENCH_SCHEMA}")


async def _teardown_schema(database: Database) -> None:
    await database.run_unmanaged(f"DROP SCHEMA IF EXISTS {BENCH_SCHEMA} CASCADE")


async def bench_skip_scan(database: Database, ver_num: int) -> dict[str, Any]:
    """Skip-scan vs dedicated index on a composite ``(status, created_at)``.

    Fixture: ``FIXTURE_ROWS`` rows with 4 distinct ``status`` values and
    a monotonically increasing ``created_at``. The probe query filters
    ONLY on ``created_at`` — pre-19 with just the composite index this
    has to seq-scan; PG 19 picks up skip-scan and walks the composite
    one ``status`` value at a time.

    Two phases:

    * Phase A — composite only (``(status, created_at)``). On PG 19 the
      planner should pick skip-scan; on PG ≤ 18 it'll seq-scan.
    * Phase B — drop the composite, create a dedicated ``(created_at)``
      index. Works everywhere; serves as the "pre-19 catch-up" baseline.

    The interesting comparison is **Phase A median on PG 19 vs Phase B
    median** — i.e. does skip-scan close the gap to a dedicated index?
    """
    table = f"{BENCH_SCHEMA}.skip_scan_fixture"
    composite_idx = f"{BENCH_SCHEMA}_skip_scan_composite"
    dedicated_idx = f"{BENCH_SCHEMA}_skip_scan_dedicated"

    await database.run_unmanaged(
        f"""
        CREATE TABLE {table} (
            id BIGINT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            payload TEXT
        )
        """
    )
    await database.run_unmanaged(
        f"""
        INSERT INTO {table} (id, status, created_at, payload)
        SELECT
            g,
            (ARRAY['pending', 'active', 'done', 'failed'])[1 + (g % 4)],
            TIMESTAMPTZ '2025-01-01 00:00:00+00' + (g || ' minutes')::INTERVAL,
            repeat('x', 32)
        FROM generate_series(1, {FIXTURE_ROWS}) g
        """
    )
    await database.run_unmanaged(f"ANALYZE {table}")

    probe_sql = (
        f"SELECT count(*) FROM {table} "
        f"WHERE created_at BETWEEN "
        f"TIMESTAMPTZ '2025-01-15 00:00:00+00' AND "
        f"TIMESTAMPTZ '2025-01-15 23:59:59+00'"
    )

    # Phase A — composite alone.
    await database.run_unmanaged(f"CREATE INDEX {composite_idx} ON {table} (status, created_at)")
    composite_result = await _median_execution_ms(database, probe_sql)

    # Phase B — dedicated single-column index.
    await database.run_unmanaged(f"DROP INDEX {BENCH_SCHEMA}.{composite_idx}")
    await database.run_unmanaged(f"CREATE INDEX {dedicated_idx} ON {table} (created_at)")
    dedicated_result = await _median_execution_ms(database, probe_sql)

    skip_scan_available = ver_num >= 190000
    return {
        "available": skip_scan_available,
        "server_version_num": ver_num,
        "probe_sql": probe_sql,
        "rows_in_fixture": FIXTURE_ROWS,
        "composite_index_only": composite_result,
        "dedicated_index": dedicated_result,
        "interpretation": (
            "On PG 19 the composite-only median should be close to the dedicated-index "
            "median (skip-scan kicks in). On PG <= 18 the composite-only median is the "
            "seq-scan baseline."
        ),
    }


async def bench_repack_vs_vacuum_full(database: Database, ver_num: int) -> dict[str, Any]:
    """REPACK CONCURRENTLY (PG 19) vs VACUUM FULL on the same bloated table.

    Fixture: a table with significant dead-tuple bloat (insert N rows,
    UPDATE every row to double the live size, then DELETE half).
    Rebuild it once with VACUUM FULL, then re-create the bloat and
    rebuild with REPACK CONCURRENTLY. Both operations are timed with
    ``clock_timestamp()`` deltas (they cannot be wrapped in EXPLAIN).
    """
    if ver_num < 190000:
        return {
            "available": False,
            "server_version_num": ver_num,
            "skipped": "REPACK requires PostgreSQL 19; this server is older.",
        }

    table = f"{BENCH_SCHEMA}.repack_fixture"
    driver = database.driver()

    async def _build_bloat() -> int:
        """Recreate the bloated fixture and return its post-bloat size."""
        await database.run_unmanaged(f"DROP TABLE IF EXISTS {table}")
        await database.run_unmanaged(
            f"""
            CREATE TABLE {table} (
                id BIGINT PRIMARY KEY,
                payload TEXT
            )
            """
        )
        await database.run_unmanaged(
            f"""
            INSERT INTO {table} (id, payload)
            SELECT g, repeat(md5(g::text), 4)
            FROM generate_series(1, {FIXTURE_ROWS}) g
            """
        )
        # Force bloat: UPDATE rewrites every row (doubles dead tuples).
        await database.run_unmanaged(f"UPDATE {table} SET payload = payload || 'x'")
        # Delete half — leaves dead tuples without immediate auto-vacuum reclaim.
        await database.run_unmanaged(f"DELETE FROM {table} WHERE id % 2 = 0")
        return await _table_size_bytes(database, table)

    async def _timed_rebuild(sql: str) -> float:
        """Run ``sql`` on the unmanaged connection and time it server-side
        via ``clock_timestamp()`` bookends. Returns elapsed milliseconds."""
        rows = await driver.execute_query("SELECT extract(epoch FROM clock_timestamp())", [])
        start = float(next(iter(rows[0].cells.values())))
        await database.run_unmanaged(sql)
        rows = await driver.execute_query("SELECT extract(epoch FROM clock_timestamp())", [])
        end = float(next(iter(rows[0].cells.values())))
        return round((end - start) * 1000.0, 3)

    pre_vacuum_bytes = await _build_bloat()
    vacuum_ms = await _timed_rebuild(f"VACUUM FULL {table}")
    post_vacuum_bytes = await _table_size_bytes(database, table)

    pre_repack_bytes = await _build_bloat()
    repack_ms = await _timed_rebuild(f"REPACK {table} CONCURRENTLY")
    post_repack_bytes = await _table_size_bytes(database, table)

    return {
        "available": True,
        "server_version_num": ver_num,
        "rows_in_fixture": FIXTURE_ROWS,
        "vacuum_full": {
            "elapsed_ms": vacuum_ms,
            "pre_bytes": pre_vacuum_bytes,
            "post_bytes": post_vacuum_bytes,
            "lock_mode": "ACCESS EXCLUSIVE (blocks all reads + writes)",
        },
        "repack_concurrently": {
            "elapsed_ms": repack_ms,
            "pre_bytes": pre_repack_bytes,
            "post_bytes": post_repack_bytes,
            "lock_mode": "no long-held heavy lock (online rebuild)",
        },
        "interpretation": (
            "VACUUM FULL is usually faster wall-clock but holds ACCESS EXCLUSIVE for the "
            "entire run; REPACK CONCURRENTLY trades a small wall-clock penalty for "
            "uninterrupted reads + writes against the table."
        ),
    }


async def bench_lz4_vs_pglz_toast(database: Database) -> dict[str, Any]:
    """LZ4 vs pglz TOAST compression on the same compressible payload.

    Two tables with explicit per-column ``COMPRESSION`` settings. Same
    rows inserted into each. Compares the resulting ``pg_table_size``
    (which counts the TOAST table) plus INSERT throughput.

    LZ4 has been available since PG 14 and became the *default* in
    PG 19; the comparison is still useful pre-19 because the question
    "what does LZ4 buy me on my workload?" is the same on either side
    of the default flip.
    """
    lz4_table = f"{BENCH_SCHEMA}.toast_lz4"
    pglz_table = f"{BENCH_SCHEMA}.toast_pglz"
    driver = database.driver()

    # Highly compressible: long runs of the same character pattern.
    # Real workloads see worse ratios; this gives both sides the same
    # input so any difference is purely the algorithm.
    payload_sql = f"repeat('Lorem ipsum dolor sit amet ', {TOAST_PAYLOAD_BYTES // 27})"

    for table_name, compression in ((lz4_table, "lz4"), (pglz_table, "pglz")):
        await database.run_unmanaged(
            f"""
            CREATE TABLE {table_name} (
                id BIGINT PRIMARY KEY,
                payload TEXT COMPRESSION {compression} STORAGE EXTENDED
            )
            """
        )

    async def _timed_insert(table_name: str) -> float:
        rows = await driver.execute_query("SELECT extract(epoch FROM clock_timestamp())", [])
        start = float(next(iter(rows[0].cells.values())))
        await database.run_unmanaged(
            f"""
            INSERT INTO {table_name} (id, payload)
            SELECT g, {payload_sql}
            FROM generate_series(1, {TOAST_ROW_COUNT}) g
            """
        )
        rows = await driver.execute_query("SELECT extract(epoch FROM clock_timestamp())", [])
        end = float(next(iter(rows[0].cells.values())))
        return round((end - start) * 1000.0, 3)

    lz4_insert_ms = await _timed_insert(lz4_table)
    pglz_insert_ms = await _timed_insert(pglz_table)

    lz4_bytes = await _table_size_bytes(database, lz4_table)
    pglz_bytes = await _table_size_bytes(database, pglz_table)

    ratio = round(lz4_bytes / pglz_bytes, 3) if pglz_bytes else None
    return {
        "available": True,
        "rows_inserted": TOAST_ROW_COUNT,
        "payload_bytes_per_row": TOAST_PAYLOAD_BYTES,
        "lz4": {"insert_ms": lz4_insert_ms, "table_size_bytes": lz4_bytes},
        "pglz": {"insert_ms": pglz_insert_ms, "table_size_bytes": pglz_bytes},
        "lz4_to_pglz_size_ratio": ratio,
        "interpretation": (
            "Ratio < 1 means LZ4 produced a smaller TOAST table; LZ4 also typically "
            "compresses + decompresses faster than pglz on text workloads."
        ),
    }


async def main() -> int:
    url = os.environ.get("MCPG_TEST_DATABASE_URL")
    if not url:
        print(
            "MCPG_TEST_DATABASE_URL is not set. Run via scripts/benchmark_pg19.sh instead.",
            file=sys.stderr,
        )
        return 2
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": url,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
        }
    )
    database = Database(settings)
    try:
        await database.connect()
        print(">>> Connected to", url)
        ver_num = await _server_version_num(database)
        print(f">>> server_version_num = {ver_num}")

        await _setup_schema(database)

        skip_scan = await bench_skip_scan(database, ver_num)
        _print("Skip-scan vs dedicated index", skip_scan)

        repack = await bench_repack_vs_vacuum_full(database, ver_num)
        _print("REPACK CONCURRENTLY vs VACUUM FULL", repack)

        # Re-create the schema between scenarios so each one starts
        # from a clean slate without inheriting prior fixture rows.
        await _setup_schema(database)
        toast = await bench_lz4_vs_pglz_toast(database)
        _print("LZ4 vs pglz TOAST", toast)

        await _teardown_schema(database)
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        await database.close()
    print("\n>>> Benchmark complete.")
    return 0


if __name__ == "__main__":
    # Mirrors smoke_test_pg19.py — psycopg async doesn't play nicely
    # with the Windows ProactorEventLoop default.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))

"""Live smoke test of every Phase 3 PG 19 tool against a real PG 19 server.

Driven by ``scripts/smoke_test_pg19.sh``, which spins up a PG 19 Beta
container and points ``MCPG_TEST_DATABASE_URL`` at it. This script
walks each Phase 3 tool's status probe + (where safely possible) one
representative write, and prints what the tool returned.

The point isn't *test* the tools — that's what
``tests/unit/test_*.py`` and the contract snapshots do. The point is
to confirm that:

  1. Our SQL is accepted by the real PG 19 build.
  2. The diagnostics we wrote are accurate when the feature is
     genuinely present.
  3. Operators can copy-paste this script as a "what does MCPg
     actually do on PG 19" demo.

It is intentionally read-mostly. The only write paths exercised:

  - ``enable_logical_replication_on_demand`` — flips wal_level to
    logical; harmless on a throwaway smoke instance.
  - ``enable_data_checksums`` — toggles the GUC + kicks off the
    background rewriter; harmless because the container is going to
    be discarded.

Both writes are skipped when the status probe reports the feature
isn't available — so on a Beta build that doesn't ship them yet, the
script still completes cleanly with informative output.

Run via the launcher: ``scripts/smoke_test_pg19.sh``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import traceback
from collections.abc import Awaitable
from typing import Any

from mcpg.config import load_settings
from mcpg.database import Database


def _emit(label: str, payload: Any) -> None:
    """Pretty-print one probe / write result with a header."""
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    elif isinstance(payload, list) and payload and dataclasses.is_dataclass(payload[0]):
        payload = [dataclasses.asdict(p) for p in payload]
    body = json.dumps(payload, indent=2, default=str)
    print(f"\n=== {label} ===")
    print(body)


async def _safe(label: str, awaitable: Awaitable[Any]) -> Any:
    """Run an awaitable and emit its result, capturing exceptions so a single
    feature being unavailable doesn't abort the whole smoke run."""
    try:
        result = await awaitable
        _emit(label, result)
        return result
    except Exception as exc:
        _emit(label, {"error_type": type(exc).__name__, "error": str(exc)})
        return None


async def _exercise_status_probes(database: Database) -> dict[str, dict[str, Any]]:
    """Phase 1 — every Phase 3 status probe (never raises).

    Returns a dict from tool name to the parsed result, so the second
    phase can decide which writes to attempt based on per-feature
    availability.
    """
    from mcpg import aio, pg19_ddl, pg19_partitions, pg19_runtime, pg19_skip_scan, pg19_stats, pgq, repack

    driver = database.driver()
    results: dict[str, dict[str, Any]] = {}

    for label, helper in (
        ("get_pgq_status", pgq.get_pgq_status(driver)),
        ("get_repack_status", repack.get_repack_status(driver)),
        ("get_aio_status", aio.get_aio_status(driver)),
        ("get_pg19_stats_status", pg19_stats.get_pg19_stats_status(driver)),
        ("get_data_checksums_status", pg19_runtime.get_data_checksums_status(driver)),
        (
            "get_logical_replication_status",
            pg19_runtime.get_logical_replication_status(driver),
        ),
        ("get_pg19_ddl_status", pg19_ddl.get_pg19_ddl_status(driver)),
        ("get_pg19_partitions_status", pg19_partitions.get_pg19_partitions_status(driver)),
        ("get_skip_scan_status", pg19_skip_scan.get_skip_scan_status(driver)),
    ):
        result = await _safe(label, helper)
        if result is not None:
            results[label] = dataclasses.asdict(result)
    return results


async def _exercise_advisors(database: Database) -> None:
    """Phase 2 — read-only advisors that are safe to call on any cluster."""
    from mcpg import aio, pg19_ddl, pg19_skip_scan, pg19_stats

    driver = database.driver()
    await _safe("recommend_io_method", aio.recommend_io_method(driver))
    await _safe("analyze_lock_hotspots", pg19_stats.analyze_lock_hotspots(driver))
    await _safe("read_pg_stat_lock", pg19_stats.read_pg_stat_lock(driver))
    await _safe("read_pg_stat_recovery", pg19_stats.read_pg_stat_recovery(driver))
    await _safe("list_property_graphs", pgq_list_safely(driver))
    # PG 19 DDL dumps — exercise on stock-cluster objects that always
    # exist (the bootstrap superuser, the connected database, the
    # default tablespace). Each tool raises on PG ≤ 18; _safe captures
    # the error so the smoke run keeps moving.
    await _safe("get_role_ddl(postgres)", pg19_ddl.get_role_ddl(driver, "postgres"))
    await _safe(
        "get_database_ddl(postgres)",
        pg19_ddl.get_database_ddl(driver, "postgres"),
    )
    await _safe(
        "get_tablespace_ddl(pg_default)",
        pg19_ddl.get_tablespace_ddl(driver, "pg_default"),
    )
    # PG 19 skip-scan advisor — exercise on whatever indexes the
    # smoke container happens to have. On a stock container with no
    # user indexes the result will be an empty list; that's a clean
    # signal that the query ran and the planner detection works.
    await _safe(
        "recommend_skip_scan_indexes",
        pg19_skip_scan.recommend_skip_scan_indexes(driver),
    )


async def pgq_list_safely(driver: Any) -> Any:
    """``list_property_graphs`` swallows missing-view errors internally —
    wrap here purely so the label appears in the report alongside the
    other read calls."""
    from mcpg import pgq

    return await pgq.list_property_graphs(driver)


def _print_capability_matrix(probes: dict[str, dict[str, Any]]) -> None:
    """Two-column "available?" matrix so a reviewer can see at a glance
    which PG 19 features actually showed up on this server."""
    print("\n=== Capability matrix ===")
    rows: list[tuple[str, str]] = []
    for label, payload in probes.items():
        if payload.get("available") is True:
            available = "available"
        elif payload.get("available") is False:
            available = "unavailable"
        else:
            available = "(no `available` field)"
        rows.append((label, available))
    width = max(len(label) for label, _ in rows) if rows else 0
    for label, available in rows:
        print(f"  {label.ljust(width)}  {available}")


async def _exercise_safe_writes(database: Database, probes: dict[str, dict[str, Any]]) -> None:
    """Phase 3 — representative writes, gated on the status probe.

    Each write is skipped (with a clear note) when its feature is
    reported unavailable, so the smoke script completes cleanly on
    early-Beta builds that haven't shipped every feature yet.
    """
    from mcpg import pg19_runtime

    logical_status = probes.get("get_logical_replication_status", {})
    if logical_status.get("available"):
        print("\n>>> Exercising enable_logical_replication_on_demand")
        await _safe(
            "enable_logical_replication_on_demand",
            pg19_runtime.enable_logical_replication_on_demand(database),
        )
    else:
        _emit("enable_logical_replication_on_demand", {"skipped": "status reports unavailable"})

    checksums_status = probes.get("get_data_checksums_status", {})
    if checksums_status.get("available") and checksums_status.get("enabled") is False:
        print("\n>>> Exercising enable_data_checksums (currently disabled)")
        await _safe("enable_data_checksums", pg19_runtime.enable_data_checksums(database))
    else:
        _emit(
            "enable_data_checksums",
            {"skipped": ("status reports unavailable or already enabled — skipping to keep the smoke read-mostly")},
        )

    # validate_check_constraint / merge_partitions / split_partition all
    # require pre-seeded objects (a NOT VALID constraint, a partitioned
    # table with the right shape). Round-tripping create + populate +
    # exercise + drop would blur the "what does MCPg do" report — skip
    # with notes and rely on the unit tests for behavioural coverage.
    _emit(
        "validate_check_constraint",
        {"skipped": "requires a pre-seeded NOT VALID constraint — see tests/unit/test_pg19_ddl.py"},
    )
    _emit(
        "merge_partitions",
        {"skipped": "requires a pre-seeded partitioned table — see tests/unit/test_pg19_partitions.py"},
    )
    _emit(
        "split_partition",
        {"skipped": "requires a pre-seeded partitioned table — see tests/unit/test_pg19_partitions.py"},
    )


async def main() -> int:
    url = os.environ.get("MCPG_TEST_DATABASE_URL")
    if not url:
        print(
            "MCPG_TEST_DATABASE_URL is not set. Run via scripts/smoke_test_pg19.sh instead.",
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
        # connect() inside the try block so a connection failure still
        # routes through the finally cleanup (gemini review on PR #143).
        await database.connect()
        print(">>> Connected to", url)
        probes = await _exercise_status_probes(database)
        _print_capability_matrix(probes)
        await _exercise_advisors(database)
        await _exercise_safe_writes(database, probes)
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        await database.close()
    print("\n>>> Smoke complete.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

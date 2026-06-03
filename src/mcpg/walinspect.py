"""PostgreSQL pg_walinspect extension reader."""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True, slots=True)
class WalRecordRow:
    """Detailed information for a single WAL record."""

    start_lsn: str
    end_lsn: str
    prev_lsn: str
    xid: int
    resource_manager: str
    record_type: str
    record_length: int
    main_data_length: int
    fpi_length: int
    description: str
    block_ref: str | None


@dataclass(frozen=True, slots=True)
class WalRecordsReport:
    """WAL records report."""

    available: bool
    records: list[WalRecordRow]


@dataclass(frozen=True, slots=True)
class WalStatRow:
    """Aggregated WAL statistics for a resource manager or record type."""

    resource_manager_or_record_type: str
    count: int
    count_percentage: float
    record_size: int
    record_size_percentage: float
    fpi_size: int
    fpi_size_percentage: float
    combined_size: int
    combined_size_percentage: float


@dataclass(frozen=True, slots=True)
class WalStatsReport:
    """WAL statistics report."""

    available: bool
    stats: list[WalStatRow]


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[call-overload,no-any-return]


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


async def read_pg_wal_records(
    driver: SqlDriver,
    start_lsn: str,
    end_lsn: str = "FFFFFFFF/FFFFFFFF",
    limit: int = 100,
) -> WalRecordsReport:
    """Read WAL records info using ``pg_walinspect``.

    Requires the ``pg_walinspect`` extension. Returns ``available=False`` if not installed.
    """
    from mcpg.extensions import extension_installed

    if not await extension_installed(driver, "pg_walinspect"):
        return WalRecordsReport(available=False, records=[])

    rows = await driver.execute_query(
        "SELECT start_lsn::text AS start_lsn, "
        "       end_lsn::text AS end_lsn, "
        "       prev_lsn::text AS prev_lsn, "
        "       xid::text::bigint AS xid, "
        "       resource_manager, "
        "       record_type, "
        "       record_length, "
        "       main_data_length, "
        "       fpi_length, "
        "       description, "
        "       block_ref "
        "FROM pg_get_wal_records_info(%s::pg_lsn, %s::pg_lsn) "
        "LIMIT %s",
        params=[start_lsn, end_lsn, limit],
        force_readonly=True,
    )

    records = [
        WalRecordRow(
            start_lsn=str(row.cells["start_lsn"]),
            end_lsn=str(row.cells["end_lsn"]),
            prev_lsn=str(row.cells["prev_lsn"]),
            xid=int(row.cells["xid"]),
            resource_manager=str(row.cells["resource_manager"]),
            record_type=str(row.cells["record_type"]),
            record_length=int(row.cells["record_length"]),
            main_data_length=int(row.cells["main_data_length"]),
            fpi_length=int(row.cells["fpi_length"]),
            description=str(row.cells["description"]),
            block_ref=str(row.cells["block_ref"]) if row.cells.get("block_ref") is not None else None,
        )
        for row in rows or []
    ]

    return WalRecordsReport(available=True, records=records)


async def read_pg_wal_stats(
    driver: SqlDriver,
    start_lsn: str,
    end_lsn: str = "FFFFFFFF/FFFFFFFF",
    per_record: bool = False,
) -> WalStatsReport:
    """Read WAL record statistics using ``pg_walinspect``.

    Requires the ``pg_walinspect`` extension. Returns ``available=False`` if not installed.
    """
    from mcpg.extensions import extension_installed

    if not await extension_installed(driver, "pg_walinspect"):
        return WalStatsReport(available=False, stats=[])

    # By default, pg_get_wal_stats groups by resource_manager.
    # If per_record=True, it groups by record_type instead.
    # The first column is dynamically named record_type or resource_manager.
    first_col = "record_type" if per_record else "resource_manager"

    rows = await driver.execute_query(
        f"SELECT {first_col}, "
        "       count, "
        "       count_percentage, "
        "       record_size, "
        "       record_size_percentage, "
        "       fpi_size, "
        "       fpi_size_percentage, "
        "       combined_size, "
        "       combined_size_percentage "
        "FROM pg_get_wal_stats(%s::pg_lsn, %s::pg_lsn, %s)",
        params=[start_lsn, end_lsn, per_record],
        force_readonly=True,
    )

    stats = [
        WalStatRow(
            resource_manager_or_record_type=str(row.cells[first_col]),
            count=int(row.cells["count"]),
            count_percentage=float(row.cells["count_percentage"]),
            record_size=int(row.cells["record_size"]),
            record_size_percentage=float(row.cells["record_size_percentage"]),
            fpi_size=int(row.cells["fpi_size"]),
            fpi_size_percentage=float(row.cells["fpi_size_percentage"]),
            combined_size=int(row.cells["combined_size"]),
            combined_size_percentage=float(row.cells["combined_size_percentage"]),
        )
        for row in rows or []
    ]

    return WalStatsReport(available=True, stats=stats)

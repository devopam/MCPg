"""pg_partman partition-management wrappers.

pg_partman drives PostgreSQL's declarative partitioning with helpers
for creating partition sets, running periodic maintenance (adding
forward partitions, dropping retired ones), and retention-based
partition drops.

All tools are write-gated. Each raises :class:`PartmanError` when the
extension is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg.extensions import extension_installed
from mcpg.sql import SqlDriver

# pg_partman accepts these partition-type strings since version 5.x —
# 'range' and 'list' for declarative partitioning, 'native' as a legacy
# alias. The allowlist guards against arbitrary identifier injection.
_PARTITION_TYPES = frozenset({"range", "list", "native"})


class PartmanError(Exception):
    """Raised when a pg_partman operation cannot complete."""


@dataclass(frozen=True)
class PartmanResult:
    """Generic outcome of a pg_partman call."""

    parent_table: str
    detail: str


async def _ensure_installed(driver: SqlDriver) -> None:
    if not await extension_installed(driver, "pg_partman"):
        raise PartmanError("pg_partman extension is not installed in this database")


async def partman_create_parent(
    driver: SqlDriver,
    parent_table: str,
    control_column: str,
    partition_interval: str,
    *,
    partition_type: str = "range",
) -> PartmanResult:
    """Register ``parent_table`` as a pg_partman-managed partition set.

    ``parent_table`` must already exist as a partitioned table.
    ``control_column`` is the column pg_partman uses to derive
    partition bounds; ``partition_interval`` is a string accepted by
    pg_partman (e.g. ``"daily"``, ``"1 month"``, ``"1000"``).

    Raises:
        PartmanError: pg_partman is not installed, ``partition_type``
            is not in the allowlist, or the underlying create_parent
            call returns false.
    """
    await _ensure_installed(driver)
    if partition_type not in _PARTITION_TYPES:
        raise PartmanError(f"unsupported partition_type {partition_type!r}; expected one of {sorted(_PARTITION_TYPES)}")
    rows = await driver.execute_query(
        "SELECT partman.create_parent("
        "  p_parent_table := %s, "
        "  p_control := %s, "
        "  p_type := %s, "
        "  p_interval := %s"
        ") AS created",
        params=[parent_table, control_column, partition_type, partition_interval],
    )
    created = bool(rows and rows[0].cells["created"])
    if not created:
        raise PartmanError(f"partman.create_parent returned false for {parent_table!r}")
    return PartmanResult(parent_table=parent_table, detail="created")


async def partman_run_maintenance(driver: SqlDriver, parent_table: str | None = None) -> PartmanResult:
    """Run pg_partman maintenance — add forward partitions, drop retired ones.

    When ``parent_table`` is ``None``, maintenance runs for every
    pg_partman-managed parent in the database.
    """
    await _ensure_installed(driver)
    if parent_table is None:
        await driver.execute_query("SELECT partman.run_maintenance()")
        return PartmanResult(parent_table="*", detail="ran maintenance across all parents")
    await driver.execute_query(
        "SELECT partman.run_maintenance(p_parent_table := %s)",
        params=[parent_table],
    )
    return PartmanResult(parent_table=parent_table, detail="ran maintenance")


async def partman_drop_partition(
    driver: SqlDriver,
    parent_table: str,
    retention: str,
    *,
    control_is_time: bool = True,
) -> list[str]:
    """Drop pg_partman partitions older than ``retention``.

    For time-controlled parents (the default), ``retention`` is a
    PostgreSQL interval (``"30 days"``, ``"1 year"``). For id-controlled
    parents, set ``control_is_time=False`` and pass an integer-like
    string (``"1000000"``).

    Returns the qualified names of the dropped partitions.
    """
    await _ensure_installed(driver)
    if control_is_time:
        rows = await driver.execute_query(
            "SELECT partman.drop_partition_time(  p_parent_table := %s,   p_retention := %s) AS dropped",
            params=[parent_table, retention],
        )
    else:
        rows = await driver.execute_query(
            "SELECT partman.drop_partition_id(  p_parent_table := %s,   p_retention := %s::bigint) AS dropped",
            params=[parent_table, retention],
        )
    return [row.cells["dropped"] for row in rows or [] if row.cells["dropped"]]

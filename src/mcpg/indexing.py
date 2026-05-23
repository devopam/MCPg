"""Index recommendations from table scan statistics and column types.

A table-level heuristic flags large tables read mostly by sequential scan;
for each, column types drive per-column index-type suggestions (GIN for
``jsonb``/arrays, trigram GIN for text). Choosing exactly which columns a
workload filters on still needs query analysis (see ``analyze_workload``).
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Tables smaller than this are ignored — sequential scans of them are cheap.
DEFAULT_MIN_LIVE_TUPLES = 10_000

# information_schema.columns data types treated as text for trigram indexing.
_TEXT_TYPES = frozenset({"text", "character varying", "character"})


@dataclass(frozen=True, slots=True)
class IndexSuggestion:
    """A suggested index for one column of a candidate table."""

    column: str
    index_type: str
    rationale: str


@dataclass(frozen=True, slots=True)
class IndexRecommendation:
    """A table that may benefit from indexing, with per-column suggestions.

    For a partitioned table, ``partitioned`` is ``True`` and the scan and row
    counts are summed across its partitions — an index created on the parent
    propagates to them all.
    """

    schema: str
    table: str
    seq_scans: int
    live_tuples: int
    reason: str
    suggestions: list[IndexSuggestion]
    partitioned: bool


def _suggest(column: str, data_type: str) -> IndexSuggestion | None:
    """Suggest an index type for a column based on its data type, if any."""
    if data_type == "jsonb":
        return IndexSuggestion(column, "gin", "GIN supports jsonb containment and key lookups")
    if data_type == "ARRAY":
        return IndexSuggestion(column, "gin", "GIN supports array membership queries")
    if data_type in _TEXT_TYPES:
        return IndexSuggestion(column, "gin_trgm", "trigram GIN (pg_trgm) accelerates LIKE/ILIKE pattern search")
    return None


async def recommend_indexes(
    driver: SqlDriver, *, min_live_tuples: int = DEFAULT_MIN_LIVE_TUPLES
) -> list[IndexRecommendation]:
    """Recommend tables that may benefit from indexing, with column hints.

    Heuristic: large tables (at least ``min_live_tuples`` rows) read more
    often by sequential scan than by index scan. For each, columns with
    GIN-friendly types yield an :class:`IndexSuggestion`. A flagged partition
    is rolled up to its partitioned parent, since an index belongs on the
    parent; scan and row counts are summed across the partitions. The
    partitioned parent's own (empty) stats row is excluded from the
    aggregation so partition stats are not double-counted.

    Args:
        driver: The SQL driver to query through.
        min_live_tuples: Smallest table (row estimate) worth flagging.
    """
    rows = await driver.execute_query(
        "SELECT s.schemaname, s.relname, s.seq_scan, s.n_live_tup, "
        "c.column_name, c.data_type, "
        "pn.nspname AS parent_schema, parent.relname AS parent_table "
        "FROM pg_stat_user_tables s "
        "JOIN pg_class self ON self.oid = s.relid AND self.relkind <> 'p' "
        "JOIN information_schema.columns c "
        "  ON c.table_schema = s.schemaname AND c.table_name = s.relname "
        "LEFT JOIN pg_inherits inh ON inh.inhrelid = s.relid "
        "LEFT JOIN pg_class parent "
        "  ON parent.oid = inh.inhparent AND parent.relkind = 'p' "
        "LEFT JOIN pg_namespace pn ON pn.oid = parent.relnamespace "
        "WHERE s.n_live_tup >= %s AND s.seq_scan > COALESCE(s.idx_scan, 0) "
        "ORDER BY s.seq_scan DESC, s.relname, c.ordinal_position",
        params=[min_live_tuples],
        force_readonly=True,
    )

    order: list[tuple[str, str]] = []
    stats: dict[tuple[str, str], list[int]] = {}
    partitioned: dict[tuple[str, str], bool] = {}
    suggestions: dict[tuple[str, str], list[IndexSuggestion]] = {}
    suggested_columns: dict[tuple[str, str], set[str]] = {}
    counted: set[tuple[str, str]] = set()
    for row in rows or []:
        parent_table = row.cells["parent_table"]
        is_partition = parent_table is not None
        if is_partition:
            key = (row.cells["parent_schema"], parent_table)
        else:
            key = (row.cells["schemaname"], row.cells["relname"])
        if key not in stats:
            order.append(key)
            stats[key] = [0, 0]
            partitioned[key] = False
            suggestions[key] = []
            suggested_columns[key] = set()
        physical = (row.cells["schemaname"], row.cells["relname"])
        if physical not in counted:
            counted.add(physical)
            stats[key][0] += row.cells["seq_scan"]
            stats[key][1] += row.cells["n_live_tup"]
            partitioned[key] = partitioned[key] or is_partition
        suggestion = _suggest(row.cells["column_name"], row.cells["data_type"])
        if suggestion is not None and suggestion.column not in suggested_columns[key]:
            suggested_columns[key].add(suggestion.column)
            suggestions[key].append(suggestion)

    return [
        IndexRecommendation(
            schema=schema,
            table=table,
            seq_scans=stats[(schema, table)][0],
            live_tuples=stats[(schema, table)][1],
            reason=(
                "partitioned table whose partitions are read mostly by sequential scan"
                if partitioned[(schema, table)]
                else "large table read mostly by sequential scan"
            ),
            suggestions=suggestions[(schema, table)],
            partitioned=partitioned[(schema, table)],
        )
        for schema, table in order
    ]

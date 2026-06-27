"""Index recommendations from table scan statistics and column types.

A table-level heuristic flags large tables read mostly by sequential scan;
for each, column types drive per-column index-type suggestions (GIN for
``jsonb``/arrays, trigram GIN for text). Choosing exactly which columns a
workload filters on still needs query analysis (see ``analyze_workload``).

The companion :func:`recommend_index_drops` looks at the same scan
statistics from the opposite angle — flagging *existing* indexes that
are large but rarely (or never) scanned, so disk + write amplification
can be reclaimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver

# Tables smaller than this are ignored — sequential scans of them are cheap.
DEFAULT_MIN_LIVE_TUPLES = 10_000

# Skip indexes below this size when looking for drop candidates — even a
# never-scanned 8 KB index isn't worth recommending the operator drop.
# Dropping reclaims disk and write amplification only when there's real
# size involved.
DEFAULT_MIN_INDEX_SIZE_BYTES = 1_000_000

# "Rarely scanned" threshold: an index is flagged if its scan count is
# below this fraction of the table's sequential-scan count. 0.01 = 1%;
# below that we consider it a marginal asset whose drop is worth
# evaluating against the disk-space win.
DEFAULT_LOW_SCAN_RATIO = 0.01

# information_schema.columns data types treated as text for trigram indexing.
_TEXT_TYPES = frozenset({"text", "character varying", "character"})


@dataclass(frozen=True)
class IndexSuggestion:
    """A suggested index for one column of a candidate table."""

    column: str
    index_type: str
    rationale: str


@dataclass(frozen=True)
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


@dataclass(slots=True)
class _TableAgg:
    """Per-logical-table aggregator used while rolling up scan stats."""

    seq_scans: int = 0
    live_tuples: int = 0
    partitioned: bool = False
    suggestions: list[IndexSuggestion] = field(default_factory=list)
    _seen_columns: set[str] = field(default_factory=set, repr=False)

    def add_stats(self, seq_scan: int, live_tup: int, is_partition: bool) -> None:
        self.seq_scans += seq_scan
        self.live_tuples += live_tup
        self.partitioned = self.partitioned or is_partition

    def add_suggestion(self, column: str, data_type: str) -> None:
        if column in self._seen_columns:
            return
        suggestion = _suggest(column, data_type)
        if suggestion is None:
            return
        self._seen_columns.add(column)
        self.suggestions.append(suggestion)


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
    tables: dict[tuple[str, str], _TableAgg] = {}
    counted: set[tuple[str, str]] = set()
    for row in rows or []:
        parent_table = row.cells["parent_table"]
        is_partition = parent_table is not None
        if is_partition:
            key = (row.cells["parent_schema"], parent_table)
        else:
            key = (row.cells["schemaname"], row.cells["relname"])
        if key not in tables:
            tables[key] = _TableAgg()
            order.append(key)
        agg = tables[key]
        physical = (row.cells["schemaname"], row.cells["relname"])
        if physical not in counted:
            counted.add(physical)
            agg.add_stats(row.cells["seq_scan"], row.cells["n_live_tup"], is_partition)
        agg.add_suggestion(row.cells["column_name"], row.cells["data_type"])

    return [
        IndexRecommendation(
            schema=schema,
            table=table,
            seq_scans=tables[(schema, table)].seq_scans,
            live_tuples=tables[(schema, table)].live_tuples,
            reason=(
                "partitioned table whose partitions are read mostly by sequential scan"
                if tables[(schema, table)].partitioned
                else "large table read mostly by sequential scan"
            ),
            suggestions=tables[(schema, table)].suggestions,
            partitioned=tables[(schema, table)].partitioned,
        )
        for schema, table in order
    ]


# --- recommend_index_drops ------------------------------------------------


# Drop-reason taxonomy. ``never_used`` is the strongest signal (pure
# disk + write-amp tax for no read benefit). ``scan_no_fetch`` flags
# indexes the planner picks but which never actually return matching
# rows — typically existence-only access patterns that a partial
# index would serve more cheaply. ``rarely_used`` is the marginal case
# where the index is hit but its scan rate is dwarfed by the table's
# sequential-scan rate; operators evaluate against disk savings.
DROP_REASON_NEVER_USED = "never_used"
DROP_REASON_SCAN_NO_FETCH = "scan_no_fetch"
DROP_REASON_RARELY_USED = "rarely_used"


@dataclass(frozen=True)
class IndexDropCandidate:
    """One index whose scan stats suggest dropping it.

    ``drop_sql`` is a ready-to-run ``DROP INDEX CONCURRENTLY`` statement
    so the operator can paste it into a maintenance window. Not
    executed automatically — this is a read-only advisor.

    ``reason_code`` matches one of the ``DROP_REASON_*`` constants so
    a caller can route on it; ``rationale`` is the human-readable
    explanation embedding the numbers we keyed on.
    """

    schema: str
    table: str
    index: str
    size_bytes: int
    idx_scan: int
    idx_tup_fetch: int
    table_seq_scan: int
    table_idx_scan: int
    reason_code: str
    rationale: str
    drop_sql: str
    definition: str


async def recommend_index_drops(
    driver: SqlDriver,
    *,
    schema: str | None = None,
    min_index_size_bytes: int = DEFAULT_MIN_INDEX_SIZE_BYTES,
    low_scan_ratio: float = DEFAULT_LOW_SCAN_RATIO,
) -> list[IndexDropCandidate]:
    """Recommend existing indexes that look like drop candidates.

    Sibling of :func:`recommend_indexes`: walks ``pg_stat_user_indexes``
    + ``pg_stat_user_tables`` and flags indexes that look like pure
    cost — large on disk but never (or barely) scanned. Three signals,
    in descending strength:

    - ``never_used`` — ``idx_scan == 0``. Dropping the index is risk-
      free for read performance; the only tax is the write
      amplification and disk space the index has been carrying.
    - ``scan_no_fetch`` — ``idx_scan > 0`` but ``idx_tup_fetch == 0``.
      The planner picks the index but it never returns matching rows;
      usually an existence-check pattern that a partial index (or
      no index at all) would serve more cheaply.
    - ``rarely_used`` — ``idx_scan > 0`` and ``idx_scan <
      low_scan_ratio * (table seq_scan + table idx_scan)``. The
      index is being hit but at a rate that's dwarfed by the table's
      overall activity; the operator should weigh the disk + write-
      amplification cost against the marginal read benefit.

    Primary-key, unique, and exclusion-constraint indexes are
    skipped — dropping those breaks integrity. Indexes below
    ``min_index_size_bytes`` are also skipped: even a never-scanned
    8 KB index isn't worth the operator's attention.

    Args:
        schema: Optional schema filter. ``None`` scans every non-system
            schema visible to ``pg_stat_user_indexes``.
        min_index_size_bytes: Indexes smaller than this are ignored.
            Default 1 MB.
        low_scan_ratio: ``idx_scan`` fraction below which an
            otherwise-used index counts as ``rarely_used``. Default
            ``0.01`` (1%).
    """
    # pg_relation_size is the dominant cost on big catalogs; compute
    # it once in a CTE and reuse rather than calling it twice (filter
    # + projection).
    query = (
        "WITH sized AS ("
        "  SELECT "
        "    si.schemaname, "
        "    si.relname AS table_name, "
        "    si.indexrelname AS index_name, "
        "    si.indexrelid, "
        "    si.relid, "
        "    COALESCE(si.idx_scan, 0) AS idx_scan, "
        "    COALESCE(si.idx_tup_fetch, 0) AS idx_tup_fetch, "
        "    COALESCE(pg_relation_size(si.indexrelid), 0) AS size_bytes "
        "  FROM pg_stat_user_indexes si "
        ") "
        "SELECT "
        "  si.schemaname, "
        "  si.table_name, "
        "  si.index_name, "
        "  si.idx_scan, "
        "  si.idx_tup_fetch, "
        "  si.size_bytes, "
        "  pg_get_indexdef(si.indexrelid) AS definition, "
        "  COALESCE(st.seq_scan, 0) AS table_seq_scan, "
        "  COALESCE(st.idx_scan, 0) AS table_idx_scan "
        "FROM sized si "
        "JOIN pg_index ix ON ix.indexrelid = si.indexrelid "
        "JOIN pg_stat_user_tables st ON st.relid = si.relid "
        # Exclude indexes that back integrity constraints — dropping
        # those would be a schema change, not a performance win.
        "WHERE NOT ix.indisprimary "
        "  AND NOT ix.indisunique "
        "  AND NOT ix.indisexclusion "
        "  AND si.size_bytes >= %s "
    )
    params: list[object] = [min_index_size_bytes]
    if schema is not None:
        query += " AND si.schemaname = %s "
        params.append(schema)
    query += " ORDER BY si.schemaname, si.relname, si.indexrelname"

    rows = await driver.execute_query(query, params=params, force_readonly=True)

    candidates: list[IndexDropCandidate] = []
    for row in rows or []:
        idx_scan = int(row.cells["idx_scan"])
        idx_tup_fetch = int(row.cells["idx_tup_fetch"])
        size_bytes = int(row.cells["size_bytes"])
        table_seq_scan = int(row.cells["table_seq_scan"])
        table_idx_scan = int(row.cells["table_idx_scan"])
        schema_name = str(row.cells["schemaname"])
        table_name = str(row.cells["table_name"])
        index_name = str(row.cells["index_name"])
        definition = str(row.cells["definition"])

        reason_code, rationale = _classify_drop_candidate(
            idx_scan=idx_scan,
            idx_tup_fetch=idx_tup_fetch,
            size_bytes=size_bytes,
            table_total_scans=table_seq_scan + table_idx_scan,
            low_scan_ratio=low_scan_ratio,
        )
        if reason_code is None:
            continue

        # CONCURRENTLY is the right default — drop without blocking
        # reads. Cannot run inside a transaction, which the operator's
        # tooling has to handle. Identifiers come from
        # ``pg_stat_user_indexes`` so they're real names, but a
        # ``CREATE INDEX "weird""name"`` is legal Postgres syntax — a
        # literal ``"`` in the identifier would break the emitted SQL
        # without the standard double-double-quote escape.
        schema_quoted = '"' + schema_name.replace('"', '""') + '"'
        index_quoted = '"' + index_name.replace('"', '""') + '"'
        drop_sql = f"DROP INDEX CONCURRENTLY {schema_quoted}.{index_quoted};"
        candidates.append(
            IndexDropCandidate(
                schema=schema_name,
                table=table_name,
                index=index_name,
                size_bytes=size_bytes,
                idx_scan=idx_scan,
                idx_tup_fetch=idx_tup_fetch,
                table_seq_scan=table_seq_scan,
                table_idx_scan=table_idx_scan,
                reason_code=reason_code,
                rationale=rationale,
                drop_sql=drop_sql,
                definition=definition,
            )
        )

    # Sort by reason strength (never_used first) then size descending
    # so the report leads with the highest-confidence + highest-
    # impact drops.
    _strength = {
        DROP_REASON_NEVER_USED: 0,
        DROP_REASON_SCAN_NO_FETCH: 1,
        DROP_REASON_RARELY_USED: 2,
    }
    candidates.sort(key=lambda c: (_strength[c.reason_code], -c.size_bytes))
    return candidates


def _classify_drop_candidate(
    *,
    idx_scan: int,
    idx_tup_fetch: int,
    size_bytes: int,
    table_total_scans: int,
    low_scan_ratio: float,
) -> tuple[str, str] | tuple[None, str]:
    """Return ``(reason_code, rationale)`` or ``(None, "")`` to skip."""
    if idx_scan == 0:
        return (
            DROP_REASON_NEVER_USED,
            f"index has never been scanned since the stats counter last reset; "
            f"reclaim {size_bytes / 1024 / 1024:.1f} MiB and the write-amplification "
            "tax with no read-side cost.",
        )
    if idx_tup_fetch == 0:
        return (
            DROP_REASON_SCAN_NO_FETCH,
            f"index has been scanned {idx_scan:,} times but returned zero rows — "
            "likely an existence-check pattern. A partial index or removing the "
            "index entirely is usually cheaper.",
        )
    if table_total_scans > 0 and idx_scan < low_scan_ratio * table_total_scans:
        return (
            DROP_REASON_RARELY_USED,
            f"index has been scanned only {idx_scan:,} times against a table with "
            f"{table_total_scans:,} total scans (below the {low_scan_ratio:.1%} "
            "threshold); weigh the disk + write-amplification cost against the "
            "marginal read benefit.",
        )
    return (None, "")

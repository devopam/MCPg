"""pg_turboquant integration: observability + advisor + write + DDL + query.

`pg_turboquant <https://github.com/mayflower/pg_turboquant>`_ is a
PostgreSQL extension providing a custom ANN index access method
(``USING turboquant``) over pgvector ``vector`` / ``halfvec`` columns.
This module covers the full extension surface — read, advise,
maintain, build, and query:

* :func:`list_turboquant_indexes`, :func:`get_turboquant_index_metadata`,
  :func:`get_turboquant_heap_stats`, :func:`get_turboquant_last_scan_stats`
  — observability.
* :func:`recommend_turboquant_maintenance`, :func:`audit_turboquant_indexes`
  — rule-table advisor + scorecard category adapter.
* :func:`maintain_turboquant_index` — wraps ``tq_maintain_index``;
  pre-flights that the named index is actually a turboquant index so
  the call can't be turned into a way to probe arbitrary catalogs.
* :func:`create_turboquant_index`, :func:`reindex_turboquant_index`
  — DDL.
* :func:`turboquant_approx_candidates`,
  :func:`turboquant_rerank_candidates`,
  :func:`recommend_turboquant_query_knobs` — query-execution and
  per-query knob advisor. Added in TQ-post-investigation after the
  upstream SQL definitions were read directly from
  ``sql/pg_turboquant--0.1.0.sql``; argument types and return
  shapes are taken verbatim, not inferred.

All read functions return cleanly (empty list / ``None``) when the
extension is not installed, so callers can treat absence as "no
turboquant in use" rather than a hard error.

**Upstream contract notes.** The TQ-field-alignment pass removed
seven fields the original TQ-1 implementation had inherited from
README prose (``algorithm_version``, ``quantizer_family``,
``residual_sketch_kind``, ``fast_path_eligible``,
``capability_flags``, ``delta_state``,
``maintenance_recommended``) along with three rules that depended
on them (``format_v1_reindex_needed``, ``maintenance_due``,
``fast_path_ineligible``). None of those keys appear in upstream's
actual JSON payload (per ``src/tq_extension.c`` +
``sql/pg_turboquant--0.1.0.sql``), so the fields would have always
returned ``None`` and the rules would have never fired against a
real install. The replacement fields (``access_method``,
``opclass``, ``input_type``, ``heap_relation``,
``heap_live_rows_estimate``, ``capabilities``, ``operability``,
the ``delta_*`` set, and ``delta_merge_thresholds``) are sourced
from verified upstream keys; the remaining advisor rules
(``prerequisites_unmet``, ``delta_tier_large``) source from
verified signals. ``raw_metadata`` always carries the full payload
for callers needing fidelity to keys not surfaced as typed fields.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mcpg.database import Database
from mcpg.extensions import extension_installed
from mcpg.sql import SqlDriver

# Plain unquoted PostgreSQL identifier — matches the rule used by
# vector_tuning. Anything that would require delimited quoting at the
# catalog level is refused rather than parsed out of an agent string.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class TurboQuantError(Exception):
    """Raised when a pg_turboquant operation cannot complete."""


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise TurboQuantError(f"invalid {kind} name: {name!r}")


def _pg_quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier the way ``format('%I')`` would.

    Wraps ``name`` in double quotes and doubles any embedded ``"``.
    Used for suggested-action SQL where the schema / index names come
    from the catalog (mixed-case and special characters are legal in
    PG via delimited identifiers).
    """
    return '"' + name.replace('"', '""') + '"'


def _pg_quote_literal(text: str) -> str:
    """Quote a PostgreSQL string literal the way ``format('%L')`` would.

    Wraps ``text`` in single quotes and doubles any embedded ``'``.
    """
    return "'" + text.replace("'", "''") + "'"


@dataclass(frozen=True)
class TurboQuantIndexInfo:
    """A turboquant index and the metadata `tq_index_metadata` reports for it.

    Every typed field on this class is sourced from a key that has
    been verified in upstream's source
    (`sql/pg_turboquant--0.1.0.sql` + `src/tq_extension.c`):

    * Catalog-level: :attr:`schema`, :attr:`index`, :attr:`table`,
      :attr:`column`.
    * `tq_index_metadata` top-level: :attr:`access_method`,
      :attr:`opclass`, :attr:`input_type`, :attr:`heap_relation`,
      :attr:`heap_live_rows_estimate`, :attr:`capabilities`,
      :attr:`operability`.
    * Delta-tier counters: :attr:`delta_enabled`,
      :attr:`delta_live_count`, :attr:`delta_batch_page_count`,
      :attr:`delta_head_block`, :attr:`delta_tail_block`.
    * `delta_health` sub-object: :attr:`delta_page_depth`,
      :attr:`delta_live_fraction`, :attr:`delta_merge_recommended`,
      :attr:`delta_merge_thresholds`. :attr:`delta_merge_recommended`
      powers the ``delta_tier_large`` advisor rule.

    :attr:`raw_metadata` always carries the full ``tq_index_metadata``
    payload so callers can reach anything not surfaced as a typed
    field. :attr:`index_options` is sourced separately from
    ``pg_class.reloptions`` and parsed into typed values
    (``bits``, ``lists`` as ints, ``normalized`` as bool,
    ``transform`` as str).

    *Note on the alignment.* The original TQ-1 dataclass exposed
    fields named after README prose (``algorithm_version``,
    ``quantizer_family``, ``residual_sketch_kind``,
    ``fast_path_eligible``, ``capability_flags``, ``delta_state``,
    ``maintenance_recommended``) — those key names don't appear in
    upstream's actual JSON output. The TQ-field-alignment PR removed
    them. Callers needing equivalent information should read
    :attr:`capabilities` / :attr:`operability` / :attr:`raw_metadata`
    until upstream documents what's in those sub-objects.
    """

    schema: str
    index: str
    table: str
    column: str
    access_method: str | None = None
    opclass: str | None = None
    input_type: str | None = None
    heap_relation: str | None = None
    heap_live_rows_estimate: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    operability: dict[str, Any] = field(default_factory=dict)
    delta_enabled: bool | None = None
    delta_live_count: int | None = None
    delta_batch_page_count: int | None = None
    delta_head_block: int | None = None
    delta_tail_block: int | None = None
    delta_page_depth: int | None = None
    delta_live_fraction: float | None = None
    delta_merge_recommended: bool | None = None
    delta_merge_thresholds: dict[str, Any] = field(default_factory=dict)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    index_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurboQuantHeapStats:
    """Exact heap row count for a turboquant index."""

    schema: str
    index: str
    row_count: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurboQuantLastScanStats:
    """The most recent scan's diagnostic JSON, parsed defensively.

    ``raw`` always holds the full upstream payload; the named fields
    are convenience extractions for the documented keys.
    """

    raw: dict[str, Any]
    score_mode: str | None
    simd_kernel: str | None
    pages_scanned: int | None
    pages_pruned: int | None


# Metric → upstream operator class. Single source of truth used by
# the DDL builder; surfaced by name here so external consumers (and
# future per-query knob helpers) can rely on the same mapping.
_TQ_OPS_FOR_METRIC: dict[str, str] = {
    "cosine": "tq_cosine_ops",
    "inner_product": "tq_inner_product_ops",
    "l2": "tq_l2_ops",
}

# `transform` allowlist. README only documents ``hadamard`` as a
# user-facing value — we refuse anything else rather than guess.
# Omitting the option entirely (the function's default) lets upstream
# apply its own default, which is what we want when the caller hasn't
# stated a preference.
_VALID_TRANSFORMS: frozenset[str] = frozenset({"hadamard"})

# Safety bounds — these are guards, not claims about what upstream
# accepts. The README documents the option names but not valid value
# ranges; passing through user-supplied values within these bounds
# lets upstream reject anything genuinely invalid with its own
# (informative) error message.
_BITS_MIN, _BITS_MAX = 1, 64
_LISTS_MIN, _LISTS_MAX = 0, 1_000_000


@dataclass(frozen=True)
class CreateIndexResult:
    """Outcome of a :func:`create_turboquant_index` call.

    The rendered ``create_sql`` is included for auditability — every
    identifier in it has already been passed through
    :func:`_pg_quote_ident`, and every option value through the bounds
    or allowlist checks above.
    """

    schema: str
    table: str
    column: str
    index_name: str
    metric: str
    options: dict[str, Any]
    concurrently: bool
    create_sql: str
    started_at: str
    completed_at: str
    duration_seconds: float


@dataclass(frozen=True)
class ReindexResult:
    """Outcome of a :func:`reindex_turboquant_index` call."""

    schema: str
    index: str
    concurrently: bool
    reindex_sql: str
    started_at: str
    completed_at: str
    duration_seconds: float


@dataclass(frozen=True)
class MaintenanceResult:
    """Outcome of a :func:`maintain_turboquant_index` call.

    Client-side observables (timestamps + elapsed wall time) plus the
    upstream return JSON parsed into typed fields. The documented
    keys (per `src/tq_maintenance.h`) are
    :attr:`delta_merge_performed`, :attr:`merged_delta_count`, and
    :attr:`recycled_delta_page_count`; all are parsed defensively
    (``None`` when absent) and the full raw payload is preserved in
    :attr:`raw` so any additional fields upstream adds stay
    accessible.
    """

    schema: str
    index: str
    started_at: str  # ISO 8601 UTC
    completed_at: str  # ISO 8601 UTC
    duration_seconds: float
    delta_merge_performed: bool | None = None
    merged_delta_count: int | None = None
    recycled_delta_page_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# --- SQL --------------------------------------------------------------------

# Joining catalog tables in one trip is cheaper than walking indexes and
# fetching metadata one-by-one, so we splice the regclass argument from
# the catalog row itself.
_LIST_INDEXES_SQL = """
SELECT
    n.nspname                                  AS schema,
    i.relname                                  AS index,
    t.relname                                  AS table,
    a.attname                                  AS column,
    i.reloptions                               AS reloptions,
    tq_index_metadata(i.oid::regclass)::jsonb  AS metadata
FROM pg_index ix
JOIN pg_class i           ON i.oid = ix.indexrelid
JOIN pg_class t           ON t.oid = ix.indrelid
JOIN pg_namespace n       ON n.oid = i.relnamespace
JOIN pg_am am             ON am.oid = i.relam
LEFT JOIN pg_attribute a  ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE am.amname = 'turboquant'
ORDER BY n.nspname, i.relname
"""

_FETCH_ONE_INDEX_SQL = """
SELECT
    n.nspname                                  AS schema,
    i.relname                                  AS index,
    t.relname                                  AS table,
    a.attname                                  AS column,
    i.reloptions                               AS reloptions,
    tq_index_metadata(i.oid::regclass)::jsonb  AS metadata
FROM pg_index ix
JOIN pg_class i           ON i.oid = ix.indexrelid
JOIN pg_class t           ON t.oid = ix.indrelid
JOIN pg_namespace n       ON n.oid = i.relnamespace
JOIN pg_am am             ON am.oid = i.relam
LEFT JOIN pg_attribute a  ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE am.amname = 'turboquant' AND n.nspname = %s AND i.relname = %s
"""

_HEAP_STATS_SQL = """
SELECT tq_index_heap_stats(format('%I.%I', %s, %s)::regclass)::jsonb AS stats
"""

_LAST_SCAN_SQL = "SELECT tq_last_scan_stats()::jsonb AS stats"


# --- helpers ---------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a JSONB-shaped value to a plain dict.

    psycopg returns JSONB as a parsed Python value; protect against
    drivers that hand back the raw text by being lenient here.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _coerce_reloption_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _parse_reloptions(raw: Any) -> dict[str, Any]:
    """Parse a ``pg_class.reloptions`` text[] into a typed dict.

    PG stores reloptions as a text[] of ``key=value`` strings.
    ``bits`` / ``lists`` come back as ints, ``normalized`` as bool,
    everything else (e.g. ``transform``) as the raw string. Unknown
    or malformed entries are skipped rather than rejected so a future
    upstream option doesn't fail catalog reads.
    """
    if not isinstance(raw, list):
        return {}
    parsed: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, _, value = item.partition("=")
        if not key:
            continue
        parsed[key] = _coerce_reloption_value(value)
    return parsed


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_bool(value: Any) -> bool | None:
    """Strict bool reader — only ``True`` / ``False`` survive.

    Reject everything else (``0``, ``1``, ``"true"``, ``None`` …) so
    rules that key on ``is True`` / ``is False`` never get tricked by
    truthiness.
    """
    return value if isinstance(value, bool) else None


def _index_info_from_row(row_cells: dict[str, Any]) -> TurboQuantIndexInfo:
    metadata = _as_dict(row_cells.get("metadata"))
    # ``delta_health`` is a nested object documented in upstream's C
    # source as ``{ page_depth, live_fraction, merge_recommended,
    # merge_thresholds }``. Read it defensively — a missing or
    # mis-typed sub-object yields ``None`` / empty dict rather than
    # raising, so this code keeps working across upstream schema
    # drift.
    delta_health = _as_dict(metadata.get("delta_health"))
    return TurboQuantIndexInfo(
        schema=row_cells["schema"],
        index=row_cells["index"],
        table=row_cells["table"],
        column=row_cells.get("column") or "",
        access_method=metadata.get("access_method"),
        opclass=metadata.get("opclass"),
        input_type=metadata.get("input_type"),
        heap_relation=metadata.get("heap_relation"),
        heap_live_rows_estimate=_maybe_int(metadata.get("heap_live_rows_estimate")),
        capabilities=_as_dict(metadata.get("capabilities")),
        operability=_as_dict(metadata.get("operability")),
        delta_enabled=_maybe_bool(metadata.get("delta_enabled")),
        delta_live_count=_maybe_int(metadata.get("delta_live_count")),
        delta_batch_page_count=_maybe_int(metadata.get("delta_batch_page_count")),
        delta_head_block=_maybe_int(metadata.get("delta_head_block")),
        delta_tail_block=_maybe_int(metadata.get("delta_tail_block")),
        delta_page_depth=_maybe_int(delta_health.get("page_depth")),
        delta_live_fraction=_maybe_float(delta_health.get("live_fraction")),
        delta_merge_recommended=_maybe_bool(delta_health.get("merge_recommended")),
        delta_merge_thresholds=_as_dict(delta_health.get("merge_thresholds")),
        raw_metadata=metadata,
        index_options=_parse_reloptions(row_cells.get("reloptions")),
    )


# --- public API ------------------------------------------------------------


async def list_turboquant_indexes(driver: SqlDriver) -> list[TurboQuantIndexInfo]:
    """List every turboquant index plus its `tq_index_metadata` payload.

    Returns an empty list when the extension is not installed.
    """
    if not await extension_installed(driver, "pg_turboquant"):
        return []
    rows = await driver.execute_query(_LIST_INDEXES_SQL, force_readonly=True)
    return [_index_info_from_row(row.cells) for row in rows or []]


async def get_turboquant_index_metadata(driver: SqlDriver, schema: str, index: str) -> TurboQuantIndexInfo:
    """Fetch the metadata payload for a single turboquant index.

    Identifier validation (``_IDENTIFIER``) runs before any SQL is built,
    so the schema / index strings cannot drive arbitrary catalog lookups.

    Raises:
        TurboQuantError: extension is not installed, the schema / index
            name is not a plain identifier, or no turboquant index with
            that name exists.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")
    rows = await driver.execute_query(_FETCH_ONE_INDEX_SQL, params=[schema, index], force_readonly=True)
    if not rows:
        raise TurboQuantError(f"no turboquant index named {schema}.{index} found")
    return _index_info_from_row(rows[0].cells)


async def get_turboquant_heap_stats(driver: SqlDriver, schema: str, index: str) -> TurboQuantHeapStats:
    """Fetch the exact heap row count for a single turboquant index.

    Raises:
        TurboQuantError: extension is not installed or the identifier
            fails validation.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")
    rows = await driver.execute_query(_HEAP_STATS_SQL, params=[schema, index], force_readonly=True)
    if not rows:
        raise TurboQuantError(f"tq_index_heap_stats returned no row for {schema}.{index}")
    stats = _as_dict(rows[0].cells.get("stats"))
    row_count = stats.get("row_count")
    if row_count is None:
        # Some upstream versions report 'rows' instead — fall back rather
        # than fail when the alternate key is the only one present.
        row_count = stats.get("rows")
    return TurboQuantHeapStats(
        schema=schema,
        index=index,
        row_count=int(row_count) if row_count is not None else 0,
        raw=stats,
    )


# --- advisor + audit -------------------------------------------------------


@dataclass(frozen=True)
class TurboQuantAdvisorFinding:
    """A single rule-table hit produced by :func:`recommend_turboquant_maintenance`.

    ``code`` is the stable identifier — ``severity`` and the human-
    readable ``evidence`` / ``suggested_action`` may evolve, but ``code``
    is the contract callers script against. ``schema`` / ``index`` are
    empty strings for cluster-level findings like ``prerequisites_unmet``
    that don't attach to a specific index.
    """

    code: str
    severity: str  # GOOD / WARNING / CRITICAL
    schema: str
    index: str
    evidence: str
    suggested_action: str


# Rule codes — stable identifiers. Every rule's underlying signal is
# sourced from a verified key in upstream's source. Three earlier
# rules (``format_v1_reindex_needed``, ``maintenance_due``,
# ``fast_path_ineligible``) were removed in the TQ-field-alignment
# pass when their source fields turned out to be README prose, not
# actual upstream keys — see the module docstring for context.
_RULE_PREREQUISITES_UNMET = "prerequisites_unmet"
_RULE_DELTA_TIER_LARGE = "delta_tier_large"


def _finding_delta_tier_large(info: TurboQuantIndexInfo) -> TurboQuantAdvisorFinding | None:
    # Trust upstream's own advisory. ``delta_health.merge_recommended``
    # is computed inside the extension against thresholds it owns, so
    # MCPg doesn't need to invent a ratio of its own. Only fires on
    # explicit ``True`` — ``None`` (not reported) is treated as
    # absence of information.
    if info.delta_merge_recommended is not True:
        return None
    rows = info.delta_live_count if info.delta_live_count is not None else "unknown"
    pages = info.delta_batch_page_count if info.delta_batch_page_count is not None else "unknown"
    qualified = f"{_pg_quote_ident(info.schema)}.{_pg_quote_ident(info.index)}"
    literal = _pg_quote_literal(qualified)
    return TurboQuantAdvisorFinding(
        code=_RULE_DELTA_TIER_LARGE,
        severity="WARNING",
        schema=info.schema,
        index=info.index,
        evidence=(
            f"tq_index_metadata reports delta_health.merge_recommended=true "
            f"(delta_live_count={rows}, delta_batch_page_count={pages})."
        ),
        suggested_action=f"SELECT tq_maintain_index({literal}::regclass);",
    )


_PER_INDEX_RULES = (_finding_delta_tier_large,)


async def recommend_turboquant_maintenance(driver: SqlDriver) -> list[TurboQuantAdvisorFinding]:
    """Walk every turboquant index and emit advisor findings.

    Returns an empty list when the extension is not installed (so the
    surface composes the same way as :func:`list_turboquant_indexes`).
    The cluster-level rule ``prerequisites_unmet`` fires when
    pg_turboquant is installed but its hard dependency (pgvector) is
    not — without pgvector, no turboquant index can be created or
    queried, so the finding short-circuits before any per-index work.
    """
    if not await extension_installed(driver, "pg_turboquant"):
        return []

    findings: list[TurboQuantAdvisorFinding] = []

    if not await extension_installed(driver, "vector"):
        findings.append(
            TurboQuantAdvisorFinding(
                code=_RULE_PREREQUISITES_UNMET,
                severity="CRITICAL",
                schema="",
                index="",
                evidence=(
                    "pg_turboquant is installed but its hard dependency (pgvector / the ``vector`` extension) "
                    "is not. Every turboquant index requires pgvector at CREATE INDEX time and at query time."
                ),
                suggested_action='CREATE EXTENSION IF NOT EXISTS "vector";',
            )
        )
        # Skip per-index walking — without pgvector there can't be any
        # working turboquant indexes anyway, and any catalog rows would
        # produce noise rather than signal.
        return findings

    for info in await list_turboquant_indexes(driver):
        for rule in _PER_INDEX_RULES:
            if (finding := rule(info)) is not None:
                findings.append(finding)

    return findings


# Score deductions by severity — single source of truth for both the
# adapter below and any external consumers.
_SEVERITY_DEDUCTION = {"CRITICAL": 30, "WARNING": 15, "GOOD": 0}


# Lazily-imported audit types kept out of the module-level imports to
# avoid a circular import: ``audit`` ultimately re-exports tools that
# may pull in this module. The adapter lives here (not in audit.py) so
# the rule-table contract stays in one file.
def _adapt_finding_to_metric(finding: TurboQuantAdvisorFinding) -> Any:
    from mcpg.audit import MetricResult

    target = finding.index or "(cluster)"
    return MetricResult(
        name=f"turboquant:{finding.code} on {finding.schema}.{target}"
        if finding.index
        else f"turboquant:{finding.code}",
        value=finding.code,
        unit="finding",
        target="no findings",
        status=finding.severity,
        severity=3 if finding.severity == "CRITICAL" else 2 if finding.severity == "WARNING" else 0,
        evidence=finding.evidence,
        suggestion=finding.suggested_action,
    )


async def audit_turboquant_indexes(driver: SqlDriver) -> Any:
    """Scorecard adapter — returns a CategoryResult or None.

    Returns ``None`` when pg_turboquant is not installed so
    :func:`audit.audit_database` cleanly omits the category for
    deployments that don't use the extension. Otherwise produces a
    CategoryResult whose metrics are the advisor findings, with the
    standard 100-point-down scoring.
    """
    from mcpg.audit import CategoryResult

    if not await extension_installed(driver, "pg_turboquant"):
        return None

    findings = await recommend_turboquant_maintenance(driver)

    score = 100
    metrics = []
    for finding in findings:
        score -= _SEVERITY_DEDUCTION.get(finding.severity, 0)
        metrics.append(_adapt_finding_to_metric(finding))

    score = max(0, score)
    status_label = "GOOD" if score >= 90 else ("WARNING" if score >= 70 else "CRITICAL")

    if not metrics:
        # No findings → emit a single GOOD baseline metric so the
        # scorecard surfaces "category checked, all good" rather than
        # an empty list that looks like the category didn't run.
        from mcpg.audit import MetricResult

        metrics.append(
            MetricResult(
                name="turboquant:no_findings",
                value="ok",
                unit="finding",
                target="no findings",
                status="GOOD",
                severity=0,
                evidence="All turboquant indexes pass the advisor rules.",
                suggestion="",
            )
        )

    return CategoryResult(
        category="pg_turboquant Indexes",
        status=status_label,
        score=score,
        metrics=metrics,
    )


async def get_turboquant_last_scan_stats(driver: SqlDriver) -> TurboQuantLastScanStats | None:
    """Return the backend-local diagnostic JSON for the most recent scan.

    Returns ``None`` when the extension is absent or no turboquant scan
    has run on this backend yet (upstream returns SQL ``NULL`` in that
    case).
    """
    if not await extension_installed(driver, "pg_turboquant"):
        return None
    rows = await driver.execute_query(_LAST_SCAN_SQL, force_readonly=True)
    if not rows:
        return None
    raw = _as_dict(rows[0].cells.get("stats"))
    if not raw:
        return None
    pages_scanned = raw.get("pages_scanned")
    pages_pruned = raw.get("pages_pruned")
    return TurboQuantLastScanStats(
        raw=raw,
        score_mode=raw.get("score_mode"),
        simd_kernel=raw.get("simd_kernel"),
        pages_scanned=int(pages_scanned) if pages_scanned is not None else None,
        pages_pruned=int(pages_pruned) if pages_pruned is not None else None,
    )


# Pre-flight: confirm the named index actually uses the turboquant
# access method before calling tq_maintain_index. Without this check,
# upstream would raise a generic error on any non-turboquant index —
# and the error text would leak a small amount of catalog information.
_ASSERT_IS_TURBOQUANT_SQL = """
SELECT 1
FROM pg_index ix
JOIN pg_class i  ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE am.amname = 'turboquant' AND n.nspname = %s AND i.relname = %s
"""

# Identifier quoting happens PG-side via format('%I.%I')::regclass,
# so the bound params can't escape into SQL syntax even before the
# preflight check above runs.
_MAINTAIN_INDEX_SQL = "SELECT tq_maintain_index(format('%I.%I', %s, %s)::regclass)::jsonb AS result"


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


async def maintain_turboquant_index(driver: SqlDriver, schema: str, index: str) -> MaintenanceResult:
    """Run ``tq_maintain_index`` on a single turboquant index.

    The call delegates the actual delta-tier merge / compaction work
    to upstream; this wrapper handles validation, the pre-flight
    catalog check, client-side wall-time measurement, and defensive
    parsing of the upstream return JSON. Documented keys
    (:attr:`MaintenanceResult.delta_merge_performed`,
    :attr:`MaintenanceResult.merged_delta_count`,
    :attr:`MaintenanceResult.recycled_delta_page_count`) are surfaced
    as typed fields; the full payload is preserved in
    :attr:`MaintenanceResult.raw` so any future-added keys remain
    accessible without a code change.

    Raises:
        TurboQuantError: extension is not installed, the identifier
            fails validation, or the named index is not a turboquant
            index.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    preflight = await driver.execute_query(_ASSERT_IS_TURBOQUANT_SQL, params=[schema, index], force_readonly=True)
    if not preflight:
        raise TurboQuantError(
            f"index {schema}.{index} is not a turboquant index (or does not exist); refusing to call tq_maintain_index"
        )

    started_at = _utc_iso_now()
    started_mono = time.monotonic()
    rows = await driver.execute_query(_MAINTAIN_INDEX_SQL, params=[schema, index], force_readonly=False)
    duration = time.monotonic() - started_mono
    completed_at = _utc_iso_now()

    raw = _as_dict(rows[0].cells.get("result")) if rows else {}
    merge_performed = raw.get("delta_merge_performed")
    return MaintenanceResult(
        schema=schema,
        index=index,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 6),
        delta_merge_performed=merge_performed if isinstance(merge_performed, bool) else None,
        merged_delta_count=_maybe_int(raw.get("merged_delta_count")),
        recycled_delta_page_count=_maybe_int(raw.get("recycled_delta_page_count")),
        raw=raw,
    )


# --- DDL --------------------------------------------------------------------


def _validate_metric(metric: str) -> None:
    if metric not in _TQ_OPS_FOR_METRIC:
        expected = ", ".join(sorted(_TQ_OPS_FOR_METRIC))
        raise TurboQuantError(f"unsupported metric {metric!r}; expected one of {expected}")


def _validate_int_option(name: str, value: int | None, lo: int, hi: int) -> None:
    """Validate a bounded integer DDL option.

    Bools are explicitly rejected even though ``bool`` is a subclass
    of ``int`` — accepting ``True`` as ``bits=1`` would silently
    coerce a typed argument that almost certainly indicates a caller
    bug.
    """
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise TurboQuantError(f"{name} must be an int in [{lo}..{hi}]; got {value!r}")


def _validate_transform(transform: str | None) -> None:
    if transform is None:
        return
    if transform not in _VALID_TRANSFORMS:
        expected = ", ".join(sorted(_VALID_TRANSFORMS))
        raise TurboQuantError(
            f"unsupported transform {transform!r}; expected one of {{{expected}}}. "
            "Omit the argument to let upstream apply its default."
        )


def _validate_bool(value: bool | None, kind: str) -> None:
    if value is None:
        return
    if not isinstance(value, bool):
        raise TurboQuantError(f"{kind} must be a bool; got {value!r}")


def _build_with_clause(
    *, bits: int | None, lists: int | None, transform: str | None, normalized: bool | None
) -> tuple[str, dict[str, Any]]:
    """Render the ``WITH (...)`` tail of a CREATE INDEX statement.

    Returns the rendered clause (empty string when no options are set
    so the SQL stays uncluttered) plus a parallel dict for the result
    object's ``options`` field.
    """
    options: dict[str, Any] = {}
    parts: list[str] = []
    if bits is not None:
        options["bits"] = bits
        parts.append(f"bits = {bits}")
    if lists is not None:
        options["lists"] = lists
        parts.append(f"lists = {lists}")
    if transform is not None:
        options["transform"] = transform
        parts.append(f"transform = {_pg_quote_literal(transform)}")
    if normalized is not None:
        options["normalized"] = normalized
        parts.append(f"normalized = {'true' if normalized else 'false'}")
    clause = f" WITH ({', '.join(parts)})" if parts else ""
    return clause, options


async def create_turboquant_index(
    database: Database,
    schema: str,
    table: str,
    column: str,
    index_name: str,
    metric: str,
    *,
    bits: int | None = None,
    lists: int | None = None,
    transform: str | None = None,
    normalized: bool | None = None,
    concurrently: bool = True,
) -> CreateIndexResult:
    """Build and execute ``CREATE INDEX … USING turboquant``.

    ``index_name`` is mandatory — we don't query back PG's
    auto-generated name to avoid an extra round-trip and the
    catalog-shape assumptions that would come with it. ``metric``
    selects the upstream operator class through
    :data:`_TQ_OPS_FOR_METRIC`. Index options that aren't supplied
    are simply omitted from the ``WITH (...)`` clause so upstream's
    defaults apply.

    Identifier safety: every schema / table / column / index-name
    string goes through :func:`_pg_quote_ident`. ``transform`` (if
    supplied) goes through :func:`_pg_quote_literal`. ``bits`` /
    ``lists`` / ``normalized`` are validated to safe types and ranges
    before they reach SQL. The full rendered statement is preserved
    in :attr:`CreateIndexResult.create_sql` for auditability.

    The statement runs on an autocommit connection via
    :meth:`Database.run_unmanaged` because ``CREATE INDEX
    CONCURRENTLY`` cannot run inside a transaction block.

    Raises:
        TurboQuantError: extension is not installed, any identifier
            fails validation, any option fails its allowlist or bounds
            check, or the underlying DDL fails.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(column, "column")
    _validate_identifier(index_name, "index_name")
    _validate_metric(metric)
    _validate_int_option("bits", bits, _BITS_MIN, _BITS_MAX)
    _validate_int_option("lists", lists, _LISTS_MIN, _LISTS_MAX)
    _validate_transform(transform)
    _validate_bool(normalized, "normalized")
    _validate_bool(concurrently, "concurrently")

    if not await extension_installed(database.driver(), "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    opclass = _TQ_OPS_FOR_METRIC[metric]
    with_clause, options = _build_with_clause(bits=bits, lists=lists, transform=transform, normalized=normalized)
    concurrently_clause = " CONCURRENTLY" if concurrently else ""
    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"

    sql = (
        f"CREATE INDEX{concurrently_clause} {_pg_quote_ident(index_name)} "
        f"ON {qualified_table} "
        f"USING turboquant ({_pg_quote_ident(column)} {opclass})"
        f"{with_clause}"
    )

    started_at = _utc_iso_now()
    started_mono = time.monotonic()
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        # Mirrors the pg_search pattern (pg_search.py:1265): catch the
        # raw driver / DatabaseError and re-raise as TurboQuantError so
        # callers always see a typed error class for DDL failures
        # (duplicate name, locked relation, missing table, etc.) rather
        # than a psycopg traceback leaking out of the wrapper.
        raise TurboQuantError(f"CREATE INDEX failed: {exc}") from exc
    duration = time.monotonic() - started_mono
    completed_at = _utc_iso_now()

    return CreateIndexResult(
        schema=schema,
        table=table,
        column=column,
        index_name=index_name,
        metric=metric,
        options=options,
        concurrently=concurrently,
        create_sql=sql,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 6),
    )


async def reindex_turboquant_index(
    database: Database,
    schema: str,
    index: str,
    *,
    concurrently: bool = True,
) -> ReindexResult:
    """Run ``REINDEX INDEX [CONCURRENTLY] schema.index``.

    Same pre-flight as :func:`maintain_turboquant_index`: confirm the
    named index is actually a turboquant index before running, so the
    call can't be turned into a way to probe arbitrary catalogs via
    PostgreSQL's error messages.

    Runs on an autocommit connection because ``REINDEX CONCURRENTLY``
    cannot run inside a transaction block.

    Raises:
        TurboQuantError: extension is not installed, identifier fails
            validation, or the named index is not a turboquant index.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    _validate_bool(concurrently, "concurrently")

    driver = database.driver()
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    preflight = await driver.execute_query(_ASSERT_IS_TURBOQUANT_SQL, params=[schema, index], force_readonly=True)
    if not preflight:
        raise TurboQuantError(
            f"index {schema}.{index} is not a turboquant index (or does not exist); refusing to REINDEX"
        )

    concurrently_clause = " CONCURRENTLY" if concurrently else ""
    qualified = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(index)}"
    sql = f"REINDEX INDEX{concurrently_clause} {qualified}"

    started_at = _utc_iso_now()
    started_mono = time.monotonic()
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        # Same pattern as create_turboquant_index above: convert raw
        # driver errors (lock contention, missing index, etc.) into a
        # typed TurboQuantError so callers don't get a psycopg
        # traceback bleeding out of the wrapper.
        raise TurboQuantError(f"REINDEX failed: {exc}") from exc
    duration = time.monotonic() - started_mono
    completed_at = _utc_iso_now()

    return ReindexResult(
        schema=schema,
        index=index,
        concurrently=concurrently,
        reindex_sql=sql,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 6),
    )


# --- TQ-5: query execution + per-query knobs --------------------------------
#
# Surface confirmed against `sql/pg_turboquant--0.1.0.sql` upstream:
# every function below has a fully-documented signature (arguments,
# types, return columns). The wrappers below are thin shells — they
# validate, build SQL with bind params, and unpack the typed return
# table. No upstream return shape is invented.

# Public-facing metric → upstream's runtime ``metric text`` token.
# Note: TQ-4's _TQ_OPS_FOR_METRIC uses the same public names but
# maps to opclass identifiers (``tq_*_ops``). The query functions
# use a different lexical domain (lowercase short codes) — both
# mappings live here as single sources of truth so an external
# rename only happens in one place.
_TQ_METRIC_TEXT_FOR_METRIC: dict[str, str] = {
    "cosine": "cosine",
    "inner_product": "ip",
    "l2": "l2",
}


@dataclass(frozen=True)
class TurboQuantCandidate:
    """A row from ``tq_approx_candidates``."""

    candidate_id: str
    approximate_rank: int
    approximate_distance: float


@dataclass(frozen=True)
class TurboQuantRerankedCandidate:
    """A row from ``tq_rerank_candidates``."""

    candidate_id: str
    approximate_rank: int
    approximate_distance: float
    exact_rank: int
    exact_distance: float


@dataclass(frozen=True)
class TurboQuantQueryKnobs:
    """Tuning knobs returned by ``tq_recommended_query_knobs``."""

    probes: int | None
    oversample_factor: int | None
    max_visited_codes: int | None
    max_visited_pages: int | None


def _tq_vector_literal(vec: list[float]) -> str:
    """Serialize a Python list to pgvector text format ``[v1,v2,...]``."""
    return "[" + ",".join(str(float(v)) for v in vec) + "]"


def _validate_query_metric(metric: str) -> None:
    if metric not in _TQ_METRIC_TEXT_FOR_METRIC:
        expected = ", ".join(sorted(_TQ_METRIC_TEXT_FOR_METRIC))
        raise TurboQuantError(f"unsupported metric {metric!r}; expected one of {expected}")


def _validate_positive_int(name: str, value: int, *, allow_zero: bool = False) -> None:
    """``candidate_limit`` / ``final_limit`` validation.

    Catches the bool-as-int subclass quirk the option helpers also
    guard against.
    """
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        op = ">=" if allow_zero else ">"
        raise TurboQuantError(f"{name} must be an int {op} 0; got {value!r}")


def _query_vector_to_param(query_vector: list[float] | str) -> str:
    """Coerce a list-of-floats or a pre-formatted text literal.

    Pre-formatted strings (e.g. ``"[1.0,2.0]"``) pass through; lists
    are serialized. The wrapper accepts both for parity with
    ``vector_search`` in :mod:`mcpg.vector_ops`.
    """
    if isinstance(query_vector, str):
        return query_vector
    if not isinstance(query_vector, list):
        raise TurboQuantError(f"query_vector must be list[float] or str; got {type(query_vector).__name__}")
    return _tq_vector_literal(query_vector)


# Filter-selectivity is documented as ``double precision`` with no
# stated range; we accept any float and let upstream reject anything
# truly invalid. Reject NaN / inf explicitly because those would be
# silently coerced by PG.
def _validate_filter_selectivity(value: float | None) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TurboQuantError(f"filter_selectivity must be a number; got {value!r}")
    fval = float(value)
    import math  # local import — only this validator needs it

    if math.isnan(fval) or math.isinf(fval):
        raise TurboQuantError(f"filter_selectivity must be finite; got {value!r}")


async def turboquant_approx_candidates(
    driver: SqlDriver,
    schema: str,
    table: str,
    id_column: str,
    embedding_column: str,
    query_vector: list[float] | str,
    metric: str,
    candidate_limit: int,
    *,
    probes: int | None = None,
    oversample_factor: int | None = None,
    half_precision: bool = False,
) -> list[TurboQuantCandidate]:
    """Wrap ``tq_approx_candidates`` — approximate retrieval, no rerank.

    ``half_precision`` selects between the ``vector`` (default) and
    ``halfvec`` overloads upstream provides. ``metric`` is the
    public-facing name (``cosine`` / ``inner_product`` / ``l2``) and
    is translated to upstream's runtime token (``cosine`` / ``ip`` /
    ``l2``) via :data:`_TQ_METRIC_TEXT_FOR_METRIC`.

    Raises:
        TurboQuantError: extension is not installed, any identifier
            fails validation, metric / limit / probe values fail
            their checks.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(id_column, "id_column")
    _validate_identifier(embedding_column, "embedding_column")
    _validate_query_metric(metric)
    _validate_positive_int("candidate_limit", candidate_limit)
    _validate_int_option("probes", probes, 1, 1_000_000)
    _validate_int_option("oversample_factor", oversample_factor, 1, 1_000_000)
    _validate_bool(half_precision, "half_precision")

    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    vector_type = "halfvec" if half_precision else "vector"
    sql = (
        "SELECT candidate_id, approximate_rank, approximate_distance "
        "FROM tq_approx_candidates("
        "format('%I.%I', %s, %s)::regclass, "
        "%s::name, %s::name, "
        f"%s::{vector_type}, "
        "%s, %s, %s, %s)"
    )
    rows = await driver.execute_query(
        sql,
        params=[
            schema,
            table,
            id_column,
            embedding_column,
            _query_vector_to_param(query_vector),
            _TQ_METRIC_TEXT_FOR_METRIC[metric],
            candidate_limit,
            probes,
            oversample_factor,
        ],
        force_readonly=True,
    )
    return [
        TurboQuantCandidate(
            candidate_id=str(row.cells["candidate_id"]),
            approximate_rank=int(row.cells["approximate_rank"]),
            approximate_distance=float(row.cells["approximate_distance"]),
        )
        for row in rows or []
    ]


async def turboquant_rerank_candidates(
    driver: SqlDriver,
    schema: str,
    table: str,
    id_column: str,
    embedding_column: str,
    query_vector: list[float] | str,
    metric: str,
    candidate_limit: int,
    final_limit: int,
    *,
    probes: int | None = None,
    oversample_factor: int | None = None,
    half_precision: bool = False,
) -> list[TurboQuantRerankedCandidate]:
    """Wrap ``tq_rerank_candidates`` — approximate retrieval + SQL-side exact rerank.

    Same validation surface as :func:`turboquant_approx_candidates`
    plus ``final_limit`` (the top-K to keep after the exact rerank).
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(id_column, "id_column")
    _validate_identifier(embedding_column, "embedding_column")
    _validate_query_metric(metric)
    _validate_positive_int("candidate_limit", candidate_limit)
    _validate_positive_int("final_limit", final_limit)
    _validate_int_option("probes", probes, 1, 1_000_000)
    _validate_int_option("oversample_factor", oversample_factor, 1, 1_000_000)
    _validate_bool(half_precision, "half_precision")

    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    vector_type = "halfvec" if half_precision else "vector"
    sql = (
        "SELECT candidate_id, approximate_rank, approximate_distance, exact_rank, exact_distance "
        "FROM tq_rerank_candidates("
        "format('%I.%I', %s, %s)::regclass, "
        "%s::name, %s::name, "
        f"%s::{vector_type}, "
        "%s, %s, %s, %s, %s)"
    )
    rows = await driver.execute_query(
        sql,
        params=[
            schema,
            table,
            id_column,
            embedding_column,
            _query_vector_to_param(query_vector),
            _TQ_METRIC_TEXT_FOR_METRIC[metric],
            candidate_limit,
            final_limit,
            probes,
            oversample_factor,
        ],
        force_readonly=True,
    )
    return [
        TurboQuantRerankedCandidate(
            candidate_id=str(row.cells["candidate_id"]),
            approximate_rank=int(row.cells["approximate_rank"]),
            approximate_distance=float(row.cells["approximate_distance"]),
            exact_rank=int(row.cells["exact_rank"]),
            exact_distance=float(row.cells["exact_distance"]),
        )
        for row in rows or []
    ]


async def recommend_turboquant_query_knobs(
    driver: SqlDriver,
    candidate_limit: int,
    *,
    final_limit: int | None = None,
    index_schema: str | None = None,
    index_name: str | None = None,
    filter_selectivity: float | None = None,
) -> TurboQuantQueryKnobs:
    """Wrap ``tq_recommended_query_knobs`` — per-query knob advisor.

    Two upstream overloads:

    * Plain: ``(candidate_limit, final_limit?)`` — no index context.
    * Index-aware: ``(indexed_index regclass, candidate_limit,
      final_limit?, filter_selectivity?)`` — recommendations
      specialised to one index's catalog state.

    The wrapper dispatches based on whether ``index_schema`` /
    ``index_name`` are supplied. Both or neither — supplying only one
    is rejected up front rather than producing a confusing PG error.
    ``filter_selectivity`` is only valid with an index.
    """
    _validate_positive_int("candidate_limit", candidate_limit)
    if final_limit is not None:
        _validate_positive_int("final_limit", final_limit)
    if (index_schema is None) != (index_name is None):
        raise TurboQuantError(
            "specify both index_schema and index_name, or neither — they identify a single regclass argument"
        )
    if filter_selectivity is not None and index_schema is None:
        raise TurboQuantError(
            "filter_selectivity only applies when index_schema / index_name are provided "
            "(it's an arg to the index-aware overload)"
        )
    if index_schema is not None and index_name is not None:
        _validate_identifier(index_schema, "index_schema")
        _validate_identifier(index_name, "index_name")
    _validate_filter_selectivity(filter_selectivity)

    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")

    if index_schema is None:
        sql = (
            "SELECT probes, oversample_factor, max_visited_codes, max_visited_pages "
            "FROM tq_recommended_query_knobs(%s, %s)"
        )
        params: list[Any] = [candidate_limit, final_limit]
    else:
        sql = (
            "SELECT probes, oversample_factor, max_visited_codes, max_visited_pages "
            "FROM tq_recommended_query_knobs(format('%I.%I', %s, %s)::regclass, %s, %s, %s)"
        )
        params = [index_schema, index_name, candidate_limit, final_limit, filter_selectivity]

    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    if not rows:
        # Upstream may return no row when there's nothing to suggest;
        # surface that as an all-None knob set rather than raising.
        return TurboQuantQueryKnobs(
            probes=None,
            oversample_factor=None,
            max_visited_codes=None,
            max_visited_pages=None,
        )
    cells = rows[0].cells
    return TurboQuantQueryKnobs(
        probes=_maybe_int(cells.get("probes")),
        oversample_factor=_maybe_int(cells.get("oversample_factor")),
        max_visited_codes=_maybe_int(cells.get("max_visited_codes")),
        max_visited_pages=_maybe_int(cells.get("max_visited_pages")),
    )

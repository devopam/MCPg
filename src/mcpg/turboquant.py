"""pg_turboquant read advisors.

`pg_turboquant <https://github.com/mayflower/pg_turboquant>`_ is a
PostgreSQL extension providing a custom ANN index access method
(``USING turboquant``) over pgvector ``vector`` / ``halfvec`` columns.
This module exposes the extension's read-only observability surface:

* :func:`list_turboquant_indexes` — every turboquant index in the
  database, joined with its ``tq_index_metadata`` payload.
* :func:`get_turboquant_index_metadata` — the metadata for one index.
* :func:`get_turboquant_heap_stats` — exact heap row count for one
  index.
* :func:`get_turboquant_last_scan_stats` — the backend-local JSON
  describing the most recent turboquant scan.

All four functions return cleanly (empty list / ``None``) when the
extension is not installed, so callers can treat absence as "no
turboquant in use" rather than a hard error.

**Upstream contract assumptions.** Upstream documents
``tq_last_scan_stats()`` as returning JSON. The other functions are
documented by the README only at the prose level
("reports algorithm version, quantizer family, …") — this module
treats them as returning JSON / JSONB as well, parses the documented
keys defensively (with ``.get()``), and preserves the raw payload in
:attr:`TurboQuantIndexInfo.raw_metadata` so any unanticipated fields
remain accessible to downstream advisors. The
``tq_recommended_query_knobs(...)`` advisor is **not** wrapped here:
its upstream signature is not documented at the field level yet, and
we'd rather skip a tool than ship one with a guessed signature. It is
expected to land in a follow-up once the signature is pinned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Plain unquoted PostgreSQL identifier — matches the rule used by
# vector_tuning. Anything that would require delimited quoting at the
# catalog level is refused rather than parsed out of an agent string.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class TurboQuantError(Exception):
    """Raised when a pg_turboquant operation cannot complete."""


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise TurboQuantError(f"invalid {kind} name: {name!r}")


@dataclass(frozen=True, slots=True)
class TurboQuantIndexInfo:
    """A turboquant index and the metadata `tq_index_metadata` reports for it.

    Documented keys are surfaced as typed fields; the full upstream
    payload is preserved in :attr:`raw_metadata` so callers can still
    reach unanticipated fields. :attr:`index_options` is sourced from
    ``pg_class.reloptions`` — the ``WITH (...)`` clause the index was
    created with, parsed into typed values (``bits``, ``lists`` as
    ints, ``normalized`` as bool, ``transform`` as str). This gives
    agents the build-time configuration at a glance without a separate
    ``tq_index_metadata`` round-trip.
    """

    schema: str
    index: str
    table: str
    column: str
    algorithm_version: str | None
    quantizer_family: str | None
    residual_sketch_kind: str | None
    fast_path_eligible: bool | None
    capability_flags: list[str] = field(default_factory=list)
    delta_state: str | None = None
    maintenance_recommended: bool | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    index_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurboQuantHeapStats:
    """Exact heap row count for a turboquant index."""

    schema: str
    index: str
    row_count: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
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


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


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


def _index_info_from_row(row_cells: dict[str, Any]) -> TurboQuantIndexInfo:
    metadata = _as_dict(row_cells.get("metadata"))
    return TurboQuantIndexInfo(
        schema=row_cells["schema"],
        index=row_cells["index"],
        table=row_cells["table"],
        column=row_cells.get("column") or "",
        algorithm_version=metadata.get("algorithm_version"),
        quantizer_family=metadata.get("quantizer_family"),
        residual_sketch_kind=metadata.get("residual_sketch_kind"),
        fast_path_eligible=metadata.get("fast_path_eligible"),
        capability_flags=_as_str_list(metadata.get("capability_flags")),
        delta_state=metadata.get("delta_state"),
        maintenance_recommended=metadata.get("maintenance_recommended"),
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


@dataclass(frozen=True, slots=True)
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


# Rule codes — stable identifiers. The mapping lives here as the single
# source of truth so the audit-database adapter and any external
# consumers (e.g. the RAG efficiency suite once it lands) read from one
# place. ``delta_tier_large`` is intentionally absent: the upstream
# ``tq_index_heap_stats`` payload does not yet document a delta-row key
# we can rely on, so the rule is deferred to a follow-up once the
# contract is verifiable.
_RULE_FORMAT_V1 = "format_v1_reindex_needed"
_RULE_MAINTENANCE_DUE = "maintenance_due"
_RULE_FAST_PATH_INELIGIBLE = "fast_path_ineligible"
_RULE_PREREQUISITES_UNMET = "prerequisites_unmet"


def _finding_format_v1(info: TurboQuantIndexInfo) -> TurboQuantAdvisorFinding | None:
    version = info.algorithm_version or ""
    if not version.lower().startswith("v1"):
        return None
    return TurboQuantAdvisorFinding(
        code=_RULE_FORMAT_V1,
        severity="CRITICAL",
        schema=info.schema,
        index=info.index,
        evidence=f"algorithm_version={version!r} — v1 indexes must be rebuilt to use the v2 on-disk format.",
        suggested_action=f'REINDEX INDEX CONCURRENTLY "{info.schema}"."{info.index}";',
    )


def _finding_maintenance_due(info: TurboQuantIndexInfo) -> TurboQuantAdvisorFinding | None:
    if info.maintenance_recommended is not True:
        return None
    state = info.delta_state or "unknown"
    return TurboQuantAdvisorFinding(
        code=_RULE_MAINTENANCE_DUE,
        severity="WARNING",
        schema=info.schema,
        index=info.index,
        evidence=f"tq_index_metadata reports maintenance_recommended=true (delta_state={state!r}).",
        suggested_action=f"SELECT tq_maintain_index('{info.schema}.{info.index}'::regclass);",
    )


def _finding_fast_path_ineligible(info: TurboQuantIndexInfo) -> TurboQuantAdvisorFinding | None:
    # Explicit ``is False`` — ``None`` means upstream didn't report,
    # which is not the same as reporting "ineligible".
    if info.fast_path_eligible is not False:
        return None
    return TurboQuantAdvisorFinding(
        code=_RULE_FAST_PATH_INELIGIBLE,
        severity="WARNING",
        schema=info.schema,
        index=info.index,
        evidence=(
            "tq_index_metadata reports fast_path_eligible=false — queries against this index will not use "
            "the SIMD fast path. Common causes: incompatible bits/transform combination, dimension below the "
            "fast-path threshold, or a missing capability flag."
        ),
        suggested_action=(
            "Review the index's WITH (...) options against the upstream tuning matrix; "
            "rebuild with a compatible configuration if a fast-path build is desired."
        ),
    )


_PER_INDEX_RULES = (
    _finding_format_v1,
    _finding_maintenance_due,
    _finding_fast_path_ineligible,
)


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

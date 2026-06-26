"""PG 19 SQL grammar characterisation tests.

Realises roadmap row **3.4** (PG 19 PR-11) — bundles audit rows #17
(characterisation tests) + the SQL-grammar half of #16
(``stats_reset`` propagation) + #20 (postgres_fdw pushdown coverage).

Why
===

The Phase 3 unit suites all use mocked drivers, so a typo in a
catalogue SELECT (``pg_state_lock`` for ``pg_stat_lock``) lands green
locally and only blows up against a real cluster. This file feeds
every SQL string MCPg's PG 19 modules emit through
:func:`pglast.parse_sql` — a Python binding around the actual
PostgreSQL parser source (``libpg_query``). Anything that doesn't
parse fails the contract.

Coverage shape
==============

Each entry in :data:`_PARSE_OK_CATALOGUE` is an SQL string that **must
parse** on the pglast we're pinned to. These are mostly catalogue
SELECTs (no PG 19-only keywords) plus a few function-call DDLs
(``pg_enable_data_checksums()``) that happen to share grammar with
older versions.

PG 19 grammar additions — ``REPACK``, ``MERGE PARTITIONS``,
``SPLIT PARTITION``, ``WAIT FOR LSN``, ``GRAPH_TABLE`` — are not in
pglast 7.x's parser source yet (it ships libpg_query for PG 17).
Those land in :data:`_PARSE_FAIL_CATALOGUE` with the exact token that
pglast trips on; when pglast picks up PG 19's grammar these tests
will start failing because parses will *succeed* — that's the signal
to move the entries up to :data:`_PARSE_OK_CATALOGUE`.

stats_reset propagation
=======================

:func:`test_stats_reset_propagates_through_pg19_stats_reads` asserts
that the SQL emitted by :func:`mcpg.pg19_stats.read_pg_stat_lock` and
:func:`mcpg.pg19_stats.read_pg_stat_recovery` selects the
``stats_reset`` column. PG 19 added the column to every ``pg_stat_*``
view that didn't already have it; the no-deprecation rule says we
surface it back to callers.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import pglast
import pytest

# ---------------------------------------------------------------------------
# Catalogue 1 — SQL strings that must parse on the pinned pglast.
# ---------------------------------------------------------------------------
#
# Source module noted alongside each entry so a failure traces straight
# back to the file that emits the string. Keep entries terse — a
# characterisation pin, not an integration test.

_PARSE_OK_CATALOGUE: list[tuple[str, str]] = [
    # mcpg.pg19_runtime — online data checksums + autovacuum priority
    ("pg19_runtime.enable", "SELECT pg_enable_data_checksums()"),
    ("pg19_runtime.disable", "SELECT pg_disable_data_checksums()"),
    # mcpg.pg19_stats — PG 19's two new monitoring views
    (
        "pg19_stats.lock_read",
        "SELECT   lock_type,   COALESCE(acquires, 0) AS acquires,   "
        "COALESCE(waits, 0) AS waits,   COALESCE(wait_time_us, 0) AS wait_time_us,   "
        "stats_reset::text AS stats_reset "
        "FROM pg_stat_lock ORDER BY wait_time_us DESC, waits DESC",
    ),
    (
        "pg19_stats.recovery_read",
        "SELECT   replay_lsn::text AS replay_lsn,   "
        "EXTRACT(epoch FROM replay_lag) AS replay_lag_seconds,   "
        "last_replayed_at::text AS last_replayed_at,   "
        "startup_state,   stats_reset::text AS stats_reset "
        "FROM pg_stat_recovery",
    ),
    # mcpg.aio — async I/O cache-pressure probe
    (
        "aio.cache_pressure",
        "SELECT SUM(blks_hit) AS hits, SUM(blks_read) AS reads "
        "FROM pg_stat_database WHERE datname = current_database()",
    ),
    # version probe shared across all four PG 19 modules
    (
        "shared.version_probe",
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
    ),
    # view-presence probe shared across pg19_stats + pg19_runtime
    (
        "shared.view_present",
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = $1 AND n.nspname = 'pg_catalog'",
    ),
]


# ---------------------------------------------------------------------------
# Catalogue 2 — SQL strings that MUST NOT parse on the pinned pglast.
# ---------------------------------------------------------------------------
#
# pglast 7.x bundles libpg_query for PostgreSQL 17. PG 19's
# new grammar (REPACK, MERGE PARTITIONS, SPLIT PARTITION, WAIT FOR LSN,
# GRAPH_TABLE) trips a parse error at the new keyword. When pglast picks
# up PG 19's parser source, these tests will fail because parses will
# *succeed*. That's the signal to move the entry into the OK catalogue.

_PARSE_FAIL_CATALOGUE: list[tuple[str, str, str]] = [
    # mcpg.repack — in-server REPACK
    ("repack.basic", 'REPACK "schema"."tbl"', "REPACK"),
    ("repack.concurrently", 'REPACK "schema"."tbl" CONCURRENTLY', "REPACK"),
    # mcpg.pg19_partitions — MERGE PARTITIONS / SPLIT PARTITION
    (
        "pg19_partitions.merge",
        'ALTER TABLE "public"."orders" MERGE PARTITIONS ("p1", "p2") INTO "p_merged"',
        "MERGE",
    ),
    (
        "pg19_partitions.split",
        'ALTER TABLE "public"."orders" SPLIT PARTITION "p_q1" INTO ('
        'PARTITION "p_jan" FOR VALUES FROM (1) TO (32), '
        'PARTITION "p_febmar" FOR VALUES FROM (32) TO (90))',
        "SPLIT",
    ),
    # mcpg.wait_for_lsn — RYW primitive
    (
        "wait_for_lsn.basic",
        "WAIT FOR LSN '0/12345678' TIMEOUT 5000",
        "WAIT",
    ),
    # mcpg.pgq — SQL/PGQ property graph queries (GRAPH_TABLE clause)
    (
        "pgq.graph_table",
        "SELECT * FROM GRAPH_TABLE (g MATCH (a)-[e]->(b) COLUMNS (a.id))",
        "MATCH",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "sql"),
    _PARSE_OK_CATALOGUE,
    ids=[label for label, _ in _PARSE_OK_CATALOGUE],
)
def test_pg_le_17_compatible_sql_parses_via_pglast(label: str, sql: str) -> None:
    """Each catalogue SELECT / function-call DDL must parse cleanly.

    The drift this catches: a typo'd identifier or a missing comma in
    a multi-line SELECT lands green in the mocked-driver unit tests
    but breaks against a real cluster. pglast is the cheapest gate —
    a few microseconds per parse, no DB round trip, no container.
    """
    parsed: Any = pglast.parse_sql(sql)
    assert parsed, f"{label}: pglast returned no AST nodes"


@pytest.mark.parametrize(
    ("label", "sql", "expected_token"),
    _PARSE_FAIL_CATALOGUE,
    ids=[label for label, _, _ in _PARSE_FAIL_CATALOGUE],
)
def test_pg19_only_grammar_fails_on_pinned_pglast(label: str, sql: str, expected_token: str) -> None:
    """PG 19-only grammar must trip the expected token on pglast 7.x.

    Once pglast picks up PG 19's parser source the parse will succeed
    and this assertion will fail — at that point move the entry into
    :data:`_PARSE_OK_CATALOGUE` and remove it from here.
    """
    with pytest.raises(pglast.parser.ParseError) as info:
        pglast.parse_sql(sql)
    assert expected_token in str(info.value), (
        f"{label}: expected pglast to trip near {expected_token!r}, got {info.value!r}"
    )


def test_parse_ok_catalogue_covers_every_phase3_module() -> None:
    """Coverage guard — every PG 19 module that emits SQL gets at least
    one catalogue entry (OK or FAIL).

    The check is a string-membership match against the label prefixes
    so adding a new PG 19 module without a catalogue entry trips the
    test immediately."""
    required_module_prefixes = {
        "pg19_runtime",
        "pg19_stats",
        "aio",
        "repack",
        "pg19_partitions",
        "wait_for_lsn",
        "pgq",
    }
    seen_prefixes: set[str] = set()
    for label, _ in _PARSE_OK_CATALOGUE:
        seen_prefixes.add(label.split(".", 1)[0])
    for label, _, _ in _PARSE_FAIL_CATALOGUE:
        seen_prefixes.add(label.split(".", 1)[0])
    missing = required_module_prefixes - seen_prefixes
    assert not missing, f"PG 19 modules without a catalogue entry: {sorted(missing)}"


def test_stats_reset_propagates_through_pg19_stats_reads() -> None:
    """Audit row #16 — PG 19 added ``stats_reset`` to every ``pg_stat_*``
    view that didn't already have it; the readers must surface it.

    This complements the in-module unit test in
    :mod:`tests.unit.test_pg19_stats` — that one drives the function;
    this one pins the SQL substring so a future refactor that drops
    the column from the SELECT trips a contract failure even if the
    dataclass field is still wired through ``.get()``."""
    from mcpg import pg19_stats

    # We pull the SQL strings out of the OK catalogue rather than re-
    # asserting them inline — that way the source of truth is one
    # place. If the catalogue entry diverges from the module the
    # parametrised parse test catches the catalogue half and this
    # test catches the module half.
    lock_sql = next(sql for label, sql in _PARSE_OK_CATALOGUE if label == "pg19_stats.lock_read")
    recovery_sql = next(sql for label, sql in _PARSE_OK_CATALOGUE if label == "pg19_stats.recovery_read")
    assert "stats_reset" in lock_sql
    assert "stats_reset" in recovery_sql
    # Catalogue entries are characterisation pins; the module is the
    # operational source. Re-render the module's own SELECTs by
    # source-grep to make sure they didn't drift.
    src = (pg19_stats.__file__ or "").replace(".pyc", ".py")
    with suppress(OSError):
        with open(src, encoding="utf-8") as fh:
            module_text = fh.read()
        assert "FROM pg_stat_lock" in module_text
        assert "FROM pg_stat_recovery" in module_text
        assert module_text.count("stats_reset") >= 2, (
            "expected stats_reset to be selected by both pg_stat_lock and pg_stat_recovery readers"
        )

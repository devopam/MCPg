"""Differential parity harness — first-party validator vs the vendored one.

The security-review gate for the de-vendor effort (roadmap 18.1): feed a
broad corpus of safe / unsafe / malformed SQL through **both**
``mcpg._vendor.sql.SafeSqlDriver`` and the first-party
``mcpg.sql.SafeSqlDriver`` and assert an **identical accept/reject verdict
on every input**. Zero divergence is the bar — this proves the re-author
didn't move the security boundary.

Temporary: deleted in PR 2 together with ``_vendor/``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from mcpg._vendor.sql import SafeSqlDriver as VendoredSafeSqlDriver
from mcpg.sql import SafeSqlDriver as FirstPartySafeSqlDriver

# Corpus: a mix of accepted read-only statements, rejected write/DDL/unsafe
# statements, and malformed / adversarial inputs. The point is not whether a
# given item is accepted, but that BOTH validators agree on it.
_CORPUS: list[str] = [
    # --- expected-safe read-only statements ---
    "SELECT 1",
    "SELECT * FROM users",
    "SELECT id, name FROM public.users WHERE id = 5",
    "SELECT count(*), max(created_at) FROM orders GROUP BY user_id",
    "SELECT * FROM a JOIN b ON a.id = b.a_id WHERE a.name LIKE 'foo%'",
    "WITH t AS (SELECT id FROM users) SELECT * FROM t",
    "SELECT * FROM users ORDER BY id LIMIT 10 OFFSET 5",
    "SELECT lower(name), upper(email) FROM users",
    "SELECT * FROM users u WHERE u.id IN (SELECT user_id FROM orders)",
    "EXPLAIN SELECT * FROM users",
    "EXPLAIN (FORMAT JSON) SELECT * FROM users",
    "SHOW search_path",
    "VACUUM users",
    "ANALYZE users",
    "SELECT jsonb_agg(row_to_json(u)) FROM users u",
    "SELECT array_agg(id) FROM users",
    "SELECT coalesce(name, 'n/a') FROM users",
    # --- expected-unsafe: writes / DDL / DCL ---
    "INSERT INTO users (name) VALUES ('x')",
    "UPDATE users SET name = 'x' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
    "DROP TABLE users",
    "CREATE TABLE t (id int)",
    "ALTER TABLE users ADD COLUMN x int",
    "TRUNCATE users",
    "GRANT SELECT ON users TO public",
    "CREATE EXTENSION pg_stat_statements",
    "CREATE EXTENSION definitely_not_allowed_ext",
    "COPY users TO '/tmp/x.csv'",
    "COPY users FROM '/tmp/x.csv'",
    "DO $$ BEGIN PERFORM 1; END $$",
    "SET search_path TO public",
    # --- expected-unsafe: read-only escapes ---
    "SELECT * FROM users FOR UPDATE",
    "EXPLAIN ANALYZE SELECT * FROM users",
    "SELECT pg_sleep(10)",
    "SELECT * FROM pg_read_file('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT 1; DROP TABLE users",
    "SELECT * FROM users; SELECT * FROM orders",
    # --- malformed / adversarial / edge ---
    "",
    "   ",
    "-- just a comment",
    "SELECT",
    "SELECT * FROM",
    "SELECT * FROM users WHERE name = 'unterminated",
    "SEL ECT bogus",
    "SELECT /* nested */ 1",
    "SELECT 'foo'; -- SELECT 2",
    "SELECT * FROM üsers",  # unicode identifier
    "SELECT " + "1 + " * 50 + "1",  # deeply nested expr
]


def _verdict(driver_cls: type, sql: str) -> tuple[bool, str]:
    """Return (accepted, error-class-name) for one validator on one input.

    ``accepted`` is ``True`` when ``_validate`` does not raise.
    """
    driver = driver_cls(Mock())
    try:
        driver._validate(sql)
        return (True, "")
    except Exception as exc:  # we compare verdicts, not raise
        return (False, type(exc).__name__)


@pytest.mark.parametrize("sql", _CORPUS, ids=lambda s: s[:40] or "<empty>")
def test_first_party_validator_matches_vendored_verdict(sql: str) -> None:
    vendored_ok, vendored_err = _verdict(VendoredSafeSqlDriver, sql)
    first_party_ok, first_party_err = _verdict(FirstPartySafeSqlDriver, sql)

    assert first_party_ok == vendored_ok, (
        f"verdict divergence on {sql!r}: "
        f"vendored={'accept' if vendored_ok else 'reject'} "
        f"first-party={'accept' if first_party_ok else 'reject'}"
    )
    # On rejection, the raised exception class should match too (both raise
    # ValueError from the validator).
    if not vendored_ok:
        assert first_party_err == vendored_err, (
            f"error-type divergence on {sql!r}: vendored={vendored_err} first-party={first_party_err}"
        )


def test_corpus_exercises_both_verdicts() -> None:
    """Sanity: the corpus contains both accepted and rejected inputs, so the
    parity test isn't vacuously passing on an all-reject (or all-accept) set."""
    verdicts = {_verdict(FirstPartySafeSqlDriver, sql)[0] for sql in _CORPUS}
    assert verdicts == {True, False}

"""Fuzz / robustness pass for the first-party SQL-safety validator.

Part of the roadmap-18.1 security-review gate. Throws malformed, oversized,
and adversarial input at ``SafeSqlDriver._validate`` and asserts it always
returns a **clean verdict** — either it accepts (returns ``None``) or it
rejects by raising ``ValueError``. It must **never** crash with a different
exception type, and must complete promptly (no unbounded parse / recursion).

The validator is the boundary between an agent and the database, so
"rejects safely" and "doesn't crash the server" are both requirements.
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from mcpg.sql import SafeSqlDriver

# Adversarial / malformed inputs — none should raise anything but ValueError.
_FUZZ_INPUTS: list[str] = [
    # empty / whitespace / comments only
    "",
    " ",
    "\n\t ",
    "-- comment",
    "/* block */",
    "/* unterminated",
    ";",
    ";;;;;",
    # comment-smuggling
    "SELECT 1 --\n; DROP TABLE x",
    "SELECT 1 /* ; DROP TABLE x */",
    "SEL/**/ECT 1",
    # unicode / homoglyph / control chars
    "SELECT * FROM üsers",
    "SELECT '​' FROM t",  # zero-width space literal
    "SELECT * FROM таблица",  # cyrillic identifier
    "SELECT\x00 1",  # embedded null
    # deeply nested — must not blow the stack / hang
    "SELECT " + "(" * 500 + "1" + ")" * 500,
    "SELECT " + "1+" * 500 + "1",
    "WITH " + " ".join(f"c{i} AS (SELECT 1)," for i in range(200)) + " x AS (SELECT 1) SELECT 1",
    "SELECT * FROM t WHERE " + " AND ".join("a = 1" for _ in range(500)),
    # oversized token / identifier
    "SELECT " + "a" * 100_000,
    "SELECT * FROM " + "t" * 50_000,
    # broken syntax
    "SELECT * FROM",
    "SELECT FROM WHERE",
    "))))",
    "'unterminated",
    '"unterminated',
    "$$ unterminated dollar",
    "SELECT $1$2$3",
    # statement-stacking attempts
    "SELECT 1; INSERT INTO t VALUES (1)",
    "SELECT 1; SELECT 2; DROP TABLE t",
    "VACUUM; DROP TABLE t",
    # write / DDL / exec attempts
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "DO $$ BEGIN END $$",
    "CALL some_proc()",
    "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN DELETE",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT dblink('', '')",
    "COPY t FROM PROGRAM 'id'",
    # binary-ish / random bytes as text
    "\x01\x02\x03\x04",
    "SELECT '" + "\\x00" * 100 + "'",
]


@pytest.mark.parametrize("sql", _FUZZ_INPUTS, ids=lambda s: repr(s[:32]))
def test_validator_returns_clean_verdict_on_fuzz_input(sql: str) -> None:
    driver = SafeSqlDriver(Mock())
    start = time.monotonic()
    try:
        driver._validate(sql)  # accepted
    except ValueError:
        pass  # rejected — the only allowed rejection path
    except Exception as exc:
        pytest.fail(f"validator raised {type(exc).__name__} (not ValueError) on {sql[:60]!r}: {exc}")
    elapsed = time.monotonic() - start
    # Parsing + walking any single input must be quick — guards against a
    # pathological input causing an unbounded parse / recursion hang.
    assert elapsed < 5.0, f"validation took {elapsed:.1f}s on {sql[:60]!r} (possible DoS input)"

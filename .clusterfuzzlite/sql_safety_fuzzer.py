"""Atheris fuzz harness for the SQL-safety kernel's parse+validate path.

Feeds arbitrary bytes to ``SafeSqlDriver._validate`` — the ``pglast``-based
parser + AST-walker in ``mcpg.sql.safety`` — and treats anything other than
the documented ``ValueError`` (malformed or disallowed SQL) as a bug: an
unhandled exception, a native crash inside the C-based ``pglast`` parser, or
a hang. This is the project's actual security-critical surface; see
CLAUDE.md's "SQL-safety kernel" section and
docs/reviews/devendor-sql-kernel-security-review.md for the threat model
this complements (that doc covers the adversarial *unit* test suite —
known-shape attacks; this harness covers unknown-shape inputs).
"""

import contextlib
import sys

import atheris

with atheris.instrument_imports():
    from mcpg.sql.safety import SafeSqlDriver

# _validate() only reads the class-level ALLOWED_* policy aliases; the
# wrapped driver is never touched during validation, so a real SqlDriver
# is unnecessary here.
_driver = SafeSqlDriver(sql_driver=None)  # type: ignore[arg-type]


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    query = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    with contextlib.suppress(ValueError):  # expected: malformed or policy-disallowed SQL is rejected
        _driver._validate(query)  # fuzzing the private validator directly


atheris.Setup(sys.argv, test_one_input)
atheris.Fuzz()

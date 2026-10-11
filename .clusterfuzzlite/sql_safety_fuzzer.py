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

import sys

import atheris

with atheris.instrument_imports():
    from pglast.parser import ParseError

    from mcpg.sql.safety import SafeSqlDriver

# _validate() only reads the class-level ALLOWED_* policy aliases; the
# wrapped driver is never touched during validation, so a real SqlDriver
# is unnecessary here.
_driver = SafeSqlDriver(sql_driver=None)  # type: ignore[arg-type]


def test_one_input(data: bytes) -> None:
    # Decode the raw bytes directly. FuzzedDataProvider.ConsumeUnicode*
    # reads bytes as variable-width code points, which turns ASCII SQL
    # keywords into garbage characters — pglast then rejects nearly every
    # input at the first token and coverage never leaves the parser's
    # error path (observed: flat cov with valid-SQL seeds). A plain decode
    # keeps seeds and dictionary tokens meaningful to the mutator.
    query = data.decode("utf-8", errors="replace")
    try:
        _driver._validate(query)  # fuzzing the private validator directly
    except ValueError as exc:
        # Expected: malformed or policy-disallowed SQL is rejected. But
        # _validate wraps *any* exception from its AST walker in a bare
        # ValueError, so a walker bug (AttributeError, RecursionError, ...)
        # would be indistinguishable from a normal rejection. Genuine
        # rejections chain a ValueError (policy) or ParseError (syntax) as
        # __cause__ (or none at all); anything else is a bug — re-raise.
        cause = exc.__cause__
        if cause is not None and not isinstance(cause, (ValueError, ParseError)):
            raise


atheris.Setup(sys.argv, test_one_input)
atheris.Fuzz()

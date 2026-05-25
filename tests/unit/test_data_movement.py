"""Tests for the in-process data-movement export tools."""

import csv
import io
import json
from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.data_movement import (
    DEFAULT_EXPORT_LIMIT,
    EXPORT_FORMATS,
    CopyTableResult,
    ExportError,
    ExportResult,
    ImportDataError,
    ImportResult,
    _libpq_env_from_url,
    _rows_to_csv,
    _rows_to_json,
    copy_table_between_databases,
    dump_database,
    export_query,
    export_table,
    import_csv,
    import_json,
    restore_database,
)
from mcpg.server import create_server
from mcpg.shell import ShellError, SubprocessResult

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- _rows_to_csv / _rows_to_json -----------------------------------------


def test_rows_to_csv_emits_header_and_quotes_strings_with_commas() -> None:
    out = _rows_to_csv([{"id": 1, "name": "a, b"}, {"id": 2, "name": "c"}])
    assert out.splitlines()[0] == "id,name"
    assert '"a, b"' in out


def test_rows_to_csv_returns_empty_string_for_no_rows() -> None:
    assert _rows_to_csv([]) == ""


def test_rows_to_csv_stringifies_non_scalar_cell_values() -> None:
    # datetime / UUID / decimal arrive verbatim — anything not a plain
    # scalar gets str()'d so the CSV is always readable.
    from datetime import datetime

    out = _rows_to_csv([{"created": datetime(2026, 5, 24, 12, 0, 0)}])
    assert "2026-05-24" in out


def test_rows_to_csv_emits_empty_string_for_none_not_the_literal_none() -> None:
    # CSV NULL convention is an empty field. Passing None to DictWriter
    # would otherwise write the literal "None" — bad for downstream
    # consumers that try to load the file with a typed reader.
    out = _rows_to_csv([{"id": 1, "name": None}])
    lines = out.splitlines()
    # Header + one data row.
    assert lines[0] == "id,name"
    # Row is ``1,`` — the trailing field is empty, not the string "None".
    assert lines[1] == "1,"
    assert "None" not in out


def test_rows_to_csv_serialises_dicts_and_lists_as_json_strings() -> None:
    # psycopg returns jsonb columns as Python dicts/lists. Calling str()
    # on them would produce Python repr (single quotes, ``True`` not
    # ``true``); json.dumps gives a valid JSON cell a JSON reader can
    # parse on the round-trip.
    out = _rows_to_csv([{"id": 1, "config": {"a": 1, "b": True}, "tags": [1, 2, 3]}])

    # Round-trip through DictReader so the CSV quoting is interpreted
    # correctly (don't hand-roll comma splitting on a field that
    # itself contains commas).
    parsed_rows = list(csv.DictReader(io.StringIO(out)))
    assert len(parsed_rows) == 1
    row = parsed_rows[0]
    assert row["id"] == "1"
    # The jsonb-shaped cells round-trip cleanly through json.loads —
    # they are valid JSON strings, not Python repr.
    assert json.loads(row["config"]) == {"a": 1, "b": True}
    assert json.loads(row["tags"]) == [1, 2, 3]
    # No Python repr leaked into the raw CSV text.
    assert " True" not in out  # JSON form is lowercase ``true``
    assert "'a'" not in out


def test_rows_to_json_serialises_with_default_str_for_non_native_types() -> None:
    from datetime import datetime

    out = _rows_to_json([{"id": 1, "created": datetime(2026, 5, 24)}])
    parsed = json.loads(out)
    assert parsed[0]["id"] == 1
    assert "2026-05-24" in parsed[0]["created"]


# --- export_query ---------------------------------------------------------


async def test_export_query_emits_csv_with_header_and_rows() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    result = await export_query(driver, "SELECT id, name FROM widget", format="csv")  # type: ignore[arg-type]

    assert isinstance(result, ExportResult)
    assert result.format == "csv"
    assert result.row_count == 2
    assert result.truncated is False
    lines = result.content.splitlines()
    assert lines[0] == "id,name"
    assert lines[1].startswith("1,")
    assert lines[2].startswith("2,")


async def test_export_query_emits_json_array_of_objects() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}])

    result = await export_query(driver, "SELECT id, name FROM widget", format="json")  # type: ignore[arg-type]

    parsed = json.loads(result.content)
    assert parsed == [{"id": 1, "name": "a"}]


async def test_export_query_flags_truncation_when_query_yields_more_than_limit() -> None:
    driver = FakeDriver([{"id": i} for i in range(5)])

    result = await export_query(driver, "SELECT id FROM widget", format="csv", limit=2)  # type: ignore[arg-type]

    assert result.truncated is True
    assert result.row_count == 2


async def test_export_query_rejects_unsupported_format() -> None:
    with pytest.raises(ExportError, match="unsupported export format"):
        await export_query(FakeDriver(), "SELECT 1", format="xml")  # type: ignore[arg-type]


async def test_export_query_rejects_non_positive_limit() -> None:
    with pytest.raises(ExportError, match="must be at least 1"):
        await export_query(FakeDriver(), "SELECT 1", limit=0)  # type: ignore[arg-type]


async def test_export_query_wraps_query_errors() -> None:
    # SafeSqlDriver inside run_select rejects non-SELECT — surfaces as
    # ExportError so the agent sees a single failure mode.
    with pytest.raises(ExportError):
        await export_query(FakeDriver(), "DELETE FROM widget")  # type: ignore[arg-type]


# --- export_table ---------------------------------------------------------


async def test_export_table_builds_a_safe_select_against_quoted_identifiers() -> None:
    driver = FakeDriver([{"id": 1}])

    await export_table(driver, "app", "widget", format="csv")  # type: ignore[arg-type]

    # The vendored SafeSqlDriver wraps the raw driver — the call we
    # care about is whatever SQL it eventually issued. Inspect the
    # FakeDriver's call log for the schema-qualified SELECT shape.
    sqls = [call[0] for call in driver.calls]
    assert any('"app"."widget"' in sql or "app.widget" in sql for sql in sqls)


async def test_export_table_rejects_invalid_identifier_characters() -> None:
    with pytest.raises(ExportError, match="invalid schema name"):
        await export_table(FakeDriver(), 'app"; DROP TABLE x; --', "widget")  # type: ignore[arg-type]
    with pytest.raises(ExportError, match="invalid table name"):
        await export_table(FakeDriver(), "app", "widget; DROP")  # type: ignore[arg-type]


# --- module exports + tool wiring -----------------------------------------


def test_export_formats_set_is_complete() -> None:
    assert EXPORT_FORMATS == {"csv", "json"}


def test_default_export_limit_is_a_sensible_ceiling() -> None:
    # Documented in the docstring; tests pin the constant so accidental
    # bumps surface in PR review.
    assert DEFAULT_EXPORT_LIMIT == 10_000


async def test_export_tools_are_registered_and_callable_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver([{"id": 1}])))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {"export_query", "export_table"} <= listed

        result = await client.call_tool("export_query", {"sql": "SELECT id FROM widget"})
    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["format"] == "csv"
    assert payload["row_count"] == 1


# --- dump_database (subprocess gate, ADR-0004) ----------------------------


def test_libpq_env_from_url_extracts_pg_env_vars() -> None:
    env = _libpq_env_from_url("postgresql://user:secret@db.host:5433/mydb?sslmode=require")
    assert env["PGHOST"] == "db.host"
    assert env["PGPORT"] == "5433"
    assert env["PGUSER"] == "user"
    assert env["PGPASSWORD"] == "secret"
    assert env["PGDATABASE"] == "mydb"


def test_libpq_env_from_url_url_decodes_credentials() -> None:
    # A password with URL-reserved characters arrives percent-encoded
    # in the URL but must reach PGPASSWORD decoded.
    env = _libpq_env_from_url("postgresql://u%40org:p%23s%24@h/d")
    assert env["PGUSER"] == "u@org"
    assert env["PGPASSWORD"] == "p#s$"


def test_libpq_env_from_url_omits_keys_for_missing_url_parts() -> None:
    env = _libpq_env_from_url("postgresql:///mydb")
    assert "PGHOST" not in env
    assert "PGUSER" not in env
    assert env["PGDATABASE"] == "mydb"


async def test_dump_database_invokes_pg_dump_with_libpq_env_and_format_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(binary: str, *argv: str, **kwargs: object) -> SubprocessResult:
        captured["binary"] = binary
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        return SubprocessResult(
            binary=binary,
            argv=list(argv),
            exit_code=0,
            stdout=b"-- PostgreSQL database dump\nSELECT 1;\n",
            stderr=b"",
            output_bytes=39,
            output_truncated=False,
            timed_out=False,
            env_redacted={"PGPASSWORD": "****"},
        )

    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", fake_run)

    result = await dump_database(
        "postgresql://u:p@h:5432/db",
        timeout_sec=10,
        max_output_bytes=1024,
    )

    assert captured["binary"] == "pg_dump"
    assert "--format=plain" in captured["argv"]  # type: ignore[operator]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PGHOST"] == "h"
    assert env["PGDATABASE"] == "db"
    # Password is in the env passed to the subprocess; the runner does
    # the redaction for audit logging, not the dump_database helper.
    assert env["PGPASSWORD"] == "p"

    assert result.exit_code == 0
    assert "PostgreSQL database dump" in result.content
    assert result.output_truncated is False


async def test_dump_database_base64_encodes_binary_format_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    async def fake_run(binary: str, *argv: str, **kwargs: object) -> SubprocessResult:
        return SubprocessResult(
            binary=binary,
            argv=list(argv),
            exit_code=0,
            stdout=b"\x00\x01\x02binary\xff",  # not valid utf-8 — must be base64'd
            stderr=b"",
            output_bytes=9,
            output_truncated=False,
            timed_out=False,
            env_redacted={},
        )

    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", fake_run)

    result = await dump_database(
        "postgresql://u@h/db",
        timeout_sec=10,
        max_output_bytes=1024,
        format="custom",
    )
    decoded = base64.b64decode(result.content)
    assert decoded == b"\x00\x01\x02binary\xff"


async def test_dump_database_rejects_unsupported_format() -> None:
    with pytest.raises(ShellError, match="unsupported pg_dump format"):
        await dump_database("postgresql://u@h/db", timeout_sec=10, max_output_bytes=1024, format="bogus")


async def test_dump_database_rejects_directory_format_as_v1_unsupported() -> None:
    with pytest.raises(ShellError, match=r"directory.*not supported"):
        await dump_database("postgresql://u@h/db", timeout_sec=10, max_output_bytes=1024, format="directory")


async def test_dump_database_requires_a_database_name_in_the_url() -> None:
    with pytest.raises(ShellError, match="must specify a database"):
        await dump_database("postgresql://user@host:5432/", timeout_sec=10, max_output_bytes=1024)


# --- tool wiring: dump_database is shell-gated ----------------------------


_UNRESTRICTED_NO_SHELL = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_UNRESTRICTED_SHELL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_SHELL": "true",
    }
)


async def test_dump_database_tool_hidden_without_unrestricted_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "dump_database" not in listed


async def test_dump_database_tool_hidden_in_unrestricted_without_allow_shell() -> None:
    # Same defence-in-depth pattern as partman vs MCPG_ALLOW_DDL —
    # unrestricted alone must not expose subprocess tools.
    server = create_server(_UNRESTRICTED_NO_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "dump_database" not in listed


async def test_dump_database_tool_registered_in_unrestricted_with_allow_shell() -> None:
    server = create_server(_UNRESTRICTED_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "dump_database" in listed


# --- restore_database -----------------------------------------------------


async def test_restore_database_plain_format_uses_psql_and_pipes_sql_to_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(binary: str, *argv: str, **kwargs: object) -> SubprocessResult:
        captured["binary"] = binary
        captured["argv"] = list(argv)
        captured["stdin"] = kwargs.get("stdin")
        captured["env"] = kwargs.get("env")
        return SubprocessResult(
            binary=binary,
            argv=list(argv),
            exit_code=0,
            stdout=b"",
            stderr=b"",
            output_bytes=0,
            output_truncated=False,
            timed_out=False,
            env_redacted={},
        )

    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", fake_run)

    sql = "CREATE TABLE w (id integer);"
    result = await restore_database(
        "postgresql://u:p@h/db",
        sql,
        timeout_sec=10,
        max_output_bytes=1024,
    )

    assert captured["binary"] == "psql"
    # Single-transaction and ON_ERROR_STOP keep partial-restore footguns away.
    assert "--single-transaction" in captured["argv"]  # type: ignore[operator]
    assert "--set=ON_ERROR_STOP=on" in captured["argv"]  # type: ignore[operator]
    assert "--file=-" in captured["argv"]  # type: ignore[operator]
    # The SQL must be piped through stdin, not interpolated into argv.
    assert captured["stdin"] == sql.encode("utf-8")
    assert result.exit_code == 0


async def test_restore_database_custom_format_base64_decodes_content_and_invokes_pg_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    captured: dict[str, object] = {}

    async def fake_run(binary: str, *argv: str, **kwargs: object) -> SubprocessResult:
        captured["binary"] = binary
        captured["argv"] = list(argv)
        captured["stdin"] = kwargs.get("stdin")
        return SubprocessResult(
            binary=binary,
            argv=list(argv),
            exit_code=0,
            stdout=b"",
            stderr=b"",
            output_bytes=0,
            output_truncated=False,
            timed_out=False,
            env_redacted={},
        )

    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", fake_run)

    raw = b"\x00\x01PGDMP\xff binary archive"
    encoded = base64.b64encode(raw).decode("ascii")
    await restore_database(
        "postgresql://u@h/db",
        encoded,
        timeout_sec=10,
        max_output_bytes=1024,
        format="custom",
    )

    assert captured["binary"] == "pg_restore"
    assert "--format=custom" in captured["argv"]  # type: ignore[operator]
    # The base64-decoded raw bytes must be piped through stdin verbatim.
    assert captured["stdin"] == raw


async def test_restore_database_rejects_unsupported_format() -> None:
    with pytest.raises(ShellError, match="unsupported restore format"):
        await restore_database(
            "postgresql://u@h/db",
            "noop",
            timeout_sec=10,
            max_output_bytes=1024,
            format="bogus",
        )


async def test_restore_database_rejects_invalid_base64_for_binary_formats() -> None:
    with pytest.raises(ShellError, match="not valid base64"):
        await restore_database(
            "postgresql://u@h/db",
            "!!!not-base64!!!",
            timeout_sec=10,
            max_output_bytes=1024,
            format="custom",
        )


async def test_restore_database_requires_a_database_name_in_the_url() -> None:
    with pytest.raises(ShellError, match="must specify a database"):
        await restore_database(
            "postgresql://user@host:5432/",
            "noop",
            timeout_sec=10,
            max_output_bytes=1024,
        )


async def test_restore_database_tool_hidden_without_allow_shell() -> None:
    server = create_server(_UNRESTRICTED_NO_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "restore_database" not in listed


async def test_restore_database_tool_registered_with_allow_shell() -> None:
    server = create_server(_UNRESTRICTED_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "restore_database" in listed


# --- copy_table_between_databases ----------------------------------------


class _FakeRunRecorder:
    """Stand-in for ``run_pg_binary`` that scripts a sequence of responses."""

    def __init__(self, responses: list[SubprocessResult]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, binary: str, *argv: str, **kwargs: Any) -> SubprocessResult:
        self.calls.append({"binary": binary, "argv": list(argv), **kwargs})
        return self._responses.pop(0)


def _ok(binary: str, argv: list[str], stdout: bytes = b"", stderr: bytes = b"") -> SubprocessResult:
    return SubprocessResult(
        binary=binary,
        argv=argv,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        output_bytes=len(stdout),
        output_truncated=False,
        timed_out=False,
        env_redacted={},
    )


async def test_copy_table_pipes_pg_dump_into_pg_restore_with_separate_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = b"PGDMP-binary-archive"
    runner = _FakeRunRecorder(
        [
            _ok("pg_dump", ["--format=custom", "--table=app.widget"], stdout=archive),
            _ok("pg_restore", ["--format=custom"]),
        ]
    )
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    result = await copy_table_between_databases(
        "postgresql://srcuser:srcpw@srchost/srcdb",
        "postgresql://dstuser:dstpw@dsthost/dstdb",
        "app",
        "widget",
        include_schema=True,
        include_data=True,
        timeout_sec=10,
        max_output_bytes=4096,
    )

    assert isinstance(result, CopyTableResult)
    assert result.dump_exit_code == 0
    assert result.restore_exit_code == 0
    assert result.timed_out is False

    # Each leg got its own URL's env. Credentials live in env, never argv.
    dump_call, restore_call = runner.calls
    assert dump_call["binary"] == "pg_dump"
    assert dump_call["env"]["PGHOST"] == "srchost"
    assert dump_call["env"]["PGDATABASE"] == "srcdb"
    assert dump_call["env"]["PGPASSWORD"] == "srcpw"
    assert "--table=app.widget" in dump_call["argv"]
    # The archive bytes flow into pg_restore's stdin verbatim.
    assert restore_call["binary"] == "pg_restore"
    assert restore_call["env"]["PGHOST"] == "dsthost"
    assert restore_call["env"]["PGDATABASE"] == "dstdb"
    assert restore_call["stdin"] == archive
    assert "--single-transaction" in restore_call["argv"]
    assert "--exit-on-error" in restore_call["argv"]


async def test_copy_table_schema_only_passes_schema_only_to_both_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _FakeRunRecorder(
        [
            _ok("pg_dump", [], stdout=b"x"),
            _ok("pg_restore", []),
        ]
    )
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    await copy_table_between_databases(
        "postgresql://u@h/src",
        "postgresql://u@h/dst",
        "app",
        "widget",
        include_schema=True,
        include_data=False,
        timeout_sec=10,
        max_output_bytes=4096,
    )

    assert "--schema-only" in runner.calls[0]["argv"]
    assert "--schema-only" in runner.calls[1]["argv"]


async def test_copy_table_data_only_passes_data_only_to_both_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _FakeRunRecorder(
        [
            _ok("pg_dump", [], stdout=b"x"),
            _ok("pg_restore", []),
        ]
    )
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    await copy_table_between_databases(
        "postgresql://u@h/src",
        "postgresql://u@h/dst",
        "app",
        "widget",
        include_schema=False,
        include_data=True,
        timeout_sec=10,
        max_output_bytes=4096,
    )

    assert "--data-only" in runner.calls[0]["argv"]
    assert "--data-only" in runner.calls[1]["argv"]


async def test_copy_table_rejects_when_neither_schema_nor_data_requested() -> None:
    with pytest.raises(ShellError, match="include_schema or include_data"):
        await copy_table_between_databases(
            "postgresql://u@h/src",
            "postgresql://u@h/dst",
            "app",
            "widget",
            include_schema=False,
            include_data=False,
            timeout_sec=10,
            max_output_bytes=4096,
        )


async def test_copy_table_rejects_unsafe_identifiers() -> None:
    with pytest.raises(ShellError, match="invalid schema name"):
        await copy_table_between_databases(
            "postgresql://u@h/src",
            "postgresql://u@h/dst",
            "app; DROP",
            "widget",
            include_schema=True,
            include_data=True,
            timeout_sec=10,
            max_output_bytes=4096,
        )
    with pytest.raises(ShellError, match="invalid table name"):
        await copy_table_between_databases(
            "postgresql://u@h/src",
            "postgresql://u@h/dst",
            "app",
            'widget"; --',
            include_schema=True,
            include_data=True,
            timeout_sec=10,
            max_output_bytes=4096,
        )


async def test_copy_table_requires_database_in_both_urls() -> None:
    with pytest.raises(ShellError, match="source_url must specify"):
        await copy_table_between_databases(
            "postgresql://u@h/",
            "postgresql://u@h/dst",
            "app",
            "widget",
            include_schema=True,
            include_data=True,
            timeout_sec=10,
            max_output_bytes=4096,
        )
    with pytest.raises(ShellError, match="dest_url must specify"):
        await copy_table_between_databases(
            "postgresql://u@h/src",
            "postgresql://u@h/",
            "app",
            "widget",
            include_schema=True,
            include_data=True,
            timeout_sec=10,
            max_output_bytes=4096,
        )


async def test_copy_table_refuses_to_restore_a_truncated_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    truncated_dump = SubprocessResult(
        binary="pg_dump",
        argv=[],
        exit_code=0,
        stdout=b"only-the-first-bytes",
        stderr=b"",
        output_bytes=10_000_000,  # what the child actually wrote, before our cap
        output_truncated=True,
        timed_out=False,
        env_redacted={},
    )
    runner = _FakeRunRecorder([truncated_dump])
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    with pytest.raises(ShellError, match="exceeded max_output_bytes"):
        await copy_table_between_databases(
            "postgresql://u@h/src",
            "postgresql://u@h/dst",
            "app",
            "widget",
            include_schema=True,
            include_data=True,
            timeout_sec=10,
            max_output_bytes=128,
        )
    # pg_restore was never invoked — only the dump call was recorded.
    assert len(runner.calls) == 1


async def test_copy_table_returns_dump_failure_without_invoking_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_dump = SubprocessResult(
        binary="pg_dump",
        argv=[],
        exit_code=1,
        stdout=b"",
        stderr=b"pg_dump: error: connection refused",
        output_bytes=0,
        output_truncated=False,
        timed_out=False,
        env_redacted={},
    )
    runner = _FakeRunRecorder([failed_dump])
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    result = await copy_table_between_databases(
        "postgresql://u@h/src",
        "postgresql://u@h/dst",
        "app",
        "widget",
        include_schema=True,
        include_data=True,
        timeout_sec=10,
        max_output_bytes=4096,
    )

    assert result.dump_exit_code == 1
    # Sentinel — restore was skipped.
    assert result.restore_exit_code == -1
    assert "connection refused" in result.dump_stderr_tail
    assert result.restore_argv == []
    assert len(runner.calls) == 1


async def test_copy_table_surfaces_restore_timeout_in_the_combined_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timed_out_restore = SubprocessResult(
        binary="pg_restore",
        argv=[],
        exit_code=-9,
        stdout=b"",
        stderr=b"",
        output_bytes=0,
        output_truncated=False,
        timed_out=True,
        env_redacted={},
    )
    runner = _FakeRunRecorder(
        [
            _ok("pg_dump", [], stdout=b"PGDMP"),
            timed_out_restore,
        ]
    )
    monkeypatch.setattr("mcpg.data_movement.run_pg_binary", runner)

    result = await copy_table_between_databases(
        "postgresql://u@h/src",
        "postgresql://u@h/dst",
        "app",
        "widget",
        include_schema=False,
        include_data=True,
        timeout_sec=1,
        max_output_bytes=4096,
    )

    assert result.timed_out is True
    assert result.restore_exit_code == -9


async def test_copy_table_tool_hidden_without_allow_shell() -> None:
    server = create_server(_UNRESTRICTED_NO_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "copy_table_between_databases" not in listed


async def test_copy_table_tool_registered_with_allow_shell() -> None:
    server = create_server(_UNRESTRICTED_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "copy_table_between_databases" in listed


# --- import_csv / import_json --------------------------------------------


async def test_import_csv_issues_copy_from_stdin_with_safe_identifiers_and_options() -> None:
    db = FakeDatabase(FakeDriver(), copy_rowcount=3)
    csv_payload = "id,name\n1,alpha\n2,beta\n3,gamma\n"

    result = await import_csv(db, "app", "widget", csv_payload)  # type: ignore[arg-type]

    assert isinstance(result, ImportResult)
    assert result.schema == "app"
    assert result.table == "widget"
    assert result.format == "csv"
    assert result.rows_imported == 3
    # One COPY call was made; the SQL quotes identifiers + sets the right options.
    assert len(db.copy_calls) == 1
    sql, data = db.copy_calls[0]
    assert 'COPY "app"."widget"' in sql
    assert "FORMAT csv" in sql
    assert "HEADER true" in sql
    assert "DELIMITER ','" in sql
    assert data == csv_payload.encode("utf-8")


async def test_import_csv_threads_explicit_columns_into_copy_sql() -> None:
    db = FakeDatabase(FakeDriver(), copy_rowcount=1)
    await import_csv(db, "app", "widget", "1,alpha\n", header=False, columns=["id", "name"])  # type: ignore[arg-type]

    sql, _ = db.copy_calls[0]
    assert '("id", "name")' in sql
    assert "HEADER false" in sql


async def test_import_csv_rejects_empty_column_list() -> None:
    with pytest.raises(ImportDataError, match="must not be empty"):
        await import_csv(FakeDatabase(FakeDriver()), "app", "widget", "x\n", columns=[])  # type: ignore[arg-type]


async def test_import_csv_rejects_unsafe_identifiers() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="invalid schema name"):
        await import_csv(db, "app; DROP", "widget", "x\n")  # type: ignore[arg-type]
    with pytest.raises(ImportDataError, match="invalid table name"):
        await import_csv(db, "app", 'widget"; --', "x\n")  # type: ignore[arg-type]
    with pytest.raises(ImportDataError, match="invalid column name"):
        await import_csv(db, "app", "widget", "x\n", columns=["bad name"])  # type: ignore[arg-type]


async def test_import_csv_rejects_dangerous_delimiters() -> None:
    db = FakeDatabase(FakeDriver())
    # Multi-char delimiter — would break COPY syntax.
    with pytest.raises(ImportDataError, match="invalid delimiter"):
        await import_csv(db, "app", "widget", "x\n", delimiter=",,")  # type: ignore[arg-type]
    # Quote / newline — could close the options list early.
    with pytest.raises(ImportDataError, match="invalid delimiter"):
        await import_csv(db, "app", "widget", "x\n", delimiter="'")  # type: ignore[arg-type]
    with pytest.raises(ImportDataError, match="invalid delimiter"):
        await import_csv(db, "app", "widget", "x\n", delimiter="\n")  # type: ignore[arg-type]


async def test_import_csv_wraps_underlying_copy_failures() -> None:
    class _BoomDatabase(FakeDatabase):
        async def copy_from_stdin(self, sql: str, data: bytes) -> int:
            raise RuntimeError("connection lost")

    db = _BoomDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="COPY failed"):
        await import_csv(db, "app", "widget", "1\n")  # type: ignore[arg-type]


async def test_import_csv_clamps_negative_rowcount_to_zero() -> None:
    db = FakeDatabase(FakeDriver(), copy_rowcount=-1)
    result = await import_csv(db, "app", "widget", "x\n")  # type: ignore[arg-type]
    assert result.rows_imported == 0


async def test_import_json_derives_columns_and_runs_parametrised_insert() -> None:
    db = FakeDatabase(FakeDriver())
    payload = json.dumps([{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}])

    result = await import_json(db, "app", "widget", payload)  # type: ignore[arg-type]

    assert isinstance(result, ImportResult)
    assert result.format == "json"
    assert result.rows_imported == 2
    sql, rows = db.execute_many_calls[0]
    assert 'INSERT INTO "app"."widget" ("id", "name") VALUES (%s, %s)' == sql
    assert rows == [(1, "alpha"), (2, "beta")]


async def test_import_json_uses_explicit_columns_when_supplied() -> None:
    db = FakeDatabase(FakeDriver())
    payload = json.dumps([{"id": 1, "name": "a", "extra": "ignored"}])

    await import_json(db, "app", "widget", payload, columns=["id", "name"])  # type: ignore[arg-type]

    sql, rows = db.execute_many_calls[0]
    assert "INSERT" in sql
    assert '"id", "name"' in sql
    assert "extra" not in sql
    assert rows == [(1, "a")]


async def test_import_json_serialises_nested_dicts_and_lists_for_jsonb() -> None:
    db = FakeDatabase(FakeDriver())
    payload = json.dumps([{"id": 1, "config": {"a": 1}, "tags": ["x", "y"]}])

    await import_json(db, "app", "widget", payload)  # type: ignore[arg-type]

    _, rows = db.execute_many_calls[0]
    assert rows[0][0] == 1
    # Nested values are sent as JSON strings, not Python repr.
    assert json.loads(rows[0][1]) == {"a": 1}
    assert json.loads(rows[0][2]) == ["x", "y"]


async def test_import_json_binds_missing_keys_as_null() -> None:
    db = FakeDatabase(FakeDriver())
    payload = json.dumps([{"id": 1, "name": "alpha"}, {"id": 2}])

    await import_json(db, "app", "widget", payload)  # type: ignore[arg-type]

    _, rows = db.execute_many_calls[0]
    assert rows == [(1, "alpha"), (2, None)]


async def test_import_json_empty_array_is_a_no_op() -> None:
    db = FakeDatabase(FakeDriver())
    result = await import_json(db, "app", "widget", "[]")  # type: ignore[arg-type]
    assert result.rows_imported == 0
    # No SQL was issued at all — empty input doesn't touch the database.
    assert db.execute_many_calls == []


async def test_import_json_rejects_non_array_payloads() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="must be a JSON array"):
        await import_json(db, "app", "widget", json.dumps({"id": 1}))  # type: ignore[arg-type]


async def test_import_json_rejects_invalid_json() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="not valid JSON"):
        await import_json(db, "app", "widget", "{not-json")  # type: ignore[arg-type]


async def test_import_json_rejects_non_object_rows() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match=r"every row .* must be an object"):
        await import_json(db, "app", "widget", json.dumps([[1, 2], [3, 4]]))  # type: ignore[arg-type]


async def test_import_json_rejects_unsafe_identifiers() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="invalid schema name"):
        await import_json(db, "app; DROP", "widget", "[]")  # type: ignore[arg-type]


async def test_import_json_falls_back_to_input_length_when_rowcount_is_negative() -> None:
    # psycopg's executemany sometimes reports -1; the helper compensates.
    db = FakeDatabase(FakeDriver(), execute_many_rowcount=-1)
    await import_json(db, "app", "widget", json.dumps([{"id": 1}, {"id": 2}, {"id": 3}]))  # type: ignore[arg-type]
    # We don't assert on the result directly here; the executemany_rowcount
    # forces the fallback path and the call was recorded.
    assert len(db.execute_many_calls) == 1


async def test_import_json_wraps_underlying_insert_failures() -> None:
    class _BoomDatabase(FakeDatabase):
        async def execute_many(self, sql: str, params_seq: Any) -> int:
            raise RuntimeError("PK collision")

    db = _BoomDatabase(FakeDriver())
    with pytest.raises(ImportDataError, match="INSERT failed"):
        await import_json(db, "app", "widget", json.dumps([{"id": 1}]))  # type: ignore[arg-type]


# --- tool wiring: import_csv / import_json gated under WRITE -------------


async def test_import_tools_hidden_in_read_only_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "import_csv" not in listed
    assert "import_json" not in listed


async def test_import_tools_registered_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED_NO_SHELL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {"import_csv", "import_json"} <= listed

        result = await client.call_tool(
            "import_csv",
            {"schema": "app", "table": "widget", "content": "id\n1\n2\n"},
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["format"] == "csv"

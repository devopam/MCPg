"""Unit tests for mcpg.guc (ALTER SYSTEM tools)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpg.guc import (
    _DEFAULT_DENYLIST,
    GucError,
    _maybe_reload,
    _quote_ident_path,
    _quote_literal,
    _validate_guc_name,
    _validate_value,
    alter_system,
    alter_system_reset_all,
    inspect_guc,
    parse_name_list,
    reload_config,
)


def test_validate_guc_name_accepts_dotted() -> None:
    assert _validate_guc_name("work_mem") == "work_mem"
    assert _validate_guc_name("auto_explain.log_min_duration") == "auto_explain.log_min_duration"


def test_validate_guc_name_rejects_bad() -> None:
    with pytest.raises(GucError, match="empty"):
        _validate_guc_name("")
    with pytest.raises(GucError, match="invalid GUC name"):
        _validate_guc_name("work mem")
    with pytest.raises(GucError, match="invalid GUC name"):
        _validate_guc_name("work-mem")


def test_quote_helpers() -> None:
    assert _quote_ident_path("work_mem") == '"work_mem"'
    assert _quote_ident_path("auto_explain.log_min_duration") == '"auto_explain"."log_min_duration"'
    assert _quote_literal("4MB") == "E'4MB'"
    assert _quote_literal("a'b") == "E'a''b'"
    # backslash-before-quote must not break out when standard_conforming_strings=off
    assert _quote_literal("a\\'; DROP TABLE x; --") == "E'a\\\\''; DROP TABLE x; --'"
    with pytest.raises(GucError, match="NUL"):
        _quote_literal("a\x00b")


def test_guc_name_is_lowercased() -> None:
    assert _validate_guc_name("Work_Mem") == "work_mem"


def test_default_denylist_blocks_command_settings() -> None:
    for name in ("archive_command", "restore_command", "ssl_passphrase_command", "primary_conninfo"):
        assert name in _DEFAULT_DENYLIST


def test_parse_name_list() -> None:
    assert parse_name_list(None) == frozenset()
    assert parse_name_list(" work_mem , shared_buffers ") == frozenset({"work_mem", "shared_buffers"})


def test_default_denylist_has_listen_addresses() -> None:
    assert "listen_addresses" in _DEFAULT_DENYLIST
    assert "shared_preload_libraries" in _DEFAULT_DENYLIST


def _row(name: str, **kwargs: Any) -> MagicMock:
    cells = {
        "name": name,
        "setting": kwargs.get("setting", "4MB"),
        "unit": kwargs.get("unit", "kB"),
        "category": "Resource Usage",
        "short_desc": "desc",
        "context": kwargs.get("context", "user"),
        "vartype": "string",
        "source": "configuration file",
        "pending_restart": kwargs.get("pending_restart", False),
        "boot_val": "4MB",
        "reset_val": "4MB",
        "min_val": None,
        "max_val": None,
        "enumvals": None,
    }
    result = MagicMock()
    result.cells = cells
    return result


@pytest.mark.asyncio
async def test_inspect_guc() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    info = await inspect_guc(driver, "work_mem")
    assert info.name == "work_mem"
    assert info.setting == "4MB"
    assert info.alterable_via_alter_system is True


@pytest.mark.asyncio
async def test_inspect_guc_unknown() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=None)
    with pytest.raises(GucError, match="unknown GUC"):
        await inspect_guc(driver, "no_such_guc_xyz")


@pytest.mark.asyncio
async def test_alter_system_confirmation_required() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    database = AsyncMock()
    result = await alter_system(database, driver, "work_mem", "8MB", confirm=False)
    assert result.status == "confirmation_required"
    assert result.warning is not None
    assert "NOT transactional" in result.warning
    database.run_unmanaged.assert_not_called()


@pytest.mark.asyncio
async def test_alter_system_dry_run() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    database = AsyncMock()
    result = await alter_system(database, driver, "work_mem", "8MB", dry_run=True)
    assert result.status == "dry_run"
    database.run_unmanaged.assert_not_called()


@pytest.mark.asyncio
async def test_alter_system_denylist() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("listen_addresses", setting="*")])
    database = AsyncMock()
    with pytest.raises(GucError, match="denylist"):
        await alter_system(database, driver, "listen_addresses", "127.0.0.1", confirm=True)


@pytest.mark.asyncio
async def test_alter_system_allowlist() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    database = AsyncMock()
    with pytest.raises(GucError, match="ALLOWLIST"):
        await alter_system(
            database,
            driver,
            "work_mem",
            "8MB",
            confirm=True,
            allowlist=frozenset({"shared_buffers"}),
        )


@pytest.mark.asyncio
async def test_alter_system_applies_with_confirm() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem", context="user")])
    database = AsyncMock()
    database.run_unmanaged = AsyncMock()
    plan = await alter_system(database, driver, "work_mem", "8MB", dry_run=True)
    assert plan.confirmation_token
    # confirm without the token (or with a wrong one) must not execute
    for tok in (None, "deadbeef"):
        blocked = await alter_system(
            database, driver, "work_mem", "8MB", confirm=True, confirmation_token=tok, reload="none"
        )
        assert blocked.status == "confirmation_required"
    database.run_unmanaged.assert_not_called()
    # a token for a different value does not authorize this one
    other = await alter_system(database, driver, "work_mem", "9MB", dry_run=True)
    wrong = await alter_system(
        database, driver, "work_mem", "8MB", confirm=True, confirmation_token=other.confirmation_token, reload="none"
    )
    assert wrong.status == "confirmation_required"
    result = await alter_system(
        database, driver, "work_mem", "8MB", confirm=True, confirmation_token=plan.confirmation_token, reload="none"
    )
    assert result.status == "applied"
    database.run_unmanaged.assert_called()
    sql = database.run_unmanaged.call_args.args[0]
    assert "ALTER SYSTEM SET" in sql
    assert "work_mem" in sql
    assert "E'8MB'" in sql


@pytest.mark.asyncio
async def test_alter_system_reset_all_confirmation() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[])
    database = AsyncMock()
    result = await alter_system_reset_all(database, driver, confirm=False)
    assert result.status == "confirmation_required"
    assert "RESET ALL" in result.planned_action
    database.run_unmanaged.assert_not_called()


def _info(**kw: Any) -> Any:
    from mcpg.guc import _row_to_info

    return _row_to_info(_row("x", **kw).cells)


def test_secret_settings_are_redacted() -> None:
    from mcpg.guc import _row_to_info

    info = _row_to_info(_row("archive_command", setting="cp %p /secret/%f").cells)
    assert info.setting == "<redacted>"
    assert info.boot_val == "<redacted>"
    assert _row_to_info(_row("work_mem").cells).setting == "4MB"
    assert _row_to_info(_row("my_ext.api_password", setting="hunter2").cells).setting == "<redacted>"


def test_validate_value_checks_metadata() -> None:
    from mcpg.guc import GucInfo

    def mk(vartype: str, **kw: Any) -> GucInfo:
        base: dict[str, Any] = {
            "name": "g",
            "setting": None,
            "unit": None,
            "category": None,
            "short_desc": None,
            "context": "user",
            "vartype": vartype,
            "source": None,
            "pending_restart": False,
            "boot_val": None,
            "reset_val": None,
            "min_val": None,
            "max_val": None,
            "enumvals": None,
            "activation": "reload",
            "alterable_via_alter_system": True,
            "detail": "",
        }
        base.update(kw)
        return GucInfo(**base)

    _validate_value(mk("bool"), "on")
    with pytest.raises(GucError, match="boolean"):
        _validate_value(mk("bool"), "maybe")
    _validate_value(mk("enum", enumvals=["a", "b"]), "A")
    with pytest.raises(GucError, match="allowed"):
        _validate_value(mk("enum", enumvals=["a", "b"]), "c")
    _validate_value(mk("integer", min_val="1", max_val="10"), "5")
    _validate_value(mk("integer", min_val="1", max_val="10"), "64MB")  # unit suffix: left to PostgreSQL
    with pytest.raises(GucError, match="range"):
        _validate_value(mk("integer", min_val="1", max_val="10"), "99")


@pytest.mark.asyncio
async def test_dry_run_rejects_invalid_value() -> None:
    driver = AsyncMock()
    row = _row("enable_seqscan")
    row.cells["vartype"] = "bool"
    driver.execute_query = AsyncMock(return_value=[row])
    with pytest.raises(GucError, match="boolean"):
        await alter_system(AsyncMock(), driver, "enable_seqscan", "perhaps", dry_run=True)


def _names(*names: str) -> list[MagicMock]:
    out = []
    for n in names:
        m = MagicMock()
        m.cells = {"name": n}
        out.append(m)
    return out


@pytest.mark.asyncio
async def test_reset_all_enforces_policy_on_existing_overrides() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=_names("archive_command"))
    with pytest.raises(GucError, match="denylist"):
        await alter_system_reset_all(AsyncMock(), driver, dry_run=True)
    driver.execute_query = AsyncMock(return_value=_names("work_mem"))
    with pytest.raises(GucError, match="ALLOWLIST"):
        await alter_system_reset_all(AsyncMock(), driver, dry_run=True, allowlist=frozenset({"shared_buffers"}))


@pytest.mark.asyncio
async def test_reset_all_fails_closed_when_overrides_unreadable() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(side_effect=RuntimeError("permission denied for view pg_file_settings"))
    database = AsyncMock()
    with pytest.raises(GucError, match="cannot list"):
        await alter_system_reset_all(database, driver, confirm=True, confirmation_token="x")
    database.run_unmanaged.assert_not_called()


@pytest.mark.asyncio
async def test_reset_all_token_binds_to_override_set() -> None:
    driver = AsyncMock()
    database = AsyncMock()
    driver.execute_query = AsyncMock(return_value=_names("work_mem"))
    plan = await alter_system_reset_all(database, driver, dry_run=True)
    driver.execute_query = AsyncMock(return_value=_names("work_mem", "random_page_cost"))  # someone added one
    stale = await alter_system_reset_all(database, driver, confirm=True, confirmation_token=plan.confirmation_token)
    assert stale.status == "confirmation_required"
    database.run_unmanaged.assert_not_called()


def _ok(*, value: bool) -> list[MagicMock]:
    m = MagicMock()
    m.cells = {"ok": value}
    return [m]


@pytest.mark.asyncio
async def test_reload_false_is_reported_as_failure() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=_ok(value=False))
    attempted, succeeded, activation = await _maybe_reload(driver, reload="always", context="sighup")
    assert (attempted, succeeded, activation) == (True, False, "reload_failed")
    with pytest.raises(GucError, match="returned false"):
        await reload_config(driver)


@pytest.mark.asyncio
async def test_reload_always_is_honoured_for_postmaster_context() -> None:
    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=_ok(value=True))
    attempted, succeeded, activation = await _maybe_reload(driver, reload="always", context="postmaster")
    assert (attempted, succeeded, activation) == (True, True, "restart_required")
    attempted, _, activation = await _maybe_reload(driver, reload="if_needed", context="postmaster")
    assert (attempted, activation) == (False, "restart_required")


@pytest.mark.asyncio
async def test_applied_result_survives_follow_up_failure() -> None:
    driver = AsyncMock()
    database = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    plan = await alter_system(database, driver, "work_mem", "8MB", dry_run=True)
    driver.execute_query = AsyncMock(side_effect=[[_row("work_mem")], RuntimeError("boom")])
    result = await alter_system(
        database, driver, "work_mem", "8MB", confirm=True, confirmation_token=plan.confirmation_token, reload="none"
    )
    assert result.status == "applied"
    assert "follow-up inspection failed" in result.detail


@pytest.mark.asyncio
async def test_unmanaged_call_times_out() -> None:
    import asyncio

    async def hang(_sql: str) -> None:
        await asyncio.sleep(5)

    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=[_row("work_mem")])
    database = AsyncMock()
    database.run_unmanaged = hang
    plan = await alter_system(database, driver, "work_mem", "8MB", dry_run=True)
    with pytest.raises(GucError, match="timed out"):
        await alter_system(
            database,
            driver,
            "work_mem",
            "8MB",
            confirm=True,
            confirmation_token=plan.confirmation_token,
            timeout_s=0.05,
        )


def test_tool_helpers_reject_bad_reload_and_tenancy() -> None:
    from mcpg.config import Settings
    from mcpg.guc_tools import _reload_mode, _require_untenanted

    assert _reload_mode("always") == "always"
    with pytest.raises(GucError, match="reload must be"):
        _reload_mode("alwayz")
    _require_untenanted(Settings(database_url="postgresql://u@h/d"))
    with pytest.raises(GucError, match="MCPG_DEFAULT_ROLE"):
        _require_untenanted(Settings(database_url="postgresql://u@h/d", default_role="app"))

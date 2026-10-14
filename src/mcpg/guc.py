"""Persistent GUC changes via ``ALTER SYSTEM`` (MCP tools, not ``run_ddl``).

``ALTER SYSTEM`` writes ``postgresql.auto.conf``, cannot run inside a
transaction block, and is instance-scoped. That is a different hazard
class from table DDL, so these helpers sit behind
``MCPG_ALLOW_ALTER_SYSTEM`` and use :meth:`Database.run_unmanaged`
(autocommit) rather than the transactional ``run_ddl`` allowlist.

Write tools require a second call with ``confirm=true`` after the agent
has surfaced a structured warning to the user.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from mcpg.database import Database
from mcpg.errors import MCPgError
from mcpg.sql import SqlDriver

# Foot-guns: changing these via agents can brick connectivity or preload.
_DEFAULT_DENYLIST: frozenset[str] = frozenset(
    {
        "listen_addresses",
        "port",
        "unix_socket_directories",
        "unix_socket_group",
        "unix_socket_permissions",
        "data_directory",
        "config_file",
        "hba_file",
        "ident_file",
        "external_pid_file",
        "ssl",
        "ssl_cert_file",
        "ssl_key_file",
        "ssl_ca_file",
        "ssl_crl_file",
        "shared_preload_libraries",
        "local_preload_libraries",
        "session_preload_libraries",
        "dynamic_library_path",
        # Settings that make the server run OS commands or load libraries.
        "archive_command",
        "archive_library",
        "archive_cleanup_command",
        "restore_command",
        "recovery_end_command",
        "ssl_passphrase_command",
        "jit_provider",
        # Replication / connection strings (may embed credentials).
        "primary_conninfo",
        "primary_slot_name",
    }
)

# Values of these (and any name matching _SECRETISH_RE) are redacted by inspect_guc.
_REDACTED_NAMES: frozenset[str] = frozenset(
    {
        "archive_command",
        "archive_cleanup_command",
        "restore_command",
        "recovery_end_command",
        "ssl_passphrase_command",
        "primary_conninfo",
    }
)
_SECRETISH_RE = re.compile(r"(password|passphrase|secret|conninfo|_command$)", re.IGNORECASE)
_REDACTED = "<redacted>"

_DEFAULT_TIMEOUT_S = 30.0

# GUC names: dotted custom-variable classes allowed (auto_explain.*).
_GUC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

ReloadMode = Literal["none", "if_needed", "always"]
Activation = Literal[
    "immediate",
    "reload_applied",
    "reload_failed",
    "reload_skipped",
    "restart_required",
    "not_applied",
    "confirmation_required",
    "dry_run",
]


class GucError(MCPgError):
    """Raised when a GUC inspect/alter request is rejected or fails."""


@dataclass(frozen=True)
class GucInfo:
    """Live metadata for one setting from ``pg_settings``."""

    name: str
    setting: str | None
    unit: str | None
    category: str | None
    short_desc: str | None
    context: str | None
    vartype: str | None
    source: str | None
    pending_restart: bool
    boot_val: str | None
    reset_val: str | None
    min_val: str | None
    max_val: str | None
    enumvals: list[str] | None
    activation: str
    alterable_via_alter_system: bool
    detail: str


@dataclass(frozen=True)
class AlterSystemResult:
    """Outcome of ``alter_system`` / ``alter_system_reset_all`` / dry-run / confirm gate."""

    status: str
    name: str | None
    previous_setting: str | None
    requested_value: str | None
    live_setting_after: str | None
    activation: Activation
    pending_restart: bool
    reload_attempted: bool
    reload_succeeded: bool | None
    planned_action: str
    warning: str | None
    detail: str
    confirmation_token: str | None = None


@dataclass(frozen=True)
class ReloadConfigResult:
    """Outcome of ``pg_reload_conf()``."""

    reloaded: bool
    pending_restart_settings: list[str]
    detail: str


def _validate_guc_name(name: str) -> str:
    if not name or not name.strip():
        raise GucError("invalid GUC name: must not be empty")
    name = name.strip()
    if "\x00" in name:
        raise GucError(f"invalid GUC name: {name!r} contains a NUL byte")
    if len(name) > 63 * 3:
        raise GucError(f"invalid GUC name: {name!r} is too long")
    if not _GUC_NAME_RE.match(name):
        raise GucError(
            f"invalid GUC name: {name!r}; expected identifiers optionally "
            "joined by dots (e.g. work_mem or auto_explain.log_min_duration)"
        )
    # GUC names are case-insensitive; pg_settings stores them lowercase.
    return name.lower()


def _quote_ident_path(name: str) -> str:
    """Quote each dotted component for safe inclusion in ALTER SYSTEM."""
    parts = name.split(".")
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def _quote_literal(value: str) -> str:
    """Quote ``value`` as an escape-string literal.

    ``E'...'`` always interprets backslashes, so doubling backslashes and
    single quotes is correct regardless of ``standard_conforming_strings``.
    """
    if "\x00" in value:
        raise GucError("invalid value: contains a NUL byte")
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _confirmation_token(planned: str) -> str:
    """Stateless token binding a confirmation to the exact planned statement."""
    return hashlib.sha256(planned.encode("utf-8")).hexdigest()[:16]


_BOOL_VALUES = frozenset({"on", "off", "true", "false", "yes", "no", "1", "0", "t", "f", "y", "n"})
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _validate_value(info: GucInfo, value: str) -> None:
    """Cheap pre-flight check of ``value`` against ``pg_settings`` metadata."""
    if len(value) > 4096:
        raise GucError("invalid value: too long")
    vartype = (info.vartype or "").lower()
    if vartype == "bool" and value.strip().lower() not in _BOOL_VALUES:
        raise GucError(f"invalid value {value!r} for boolean GUC {info.name!r}")
    if vartype == "enum" and info.enumvals and value.strip().lower() not in {e.lower() for e in info.enumvals}:
        raise GucError(f"invalid value {value!r} for {info.name!r}; allowed: {', '.join(info.enumvals)}")
    if vartype in {"integer", "real"} and _NUMBER_RE.match(value.strip()):
        number = float(value)
        try:
            lo = float(info.min_val) if info.min_val is not None else None
            hi = float(info.max_val) if info.max_val is not None else None
        except ValueError:
            return
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            raise GucError(f"value {value!r} for {info.name!r} is outside the allowed range [{lo}, {hi}]")


def _activation_for_context(context: str | None) -> str:
    if context is None:
        return "unknown"
    ctx = context.lower()
    if ctx == "internal":
        return "not_alterable"
    if ctx == "postmaster":
        return "restart_required"
    if ctx in {"sighup", "superuser-backend", "backend", "user", "superuser"}:
        return "reload"
    return "reload"


def _check_policy(
    name: str,
    *,
    allowlist: frozenset[str] | None,
    denylist: frozenset[str],
) -> None:
    lowered = name.lower()
    if allowlist is not None and lowered not in {a.lower() for a in allowlist}:
        raise GucError(
            f"GUC {name!r} is not on MCPG_ALTER_SYSTEM_ALLOWLIST; "
            "add it to the allowlist or clear the allowlist to use the denylist only"
        )
    if lowered in {d.lower() for d in denylist}:
        raise GucError(
            f"GUC {name!r} is blocked by the ALTER SYSTEM denylist "
            "(connectivity / preload foot-gun); remove it from "
            "MCPG_ALTER_SYSTEM_DENYLIST only if you accept the risk"
        )


async def _fetch_guc_row(driver: SqlDriver, name: str, *, primary: bool = False) -> dict[str, Any] | None:
    rows = await driver.execute_query(
        "SELECT name, setting, unit, category, short_desc, context, vartype, "
        "source, pending_restart, boot_val, reset_val, min_val, max_val, enumvals "
        "FROM pg_settings WHERE name = %s",
        [name],
        force_readonly=not primary,
    )
    if not rows:
        return None
    return dict(rows[0].cells)


def _row_to_info(row: dict[str, Any]) -> GucInfo:
    context = row.get("context")
    activation = _activation_for_context(context if isinstance(context, str) else None)
    alterable = context != "internal"
    enumvals = row.get("enumvals")
    if enumvals is not None and not isinstance(enumvals, list):
        enumvals = list(enumvals) if enumvals else None
    pending = bool(row.get("pending_restart"))
    redact = str(row.get("name", "")).lower() in _REDACTED_NAMES or bool(_SECRETISH_RE.search(str(row.get("name", ""))))

    def _val(key: str) -> str | None:
        raw = row.get(key)
        if raw is None:
            return None
        return _REDACTED if redact and raw != "" else str(raw)

    detail = f"context={context}; activation={activation}; pending_restart={pending}; source={row.get('source')}"
    return GucInfo(
        name=str(row.get("name")),
        setting=_val("setting"),
        unit=None if row.get("unit") is None else str(row.get("unit")),
        category=None if row.get("category") is None else str(row.get("category")),
        short_desc=None if row.get("short_desc") is None else str(row.get("short_desc")),
        context=None if context is None else str(context),
        vartype=None if row.get("vartype") is None else str(row.get("vartype")),
        source=None if row.get("source") is None else str(row.get("source")),
        pending_restart=pending,
        boot_val=_val("boot_val"),
        reset_val=_val("reset_val"),
        min_val=None if row.get("min_val") is None else str(row.get("min_val")),
        max_val=None if row.get("max_val") is None else str(row.get("max_val")),
        enumvals=enumvals,
        activation=activation,
        alterable_via_alter_system=alterable,
        detail=detail,
    )


async def inspect_guc(driver: SqlDriver, name: str, *, primary: bool = False) -> GucInfo:
    """Return live ``pg_settings`` metadata for one GUC.

    Reads may be served by a replica when replica routing is configured;
    write paths pass ``primary=True``. Values of command / connection-string
    / password-like settings are redacted.
    """
    name = _validate_guc_name(name)
    row = await _fetch_guc_row(driver, name, primary=primary)
    if row is None:
        raise GucError(f"unknown GUC {name!r}; not present in pg_settings on this server")
    return _row_to_info(row)


def _warning_text(*, action: str, name: str | None, activation: str) -> str:
    base = (
        "This persists cluster configuration via ALTER SYSTEM into "
        "postgresql.auto.conf. It is NOT transactional (cannot ROLLBACK). "
        "Some settings apply after pg_reload_conf(); postmaster-context "
        "settings need a full PostgreSQL restart. Incorrect values can "
        "break connectivity or prevent startup. Managed/hosted Postgres "
        "may forbid ALTER SYSTEM entirely. "
        f"Planned action: {action}."
    )
    if activation == "restart_required":
        base += " This GUC is postmaster-context: a restart will be required for the live value to change."
    if name is None:
        base += " RESET ALL clears every prior ALTER SYSTEM override (not postgresql.conf)."
    return base


async def _run_unmanaged(database: Database, sql: str, timeout_s: float) -> None:
    """``Database.run_unmanaged`` bounded by a timeout (it applies none itself)."""
    try:
        await asyncio.wait_for(database.run_unmanaged(sql), timeout=timeout_s)
    except TimeoutError as exc:
        raise GucError(f"timed out after {timeout_s:g}s running: {sql}") from exc


async def _pg_reload_conf(driver: SqlDriver) -> bool:
    """Run ``pg_reload_conf()`` on the primary; return the boolean it reports."""
    rows = await driver.execute_query("SELECT pg_reload_conf() AS ok", force_readonly=False)
    return bool(rows and rows[0].cells.get("ok"))


async def _maybe_reload(
    driver: SqlDriver,
    *,
    reload: ReloadMode,
    context: str | None,
) -> tuple[bool, bool | None, Activation]:
    needs_reload = _activation_for_context(context) == "reload"
    needs_restart = _activation_for_context(context) == "restart_required"
    settled: Activation = "restart_required" if needs_restart else "immediate"

    if reload == "none":
        return False, None, "reload_skipped" if needs_reload else settled
    if reload == "if_needed" and not needs_reload:
        return False, None, settled

    try:
        signalled = await _pg_reload_conf(driver)
    except Exception:
        return True, False, "reload_failed"
    if not signalled:
        return True, False, "reload_failed"
    return True, True, "restart_required" if needs_restart else "reload_applied"


def _unconfirmed(
    *,
    planned: str,
    warning: str,
    token: str,
    name: str | None,
    info: GucInfo | None,
    requested: str | None,
    detail: str,
) -> AlterSystemResult:
    return AlterSystemResult(
        status="confirmation_required",
        name=name,
        previous_setting=info.setting if info else None,
        requested_value=requested,
        live_setting_after=info.setting if info else None,
        activation="confirmation_required",
        pending_restart=info.pending_restart if info else False,
        reload_attempted=False,
        reload_succeeded=None,
        planned_action=planned,
        warning=warning,
        detail=detail,
        confirmation_token=token,
    )


_NEXT_STEP = "Call again with dry_run=false, confirm=true and confirmation_token to apply."


async def alter_system(
    database: Database,
    driver: SqlDriver,
    name: str,
    value: str | None,
    *,
    reload: ReloadMode = "if_needed",
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
    allowlist: frozenset[str] | None = None,
    denylist: frozenset[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> AlterSystemResult:
    """SET or RESET one GUC via ``ALTER SYSTEM`` on an autocommit connection.

    ``value is None`` means ``ALTER SYSTEM RESET name``. Executing needs both
    ``confirm=true`` and the ``confirmation_token`` returned with the
    ``confirmation_required`` / ``dry_run`` result for this exact statement.
    """
    if reload not in ("none", "if_needed", "always"):
        raise GucError(f"reload must be one of none, if_needed, always; got {reload!r}")

    name = _validate_guc_name(name)
    deny = _DEFAULT_DENYLIST | (denylist or frozenset())
    _check_policy(name, allowlist=allowlist, denylist=deny)

    info = await inspect_guc(driver, name, primary=True)
    if not info.alterable_via_alter_system:
        raise GucError(f"GUC {name!r} has context 'internal' and cannot be changed with ALTER SYSTEM")

    quoted = _quote_ident_path(name)
    if value is None:
        planned = f"ALTER SYSTEM RESET {quoted}"
        requested = None
    else:
        _validate_value(info, value)
        planned = f"ALTER SYSTEM SET {quoted} = {_quote_literal(value)}"
        requested = value

    token = _confirmation_token(planned)
    warning = _warning_text(action=planned, name=name, activation=info.activation)

    if dry_run:
        return AlterSystemResult(
            status="dry_run",
            name=name,
            previous_setting=info.setting,
            requested_value=requested,
            live_setting_after=info.setting,
            activation="dry_run",
            pending_restart=info.pending_restart,
            reload_attempted=False,
            reload_succeeded=None,
            planned_action=planned,
            warning=warning,
            detail=f"No changes applied (dry_run=true). {_NEXT_STEP}",
            confirmation_token=token,
        )

    if not confirm or confirmation_token != token:
        mismatch = "confirmation_token does not match this action. " if confirm and confirmation_token else ""
        return _unconfirmed(
            planned=planned,
            warning=warning,
            token=token,
            name=name,
            info=info,
            requested=requested,
            detail=(
                f"No changes applied. {mismatch}Show the warning to the user, then call again "
                "with confirm=true and this confirmation_token to execute."
            ),
        )

    try:
        await _run_unmanaged(database, planned, timeout_s)
    except GucError:
        raise
    except Exception as exc:
        raise GucError(f"ALTER SYSTEM failed: {exc}") from exc

    reload_attempted, reload_succeeded, activation = await _maybe_reload(driver, reload=reload, context=info.context)

    live_after: str | None = None
    pending_after = info.pending_restart
    note = ""
    try:
        after = await inspect_guc(driver, name, primary=True)
        live_after, pending_after = after.setting, after.pending_restart
    except Exception as exc:  # the write already happened; do not report failure
        note = f" (follow-up inspection failed: {exc})"
    return AlterSystemResult(
        status="applied",
        name=name,
        previous_setting=info.setting,
        requested_value=requested,
        live_setting_after=live_after,
        activation=activation,
        pending_restart=pending_after,
        reload_attempted=reload_attempted,
        reload_succeeded=reload_succeeded,
        planned_action=planned,
        warning=None,
        detail=f"Wrote postgresql.auto.conf via {planned}. activation={activation}; pending_restart={pending_after}."
        + note,
    )


async def _auto_conf_overrides(driver: SqlDriver) -> list[str]:
    """Names currently overridden in ``postgresql.auto.conf`` (needs pg_file_settings access)."""
    rows = await driver.execute_query(
        "SELECT DISTINCT name FROM pg_file_settings WHERE right(sourcefile, 20) = 'postgresql.auto.conf' ORDER BY name",
        force_readonly=False,
    )
    return [str(r.cells["name"]) for r in (rows or [])]


async def _reload_after_reset(driver: SqlDriver, reload: ReloadMode) -> tuple[bool, bool | None, Activation]:
    if reload == "none":
        return False, None, "reload_skipped"
    try:
        ok = await _pg_reload_conf(driver)
    except Exception:
        ok = False
    return True, ok, "reload_applied" if ok else "reload_failed"


async def alter_system_reset_all(
    database: Database,
    driver: SqlDriver,
    *,
    reload: ReloadMode = "if_needed",
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
    allowlist: frozenset[str] | None = None,
    denylist: frozenset[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> AlterSystemResult:
    """``ALTER SYSTEM RESET ALL`` — clears every prior ALTER SYSTEM override.

    The allow/deny policy is enforced against every name currently overridden
    in ``postgresql.auto.conf``; if the overrides cannot be listed the call
    fails closed. The confirmation token covers the current override set, so a
    retry after the set changed asks for confirmation again.
    """
    if reload not in ("none", "if_needed", "always"):
        raise GucError(f"reload must be one of none, if_needed, always; got {reload!r}")

    deny = _DEFAULT_DENYLIST | (denylist or frozenset())
    try:
        overrides = await _auto_conf_overrides(driver)
    except Exception as exc:
        raise GucError(f"cannot list postgresql.auto.conf overrides to apply the ALTER SYSTEM policy: {exc}") from exc
    for override in overrides:
        _check_policy(override, allowlist=allowlist, denylist=deny)

    planned = "ALTER SYSTEM RESET ALL"
    token = _confirmation_token(planned + "|" + ",".join(overrides))
    warning = _warning_text(action=planned, name=None, activation="reload")
    if overrides:
        warning += f" Overrides that will be cleared: {', '.join(overrides)}."

    if dry_run:
        return AlterSystemResult(
            status="dry_run",
            name=None,
            previous_setting=None,
            requested_value=None,
            live_setting_after=None,
            activation="dry_run",
            pending_restart=False,
            reload_attempted=False,
            reload_succeeded=None,
            planned_action=planned,
            warning=warning,
            detail=f"No changes applied (dry_run=true). {_NEXT_STEP}",
            confirmation_token=token,
        )

    if not confirm or confirmation_token != token:
        mismatch = "confirmation_token does not match the current overrides. " if confirm and confirmation_token else ""
        return _unconfirmed(
            planned=planned,
            warning=warning,
            token=token,
            name=None,
            info=None,
            requested=None,
            detail=(
                f"No changes applied. {mismatch}Show the warning to the user, then call again "
                "with confirm=true and this confirmation_token to execute RESET ALL."
            ),
        )

    try:
        await _run_unmanaged(database, planned, timeout_s)
    except GucError:
        raise
    except Exception as exc:
        raise GucError(f"ALTER SYSTEM RESET ALL failed: {exc}") from exc

    reload_attempted, reload_succeeded, activation = await _reload_after_reset(driver, reload)

    pending = False
    note = ""
    try:
        pending_rows = await driver.execute_query(
            "SELECT name FROM pg_settings WHERE pending_restart ORDER BY name",
            force_readonly=False,
        )
        pending = bool(pending_rows)
    except Exception as exc:  # the reset already happened; do not report failure
        note = f" (follow-up inspection failed: {exc})"

    return AlterSystemResult(
        status="applied",
        name=None,
        previous_setting=None,
        requested_value=None,
        live_setting_after=None,
        activation=activation,
        pending_restart=pending,
        reload_attempted=reload_attempted,
        reload_succeeded=reload_succeeded,
        planned_action=planned,
        warning=None,
        detail=(
            "Cleared all ALTER SYSTEM overrides in postgresql.auto.conf. "
            f"activation={activation}; pending_restart_any={pending}."
        )
        + note,
    )


async def reload_config(driver: SqlDriver) -> ReloadConfigResult:
    """Call ``pg_reload_conf()`` and list settings still pending restart."""
    try:
        signalled = await _pg_reload_conf(driver)
    except Exception as exc:
        raise GucError(f"pg_reload_conf() failed: {exc}") from exc
    if not signalled:
        raise GucError("pg_reload_conf() returned false: the postmaster was not signalled")
    detail = "pg_reload_conf() completed."

    pending: list[str] = []
    try:
        rows = await driver.execute_query(
            "SELECT name FROM pg_settings WHERE pending_restart ORDER BY name",
            force_readonly=False,
        )
        pending = [str(r.cells["name"]) for r in (rows or [])]
    except Exception as exc:  # the reload already happened
        detail += f" (could not list pending restarts: {exc})"
    if pending:
        detail += f" {len(pending)} setting(s) still require a restart: {', '.join(pending[:20])}."
    return ReloadConfigResult(reloaded=True, pending_restart_settings=pending, detail=detail)


def parse_name_list(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated GUC name list from env."""
    if not raw or not raw.strip():
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())

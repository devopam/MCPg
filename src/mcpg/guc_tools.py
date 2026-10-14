"""MCP tool registration for ALTER SYSTEM / GUC helpers.

Separated from :mod:`mcpg.tools` so the large tools module only needs a
few call sites. Wired from :func:`mcpg.tools.register_tools`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from mcpg import guc
from mcpg.config import Settings
from mcpg.context import AppContext

_Ctx = Context[AppContext, Any]


def _reload_mode(reload: str) -> guc.ReloadMode:
    """Validate the tool's free-form ``reload`` arg."""
    if reload == "none":
        return "none"
    if reload == "always":
        return "always"
    if reload == "if_needed":
        return "if_needed"
    raise guc.GucError(f"reload must be one of none, if_needed, always; got {reload!r}")


def _require_untenanted(settings: Settings) -> None:
    """ALTER SYSTEM runs on the raw pool connection, bypassing tenant roles — refuse under tenancy."""
    if settings.default_role is not None or settings.allowed_roles:
        raise guc.GucError(
            "ALTER SYSTEM tools are disabled when MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES is set: "
            "they run as the connection login and would bypass per-role privilege separation"
        )


def register_guc_reads(server: MCPServer[AppContext]) -> None:
    """Register read-only ``inspect_guc`` (available under the READ gate)."""

    @server.tool(
        name="inspect_guc",
        description=(
            "Inspect one PostgreSQL GUC via pg_settings: live value, unit, "
            "context (postmaster/sighup/user/…), pending_restart, vartype, "
            "and whether ALTER SYSTEM can change it. Use this before "
            "alter_system to see whether a reload or restart is required. "
            "Read-only."
        ),
    )
    async def inspect_guc(ctx: _Ctx, name: str) -> guc.GucInfo:
        driver = ctx.request_context.lifespan_context.database.driver()
        return await guc.inspect_guc(driver, name)


def register_guc_writes(server: MCPServer[AppContext]) -> None:
    """Register ALTER SYSTEM tools (unrestricted + MCPG_ALLOW_ALTER_SYSTEM)."""

    def _policy(ctx: _Ctx) -> tuple[frozenset[str] | None, frozenset[str]]:
        settings: Settings = ctx.request_context.lifespan_context.settings
        allow = frozenset(settings.alter_system_allowlist) if settings.alter_system_allowlist else None
        deny = frozenset(settings.alter_system_denylist)
        _require_untenanted(settings)
        return allow, deny

    def _timeout_s(ctx: _Ctx) -> float:
        return ctx.request_context.lifespan_context.settings.statement_timeout_ms / 1000

    @server.tool(
        name="alter_system",
        description=(
            "Persist a PostgreSQL setting with ALTER SYSTEM (writes "
            "postgresql.auto.conf). NOT transactional — cannot ROLLBACK. "
            "Structured args only (no free-form SQL): name is the GUC, "
            "value is the new setting or null to RESET that name. "
            "reload is none|if_needed|always (default if_needed). "
            "dry_run=true validates and returns a plan without writing. "
            "Without confirm=true the tool returns status=confirmation_required "
            "with a warning and a confirmation_token — show the warning to "
            "the user, then call again with confirm=true and that exact "
            "confirmation_token (bound to this statement) to apply. Postmaster-context GUCs need a "
            "restart; sighup-class GUCs can use pg_reload_conf(). A built-in "
            "denylist blocks connectivity/preload foot-guns. Requires "
            "MCPG_ACCESS_MODE=unrestricted and MCPG_ALLOW_ALTER_SYSTEM=true. "
            "Does not extend run_ddl. The DB connection user must be a "
            "superuser (or otherwise allowed to ALTER SYSTEM)."
        ),
    )
    async def alter_system(
        ctx: _Ctx,
        name: str,
        value: str | None = None,
        reload: str = "if_needed",
        *,
        dry_run: bool = False,
        confirm: bool = False,
        confirmation_token: str | None = None,
    ) -> guc.AlterSystemResult:
        app = ctx.request_context.lifespan_context
        allow, deny = _policy(ctx)
        mode = _reload_mode(reload)
        result = await guc.alter_system(
            app.database,
            app.database.driver(),
            name,
            value,
            reload=mode,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
            allowlist=allow,
            denylist=deny,
            timeout_s=_timeout_s(ctx),
        )
        if result.status == "applied":
            await app.cache.clear()
        return result

    @server.tool(
        name="alter_system_reset_all",
        description=(
            "ALTER SYSTEM RESET ALL — clears every setting previously written "
            "via ALTER SYSTEM (postgresql.auto.conf), not postgresql.conf and "
            "not session SET. Instance-wide and NOT transactional. Without "
            "confirm=true returns confirmation_required + a warning and a "
            "confirmation_token (covers the current override set); call again "
            "with confirm=true and that token to apply. The allow/deny policy "
            "is enforced against every current override. reload defaults to "
            "if_needed. Requires unrestricted mode and MCPG_ALLOW_ALTER_SYSTEM. "
            "The DB connection user must be a superuser."
        ),
    )
    async def alter_system_reset_all(
        ctx: _Ctx,
        reload: str = "if_needed",
        *,
        dry_run: bool = False,
        confirm: bool = False,
        confirmation_token: str | None = None,
    ) -> guc.AlterSystemResult:
        app = ctx.request_context.lifespan_context
        allow, deny = _policy(ctx)
        mode = _reload_mode(reload)
        result = await guc.alter_system_reset_all(
            app.database,
            app.database.driver(),
            reload=mode,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
            allowlist=allow,
            denylist=deny,
            timeout_s=_timeout_s(ctx),
        )
        if result.status == "applied":
            await app.cache.clear()
        return result

    @server.tool(
        name="reload_config",
        description=(
            "Call pg_reload_conf() so sighup-class settings written with "
            "ALTER SYSTEM (or edit of config files) take effect without a "
            "full restart. Returns whether reload succeeded and any settings "
            "still marked pending_restart. Requires unrestricted mode and "
            "MCPG_ALLOW_ALTER_SYSTEM."
        ),
    )
    async def reload_config(ctx: _Ctx) -> guc.ReloadConfigResult:
        app = ctx.request_context.lifespan_context
        _require_untenanted(app.settings)
        return await guc.reload_config(app.database.driver())

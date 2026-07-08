"""PG 19 ``WAIT FOR LSN`` — read-your-writes (RYW) consistency on hot standbys.

PG 19 introduces ``WAIT FOR LSN '<lsn>' [TIMEOUT <ms>]`` — a
server-side wait that blocks the current backend until WAL replay
catches up to the supplied LSN. Combined with capturing
``pg_current_wal_lsn()`` on the primary right after a write, this
turns "read your own writes from a hot standby" from a hand-rolled
poll loop into a one-call primitive.

Module surface (four tools):

* ``get_wait_for_lsn_status`` — version probe; never raises. Reports
  whether the SQL form is usable on this server + whether the current
  backend is a standby (the only context where WAIT FOR LSN does
  meaningful work).
* ``get_current_wal_lsn`` — returns ``pg_current_wal_lsn()`` on a
  primary or ``pg_last_wal_replay_lsn()`` on a standby. Works on every
  supported PG version; bracketed in this module because it's the
  natural pairing with WAIT FOR LSN for the RYW workflow.
* ``wait_for_lsn`` — issues ``WAIT FOR LSN '<lsn>' TIMEOUT <ms>``.
  Strict LSN format validation up-front; embeds the literal verbatim
  because the grammar can't parameter-bind LSN literals.
* ``recommend_read_your_writes`` — advisor that combines server-role
  + lag analysis + version detection and returns a structured
  recommendation: should the caller use WAIT FOR LSN, and with what
  bounds.

Backward compatibility
----------------------
Additive. ``get_current_wal_lsn`` and the advisor work on every PG
version (the advisor returns ``available=False`` with a guidance
string when the server is < 19). ``wait_for_lsn`` raises
``WaitForLsnError`` on PG ≤ 18 with the documented poll-loop fallback
in the message.

Security posture
----------------
* LSN values pass through a strict format check (``^[0-9A-Fa-f]+/[0-9A-Fa-f]+$``).
  Anything else raises ``WaitForLsnError`` before any SQL is composed.
  PG's WAIT FOR LSN grammar takes a string literal, not a bound
  parameter — strict validation lets us safely embed verbatim.
* ``timeout_ms`` is coerced to ``int`` and bounded to >= 0; non-int
  inputs raise.
* All reads are parameter-free pure SQL; the wait dispatches through
  the regular driver (no DDL / autocommit requirement).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg.sql import SqlDriver

# PG 19 ships WAIT FOR LSN. The version-num boundary.
_MIN_PG19_WAIT_VERSION = 190000

# PostgreSQL LSN literal: two hex segments separated by '/'.
# `pg_lsn` accepts 1-8 hex digits per segment but the canonical form is
# 1-16; the regex is intentionally permissive within hex+/+hex.
_LSN_PATTERN = re.compile(r"^[0-9A-Fa-f]+/[0-9A-Fa-f]+$")


class WaitForLsnError(Exception):
    """Raised when a WAIT FOR LSN request is rejected or fails."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaitForLsnStatus:
    """Reports whether PG 19's WAIT FOR LSN is usable on this server.

    ``available`` is True when ``server_version_num`` >= 190000.
    ``is_in_recovery`` is True when the current backend is a standby —
    the only context where WAIT FOR LSN meaningfully waits. On a
    primary the SQL is accepted but trivially returns (the LSN is
    always reached). ``detail`` is the agent-facing guidance string.
    """

    available: bool
    server_version_num: int
    server_version: str
    is_in_recovery: bool
    detail: str


@dataclass(frozen=True)
class CurrentWalLsnResult:
    """One LSN snapshot — what the helper returned at call time.

    ``role`` is ``"primary"`` or ``"standby"``. The LSN field
    semantically differs between the two: on a primary it's the
    most-recently-flushed write position; on a standby it's the
    most-recently-replayed position.
    """

    role: str
    lsn: str


@dataclass(frozen=True)
class WaitForLsnResult:
    """Outcome of `wait_for_lsn`.

    ``timed_out`` is True when the configured ``timeout_ms`` elapsed
    before WAL replay reached the target LSN — the wait surface
    returns control without an error so the caller can decide what to
    do (retry / fall through / fail). ``wait_sql`` is the rendered
    statement that actually executed.
    """

    lsn: str
    timeout_ms: int
    timed_out: bool
    wait_sql: str


@dataclass(frozen=True)
class ReadYourWritesRecommendation:
    """Advisor output for the RYW workflow.

    ``recommend_use`` is True when the caller would meaningfully
    benefit from WAIT FOR LSN — i.e. the current backend is a standby
    on PG 19+ with non-trivial replay lag. ``reason`` is one of:

    * ``primary_no_wait_needed`` — call landed on the primary; reads
      already see writes.
    * ``standby_no_lag`` — standby with a measured 0-byte replay lag;
      WAIT FOR LSN is harmless but unnecessary.
    * ``standby_lag_unknown`` — standby where the lag probe returned
      NULL or an unparseable value (transient receive-LSN gap,
      pg_wal_lsn_diff() failure, etc.). Distinct from
      ``standby_no_lag`` so the caller can tell "measured zero" from
      "couldn't measure".
    * ``standby_with_lag`` — standby with non-zero replay lag; WAIT
      FOR LSN is the right primitive.
    * ``standby_pg18_or_older`` — standby but server is PG ≤ 18;
      fall back to the poll-loop pattern.
    * ``unavailable`` — version / recovery / lag probe failed; can't tell.
    """

    recommend_use: bool
    reason: str
    is_in_recovery: bool
    server_version_num: int
    current_lag_bytes: int | None
    detail: str


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


async def _is_in_recovery(driver: SqlDriver) -> bool:
    """Return True when the connected backend is a standby."""
    rows = await driver.execute_query(
        "SELECT pg_is_in_recovery() AS in_recovery",
        force_readonly=True,
    )
    if not rows:
        return False
    raw = rows[0].cells.get("in_recovery")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"on", "true", "yes", "t", "1"}


def _validate_lsn(lsn: str) -> str:
    """Strict LSN format check. Returns the validated LSN unchanged."""
    if not isinstance(lsn, str) or not _LSN_PATTERN.fullmatch(lsn):
        raise WaitForLsnError(f"invalid LSN format: {lsn!r}; expected hex/hex (e.g. '0/1234ABCD').")
    return lsn


def _validate_timeout_ms(value: int) -> int:
    """Coerce + bound the WAIT FOR LSN timeout."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise WaitForLsnError(f"timeout_ms must be a non-negative int, got bool {value!r}")
    if not isinstance(value, int):
        raise WaitForLsnError(f"timeout_ms must be a non-negative int, got {type(value).__name__}")
    if value < 0:
        raise WaitForLsnError(f"timeout_ms must be >= 0, got {value}")
    return value


# ---------------------------------------------------------------------------
# Status probe
# ---------------------------------------------------------------------------


async def get_wait_for_lsn_status(driver: SqlDriver) -> WaitForLsnStatus:
    """Report whether PG 19's WAIT FOR LSN is usable on this server.

    Read-only; never raises. Reports both the version-num gate and
    whether the connected backend is a standby (where the wait is
    meaningful). On PG ≤ 18 returns ``available=False`` with a
    guidance string pointing at the poll-loop fallback.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return WaitForLsnStatus(
            available=False,
            server_version_num=0,
            server_version="",
            is_in_recovery=False,
            detail=(
                f"WAIT FOR LSN status unavailable (version probe failed: {exc}). "
                "Re-run after the server is back online."
            ),
        )
    try:
        in_recovery = await _is_in_recovery(driver)
    except Exception:
        in_recovery = False
    available = ver_num >= _MIN_PG19_WAIT_VERSION
    if not available:
        detail = (
            "WAIT FOR LSN requires PostgreSQL 19 or newer; this server is older. "
            "Fall back to the poll-loop pattern: capture pg_current_wal_lsn() on "
            "the primary, then poll pg_last_wal_replay_lsn() on the standby until "
            "it catches up."
        )
    elif in_recovery:
        detail = (
            "WAIT FOR LSN is available and the current backend is a standby — "
            "this is the canonical context for the read-your-writes pattern. "
            "Capture the LSN on the primary right after a write, then call "
            "wait_for_lsn() on this connection before the follow-up read."
        )
    else:
        detail = (
            "WAIT FOR LSN is available but the current backend is a primary. "
            "The wait is accepted but trivially returns (LSN is always reached). "
            "Run wait_for_lsn() on a standby session instead."
        )
    return WaitForLsnStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        is_in_recovery=in_recovery,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# get_current_wal_lsn — works on every PG version
# ---------------------------------------------------------------------------


async def get_current_wal_lsn(driver: SqlDriver) -> CurrentWalLsnResult:
    """Return the current WAL LSN — write-side on a primary, replay-side on a standby.

    The natural pairing for the RYW workflow: write on the primary →
    call this → take the result to a standby session and pass it to
    ``wait_for_lsn``.

    Works on every supported PG version. Raises :class:`WaitForLsnError`
    when the recovery probe or the LSN SQL call fails, or when the
    role-appropriate LSN function returns NULL (sourcery bug-risk on
    PR #146: the prior docstring claimed "only when both LSN-getter
    calls fail" while pg_is_in_recovery() exceptions leaked).
    """
    try:
        in_recovery = await _is_in_recovery(driver)
    except Exception as exc:
        raise WaitForLsnError(f"pg_is_in_recovery() probe failed: {exc}") from exc
    func = "pg_last_wal_replay_lsn" if in_recovery else "pg_current_wal_lsn"
    try:
        rows = await driver.execute_query(f"SELECT {func}() AS lsn", force_readonly=True)
    except Exception as exc:
        raise WaitForLsnError(f"{func}() call failed: {exc}") from exc
    if not rows:
        raise WaitForLsnError(f"{func}() returned no rows.")
    raw = rows[0].cells.get("lsn")
    if raw is None:
        raise WaitForLsnError(f"{func}() returned NULL.")
    return CurrentWalLsnResult(
        role="standby" if in_recovery else "primary",
        lsn=str(raw),
    )


# ---------------------------------------------------------------------------
# wait_for_lsn — PG 19+
# ---------------------------------------------------------------------------


async def wait_for_lsn(driver: SqlDriver, *, lsn: str, timeout_ms: int = 0) -> WaitForLsnResult:
    """Issue ``WAIT FOR LSN '<lsn>' TIMEOUT <ms>`` and return the outcome.

    ``timeout_ms`` defaults to 0, which means "wait indefinitely" per
    PG's WAIT FOR LSN semantics. Set it to a positive value for
    bounded waits; on timeout the helper returns
    ``timed_out=True`` rather than raising so the caller can decide
    how to proceed.

    Requires PG 19+. Strict LSN format validation runs before any SQL
    is composed; invalid input raises :class:`WaitForLsnError`.
    """
    lsn = _validate_lsn(lsn)
    timeout_ms = _validate_timeout_ms(timeout_ms)
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_WAIT_VERSION:
        raise WaitForLsnError(
            "WAIT FOR LSN requires PostgreSQL 19 or newer; this server "
            f"reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Fall back to the poll-loop on pg_last_wal_replay_lsn()."
        )
    # Format check already enforced — embed verbatim.
    wait_sql = f"WAIT FOR LSN '{lsn}' TIMEOUT {timeout_ms}"
    try:
        await driver.execute_query(wait_sql, force_readonly=True)
    except Exception as exc:
        # PG raises SQLSTATE 57014 (query_canceled) on TIMEOUT — that's
        # the locale-independent signal and the only reliable one
        # (gemini + sourcery critical on PR #146: substring matching
        # on "timed out" / "timeout" misfires under non-English
        # lc_messages and conflates statement_timeout with the
        # WAIT FOR LSN timeout). The English-message check stays as a
        # belt-and-braces fallback for drivers that don't surface the
        # SQLSTATE attribute.
        sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        msg = str(exc).lower()
        if sqlstate == "57014" or "timed out" in msg or "timeout" in msg:
            return WaitForLsnResult(lsn=lsn, timeout_ms=timeout_ms, timed_out=True, wait_sql=wait_sql)
        raise WaitForLsnError(f"WAIT FOR LSN failed: {exc}") from exc
    return WaitForLsnResult(lsn=lsn, timeout_ms=timeout_ms, timed_out=False, wait_sql=wait_sql)


# ---------------------------------------------------------------------------
# recommend_read_your_writes — advisor
# ---------------------------------------------------------------------------


async def recommend_read_your_writes(driver: SqlDriver) -> ReadYourWritesRecommendation:
    """Advisor — should the caller use WAIT FOR LSN for read-your-writes?

    Read-only; never raises. Returns a structured recommendation with
    one of six ``reason`` codes (see :class:`ReadYourWritesRecommendation`).

    Implementation note: version + recovery + lag are captured in a
    single round-trip SELECT so the advisor cannot mis-classify a
    standby as a primary if the recovery probe transiently fails
    (gemini critical on PR #146 — the prior split-query implementation
    swallowed an `_is_in_recovery` exception, defaulted to
    `in_recovery=False`, and would route the caller to
    `primary_no_wait_needed` on a real standby). The atomic query also
    eliminates the race window between probes during a fail-over.
    """
    try:
        rows = await driver.execute_query(
            "SELECT "
            "  current_setting('server_version_num')::int AS ver_num, "
            "  current_setting('server_version') AS ver, "
            "  pg_is_in_recovery() AS in_recovery, "
            "  CASE WHEN pg_is_in_recovery() "
            "       THEN pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint "
            "       ELSE NULL END AS lag",
            force_readonly=True,
        )
    except Exception as exc:
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="unavailable",
            is_in_recovery=False,
            server_version_num=0,
            current_lag_bytes=None,
            detail=f"Combined probe failed: {exc}. Re-run after the server is back online.",
        )
    if not rows:
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="unavailable",
            is_in_recovery=False,
            server_version_num=0,
            current_lag_bytes=None,
            detail="Combined probe returned no rows.",
        )
    cells = rows[0].cells
    try:
        ver_num = int(cells.get("ver_num") or 0)
    except (TypeError, ValueError):
        ver_num = 0
    ver = str(cells.get("ver") or "")
    raw_in_recovery = cells.get("in_recovery")
    if isinstance(raw_in_recovery, bool):
        in_recovery = raw_in_recovery
    elif raw_in_recovery is None:
        in_recovery = False
    else:
        in_recovery = str(raw_in_recovery).strip().lower() in {"on", "true", "yes", "t", "1"}
    raw_lag = cells.get("lag")
    lag: int | None
    if raw_lag is None:
        lag = None
    else:
        try:
            lag = int(raw_lag)
        except (TypeError, ValueError):
            lag = None

    if not in_recovery:
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="primary_no_wait_needed",
            is_in_recovery=False,
            server_version_num=ver_num,
            current_lag_bytes=None,
            detail=(
                "Connection is to a primary — your reads already see your writes. "
                f"Server version: {ver or 'unknown'}. WAIT FOR LSN is unnecessary in this context."
            ),
        )
    if ver_num < _MIN_PG19_WAIT_VERSION:
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="standby_pg18_or_older",
            is_in_recovery=True,
            server_version_num=ver_num,
            current_lag_bytes=lag,
            detail=(
                f"Standby on PG {ver or 'unknown'} (< 19) — WAIT FOR LSN is unavailable. "
                "Fall back to the poll-loop on pg_last_wal_replay_lsn() until it "
                "catches up to the captured primary LSN."
            ),
        )
    if lag is None:
        # Distinct from `standby_no_lag` — sourcery critical on PR #146:
        # `None` means "couldn't measure" (NULL from pg_wal_lsn_diff,
        # missing receive LSN on a freshly-started standby, …), not
        # "measured zero".
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="standby_lag_unknown",
            is_in_recovery=True,
            server_version_num=ver_num,
            current_lag_bytes=None,
            detail=(
                "Standby but replay lag could not be measured "
                "(pg_wal_lsn_diff returned NULL or pg_last_wal_receive_lsn is unset). "
                "Re-run after the receive LSN is reported; if the symptom persists, "
                "use the RYW pattern conservatively (capture-then-wait) as a precaution."
            ),
        )
    if lag == 0:
        return ReadYourWritesRecommendation(
            recommend_use=False,
            reason="standby_no_lag",
            is_in_recovery=True,
            server_version_num=ver_num,
            current_lag_bytes=0,
            detail=(
                "Standby with a measured 0-byte replay lag — WAIT FOR LSN is harmless "
                "but unnecessary. Re-evaluate during traffic spikes if you start "
                "seeing stale-read reports."
            ),
        )
    return ReadYourWritesRecommendation(
        recommend_use=True,
        reason="standby_with_lag",
        is_in_recovery=True,
        server_version_num=ver_num,
        current_lag_bytes=lag,
        detail=(
            f"Standby with {lag} bytes of replay lag. Use the RYW pattern: "
            "capture pg_current_wal_lsn() on the primary right after the write, "
            "then call wait_for_lsn(lsn=<captured>, timeout_ms=<budget>) on this "
            "connection before the follow-up read."
        ),
    )


__all__ = [
    "CurrentWalLsnResult",
    "ReadYourWritesRecommendation",
    "WaitForLsnError",
    "WaitForLsnResult",
    "WaitForLsnStatus",
    "get_current_wal_lsn",
    "get_wait_for_lsn_status",
    "recommend_read_your_writes",
    "wait_for_lsn",
]

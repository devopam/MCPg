"""Tests for the PG 19 WAIT FOR LSN module."""

from __future__ import annotations

import pytest
from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.wait_for_lsn import (
    CurrentWalLsnResult,
    ReadYourWritesRecommendation,
    WaitForLsnError,
    WaitForLsnResult,
    WaitForLsnStatus,
    get_current_wal_lsn,
    get_wait_for_lsn_status,
    recommend_read_your_writes,
    wait_for_lsn,
)


def _route(num: int, ver: str, *, in_recovery: bool = False) -> dict[str, list[dict[str, object]]]:
    return {
        "current_setting('server_version_num')": [{"ver_num": num, "ver": ver}],
        "pg_is_in_recovery()": [{"in_recovery": in_recovery}],
    }


# --- get_wait_for_lsn_status ---------------------------------------------


async def test_status_available_on_pg19_primary() -> None:
    driver = FakeRoutingDriver(_route(190001, "19beta1", in_recovery=False))
    status = await get_wait_for_lsn_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, WaitForLsnStatus)
    assert status.available is True
    assert status.is_in_recovery is False
    assert "primary" in status.detail


async def test_status_available_on_pg19_standby() -> None:
    driver = FakeRoutingDriver(_route(190001, "19beta1", in_recovery=True))
    status = await get_wait_for_lsn_status(driver)  # type: ignore[arg-type]
    assert status.available is True
    assert status.is_in_recovery is True
    assert "standby" in status.detail.lower()


async def test_status_unavailable_on_pg18() -> None:
    driver = FakeRoutingDriver(_route(180003, "18.3"))
    status = await get_wait_for_lsn_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "poll-loop" in status.detail


async def test_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_wait_for_lsn_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "version probe failed" in status.detail


# --- get_current_wal_lsn -------------------------------------------------


async def test_get_current_wal_lsn_on_primary() -> None:
    routes = {
        "pg_is_in_recovery()": [{"in_recovery": False}],
        "pg_current_wal_lsn()": [{"lsn": "0/1234ABCD"}],
    }
    driver = FakeRoutingDriver(routes)
    result = await get_current_wal_lsn(driver)  # type: ignore[arg-type]
    assert isinstance(result, CurrentWalLsnResult)
    assert result.role == "primary"
    assert result.lsn == "0/1234ABCD"


async def test_get_current_wal_lsn_on_standby() -> None:
    routes = {
        "pg_is_in_recovery()": [{"in_recovery": True}],
        "pg_last_wal_replay_lsn()": [{"lsn": "0/56789ABC"}],
    }
    driver = FakeRoutingDriver(routes)
    result = await get_current_wal_lsn(driver)  # type: ignore[arg-type]
    assert result.role == "standby"
    assert result.lsn == "0/56789ABC"


async def test_get_current_wal_lsn_wraps_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    with pytest.raises(WaitForLsnError, match="pg_is_in_recovery"):
        await get_current_wal_lsn(driver)  # type: ignore[arg-type]


# --- wait_for_lsn --------------------------------------------------------


class _WaitForLsnDriver:
    """Custom driver: routes version probe; records wait SQL; can simulate timeout / error."""

    def __init__(self, *, ver_num: int = 190001, ver: str = "19beta1", fail_with: str | None = None) -> None:
        self._ver_num = ver_num
        self._ver = ver
        self._fail_with = fail_with
        self.executed: list[str] = []
        self.calls: list[tuple[str, object, bool]] = []

    async def execute_query(self, query, params=None, force_readonly=False):  # type: ignore[no-untyped-def]
        from mcpg._vendor.sql import SqlDriver

        self.calls.append((query, params, force_readonly))
        if "current_setting" in query:
            return [SqlDriver.RowResult(cells={"ver_num": self._ver_num, "ver": self._ver})]
        # WAIT FOR LSN path.
        self.executed.append(query)
        if self._fail_with is not None:
            raise RuntimeError(self._fail_with)
        return []


async def test_wait_for_lsn_success_returns_not_timed_out() -> None:
    driver = _WaitForLsnDriver()
    result = await wait_for_lsn(driver, lsn="0/1234ABCD", timeout_ms=5000)  # type: ignore[arg-type]
    assert isinstance(result, WaitForLsnResult)
    assert result.lsn == "0/1234ABCD"
    assert result.timeout_ms == 5000
    assert result.timed_out is False
    assert driver.executed == ["WAIT FOR LSN '0/1234ABCD' TIMEOUT 5000"]


async def test_wait_for_lsn_default_timeout_is_zero() -> None:
    driver = _WaitForLsnDriver()
    result = await wait_for_lsn(driver, lsn="0/abcd")  # type: ignore[arg-type]
    assert result.timeout_ms == 0
    assert "TIMEOUT 0" in result.wait_sql


async def test_wait_for_lsn_timeout_reported_not_raised() -> None:
    """A 'timed out' message from the driver surfaces as `timed_out=True`."""
    driver = _WaitForLsnDriver(fail_with="WAIT FOR LSN timed out")
    result = await wait_for_lsn(driver, lsn="0/1234ABCD", timeout_ms=100)  # type: ignore[arg-type]
    assert result.timed_out is True
    assert result.lsn == "0/1234ABCD"


class _SqlstateError(Exception):
    """Driver exception that carries a `sqlstate` attribute, like psycopg's
    DatabaseError. Used to test the locale-independent timeout path."""

    def __init__(self, msg: str, sqlstate: str) -> None:
        super().__init__(msg)
        self.sqlstate = sqlstate


async def test_wait_for_lsn_timeout_detected_via_sqlstate_57014() -> None:
    """A non-English error carrying SQLSTATE 57014 still surfaces as timed_out=True
    (gemini + sourcery critical on PR #146 — locale-independent timeout detection)."""
    driver = _WaitForLsnDriver()
    # Override the driver's failure with a SQLSTATE-bearing exception.
    driver._fail_with = None

    async def fail_with_sqlstate(query, params=None, force_readonly=False):  # type: ignore[no-untyped-def]
        from mcpg._vendor.sql import SqlDriver

        driver.calls.append((query, params, force_readonly))
        if "current_setting" in query:
            return [SqlDriver.RowResult(cells={"ver_num": 190001, "ver": "19beta1"})]
        driver.executed.append(query)
        # Mimic a localized error string (German for "canceled by query timeout").
        raise _SqlstateError("Anfrage durch Anweisungs-Timeout abgebrochen", sqlstate="57014")

    driver.execute_query = fail_with_sqlstate  # type: ignore[method-assign]
    result = await wait_for_lsn(driver, lsn="0/1234ABCD", timeout_ms=100)  # type: ignore[arg-type]
    assert result.timed_out is True
    assert result.lsn == "0/1234ABCD"


async def test_wait_for_lsn_other_driver_failure_raises() -> None:
    driver = _WaitForLsnDriver(fail_with="connection refused")
    with pytest.raises(WaitForLsnError, match="WAIT FOR LSN failed"):
        await wait_for_lsn(driver, lsn="0/1234ABCD", timeout_ms=100)  # type: ignore[arg-type]


async def test_wait_for_lsn_raises_on_pg18() -> None:
    driver = _WaitForLsnDriver(ver_num=180003, ver="18.3")
    with pytest.raises(WaitForLsnError, match="PostgreSQL 19"):
        await wait_for_lsn(driver, lsn="0/1234ABCD")  # type: ignore[arg-type]
    assert driver.executed == []


@pytest.mark.parametrize(
    "bad_lsn",
    [
        "not-an-lsn",
        "0/",
        "/1234",
        "0/XYZ",  # non-hex
        "'; DROP TABLE evil; --",
        "0/1234 OR 1=1",
        "",
    ],
)
async def test_wait_for_lsn_rejects_malformed_lsn(bad_lsn: str) -> None:
    driver = _WaitForLsnDriver()
    with pytest.raises(WaitForLsnError, match="invalid LSN format"):
        await wait_for_lsn(driver, lsn=bad_lsn)  # type: ignore[arg-type]
    assert driver.executed == []


async def test_wait_for_lsn_rejects_negative_timeout() -> None:
    driver = _WaitForLsnDriver()
    with pytest.raises(WaitForLsnError, match=">= 0"):
        await wait_for_lsn(driver, lsn="0/1234", timeout_ms=-1)  # type: ignore[arg-type]


async def test_wait_for_lsn_rejects_non_int_timeout() -> None:
    driver = _WaitForLsnDriver()
    with pytest.raises(WaitForLsnError, match="non-negative int"):
        await wait_for_lsn(driver, lsn="0/1234", timeout_ms="5000")  # type: ignore[arg-type]


async def test_wait_for_lsn_rejects_bool_timeout() -> None:
    """bool is an int subclass — reject explicitly so True isn't silently 1."""
    driver = _WaitForLsnDriver()
    with pytest.raises(WaitForLsnError, match="bool"):
        await wait_for_lsn(driver, lsn="0/1234", timeout_ms=True)  # type: ignore[arg-type]


# --- recommend_read_your_writes ------------------------------------------


def _advisor_route(*, ver_num: int, ver: str, in_recovery: bool, lag: int | None) -> FakeRoutingDriver:
    """Build a FakeRoutingDriver for the new combined advisor query.

    The advisor runs a single SELECT that returns ver_num / ver /
    in_recovery / lag in one row — match on the (unique) substring of
    that SELECT so the fake doesn't accidentally route via the
    legacy per-probe keys.
    """
    return FakeRoutingDriver(
        {
            "AS in_recovery": [
                {
                    "ver_num": ver_num,
                    "ver": ver,
                    "in_recovery": in_recovery,
                    "lag": lag,
                }
            ],
        }
    )


async def test_recommend_primary_returns_no_wait_needed() -> None:
    driver = _advisor_route(ver_num=190001, ver="19beta1", in_recovery=False, lag=None)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert isinstance(rec, ReadYourWritesRecommendation)
    assert rec.recommend_use is False
    assert rec.reason == "primary_no_wait_needed"
    assert rec.is_in_recovery is False


async def test_recommend_standby_with_lag_recommends_use() -> None:
    driver = _advisor_route(ver_num=190001, ver="19beta1", in_recovery=True, lag=50_000_000)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.recommend_use is True
    assert rec.reason == "standby_with_lag"
    assert rec.current_lag_bytes == 50_000_000


async def test_recommend_standby_no_lag_does_not_recommend() -> None:
    driver = _advisor_route(ver_num=190001, ver="19beta1", in_recovery=True, lag=0)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.recommend_use is False
    assert rec.reason == "standby_no_lag"
    assert rec.current_lag_bytes == 0


async def test_recommend_standby_lag_unknown_is_distinct_from_no_lag() -> None:
    """lag=NULL → standby_lag_unknown, not standby_no_lag — sourcery critical on PR #146."""
    driver = _advisor_route(ver_num=190001, ver="19beta1", in_recovery=True, lag=None)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.recommend_use is False
    assert rec.reason == "standby_lag_unknown"
    assert rec.current_lag_bytes is None
    assert "could not be measured" in rec.detail


async def test_recommend_standby_pg18_falls_back() -> None:
    driver = _advisor_route(ver_num=180003, ver="18.3", in_recovery=True, lag=100)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.recommend_use is False
    assert rec.reason == "standby_pg18_or_older"
    assert "poll-loop" in rec.detail


async def test_recommend_never_raises_on_driver_failure() -> None:
    """A bare driver failure must surface as reason=unavailable, not raise.

    Gemini critical on PR #146: the prior split-query implementation
    could mis-classify a standby as a primary if the recovery probe
    swallowed an exception. The combined query collapses that risk
    into a single try/except that always routes to `unavailable`.
    """
    driver = FakeDriver(fail=True)
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.recommend_use is False
    assert rec.reason == "unavailable"
    assert rec.is_in_recovery is False


async def test_recommend_handles_empty_rows() -> None:
    """A driver that returns zero rows from the combined SELECT routes to `unavailable`."""
    driver = FakeRoutingDriver({"AS in_recovery": []})
    rec = await recommend_read_your_writes(driver)  # type: ignore[arg-type]
    assert rec.reason == "unavailable"
    assert "no rows" in rec.detail.lower()


# --- Dataclass shapes -----------------------------------------------------


def test_dataclass_shapes() -> None:
    s = WaitForLsnStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        is_in_recovery=False,
        detail="ok",
    )
    assert s.available is True
    c = CurrentWalLsnResult(role="primary", lsn="0/1")
    assert c.role == "primary"
    w = WaitForLsnResult(lsn="0/1", timeout_ms=0, timed_out=False, wait_sql="WAIT ...")
    assert w.timed_out is False
    r = ReadYourWritesRecommendation(
        recommend_use=False,
        reason="primary_no_wait_needed",
        is_in_recovery=False,
        server_version_num=190001,
        current_lag_bytes=None,
        detail="ok",
    )
    assert r.recommend_use is False

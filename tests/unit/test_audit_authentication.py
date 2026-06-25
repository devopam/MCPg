"""Tests for the `audit_authentication` category in `mcpg.audit`."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.audit import audit_authentication


def _expiry_row(expired: int = 0, expiring_soon: int = 0, roles_at_risk: list[str] | None = None) -> dict[str, Any]:
    return {
        "expired": expired,
        "expiring_soon": expiring_soon,
        "roles_at_risk": roles_at_risk or [],
    }


def _md5_row(count: int = 0, roles: list[str] | None = None) -> dict[str, Any]:
    return {"md5_count": count, "md5_roles": roles or []}


async def test_returns_good_when_no_password_issues_exist() -> None:
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row()],
            "rolpassword LIKE 'md5%'": [_md5_row()],
        }
    )
    result = await audit_authentication(driver)
    assert result.status == "GOOD"
    assert result.score == 100
    assert all(m.status == "GOOD" for m in result.metrics)


async def test_warns_on_passwords_expiring_within_window() -> None:
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row(expiring_soon=3, roles_at_risk=["alice", "bob", "carol"])],
            "rolpassword LIKE 'md5%'": [_md5_row()],
        }
    )
    result = await audit_authentication(driver)
    expiry = next(m for m in result.metrics if m.name == "Role Password Expiration")
    assert expiry.status == "WARNING"
    assert "3 login role" in expiry.evidence
    assert "alice" in expiry.evidence


async def test_critical_when_passwords_already_expired() -> None:
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row(expired=2, roles_at_risk=["staleA", "staleB"])],
            "rolpassword LIKE 'md5%'": [_md5_row()],
        }
    )
    result = await audit_authentication(driver)
    expiry = next(m for m in result.metrics if m.name == "Role Password Expiration")
    assert expiry.status == "CRITICAL"
    assert "expired" in expiry.evidence.lower()
    # CRITICAL on this metric pulls the category status below GOOD.
    assert result.status in ("WARNING", "CRITICAL")


async def test_warns_on_md5_password_hashes() -> None:
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row()],
            "rolpassword LIKE 'md5%'": [_md5_row(count=4, roles=["legacy1", "legacy2", "legacy3", "legacy4"])],
        }
    )
    result = await audit_authentication(driver)
    md5 = next(m for m in result.metrics if m.name.startswith("MD5"))
    assert md5.status == "WARNING"
    assert "4 login role" in md5.evidence
    # Suggestion must point the operator at SCRAM-SHA-256.
    assert "scram-sha-256" in md5.suggestion.lower()


async def test_pg_authid_permission_denied_degrades_to_warning_per_metric() -> None:
    """`pg_authid` is superuser-only; non-superuser audit role should
    surface a clean per-metric WARNING, not an exception."""

    class _DeniedDriver:
        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del query, params, force_readonly
            raise PermissionError("pg_authid: permission denied")

    result = await audit_authentication(_DeniedDriver())  # type: ignore[arg-type]
    assert len(result.metrics) == 2
    assert all(m.value == "N/A" for m in result.metrics)
    assert all("pg_authid" in m.evidence for m in result.metrics)
    # Both probes degrade to WARNING — the category overall stays
    # actionable rather than disappearing on a permission denial.
    assert all(m.status == "WARNING" for m in result.metrics)


async def test_category_status_drops_below_good_when_any_metric_flags() -> None:
    """One CRITICAL metric should pull category_score below 80."""
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row(expired=5)],
            "rolpassword LIKE 'md5%'": [_md5_row()],
        }
    )
    result = await audit_authentication(driver)
    assert result.score < 100
    assert result.status != "GOOD"


@pytest.mark.parametrize(
    ("expired", "expiring_soon", "md5_count", "expected_status"),
    [
        (0, 0, 0, "GOOD"),  # 100 → GOOD
        (0, 1, 0, "GOOD"),  # one WARNING metric → -10 → 90 → still GOOD
        (1, 0, 0, "WARNING"),  # CRITICAL on expiry → -25 → 75 → WARNING
        (1, 0, 5, "WARNING"),  # -25 -15 → 60 → WARNING
        (3, 0, 5, "WARNING"),  # -25 -15 → 60; metric severity is CRITICAL though
    ],
)
async def test_score_arithmetic_aligns_with_metric_severities(
    expired: int, expiring_soon: int, md5_count: int, expected_status: str
) -> None:
    driver = FakeRoutingDriver(
        {
            "rolvaliduntil IS NOT NULL": [_expiry_row(expired=expired, expiring_soon=expiring_soon)],
            "rolpassword LIKE 'md5%'": [_md5_row(count=md5_count)],
        }
    )
    result = await audit_authentication(driver)
    assert result.status == expected_status

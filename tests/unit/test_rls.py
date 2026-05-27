"""Tests for the RLS policy tester (Phase 4.8)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.rls import (
    ActivePolicy,
    RLSError,
    RLSTestResult,
)
from mcpg.rls import test_rls_for_role as run_rls_test


async def test_test_rls_for_role_rejects_unsafe_role_name() -> None:
    with pytest.raises(RLSError, match="invalid role"):
        await run_rls_test(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema="app",
            table="orders",
            role='"; DROP USER alice',
        )


async def test_test_rls_for_role_rejects_unsafe_schema() -> None:
    with pytest.raises(RLSError, match="invalid schema"):
        await run_rls_test(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema='app"; DROP TABLE x',
            table="orders",
            role="readonly",
        )


async def test_test_rls_for_role_rejects_negative_sample_size() -> None:
    with pytest.raises(RLSError, match="sample_size"):
        await run_rls_test(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema="app",
            table="orders",
            role="readonly",
            sample_size=-1,
        )


async def test_test_rls_for_role_raises_when_table_not_found() -> None:
    # The relrowsecurity check returns no rows.
    driver = FakeRoutingDriver({"c.relrowsecurity": []})

    with pytest.raises(RLSError, match="not found"):
        await run_rls_test(
            driver,  # type: ignore[arg-type]
            schema="app",
            table="orders",
            role="readonly",
        )


async def test_test_rls_for_role_returns_result_with_policies_and_sample() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relrowsecurity": [{"enabled": True}],
            "pg_policy p": [
                {
                    "name": "orders_owner_read",
                    "permissive": "permissive",
                    "roles": ["readonly"],
                    "command": "SELECT",
                    "using_expr": "(owner = current_user)",
                    "with_check_expr": None,
                }
            ],
            "SELECT COUNT(*)": [{"n": 7}],
            "SELECT *": [
                {"id": 1, "owner": "readonly"},
                {"id": 2, "owner": "readonly"},
            ],
        }
    )

    result = await run_rls_test(
        driver,  # type: ignore[arg-type]
        schema="app",
        table="orders",
        role="readonly",
    )

    assert isinstance(result, RLSTestResult)
    assert result.rls_enabled is True
    assert len(result.active_policies) == 1
    assert isinstance(result.active_policies[0], ActivePolicy)
    assert result.active_policies[0].command == "SELECT"
    assert result.rows_visible == 7
    assert len(result.sample) == 2
    assert result.columns == ["id", "owner"]


async def test_test_rls_for_role_skips_sample_when_sample_size_zero() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relrowsecurity": [{"enabled": False}],
            "pg_policy p": [],
            "SELECT COUNT(*)": [{"n": 0}],
        }
    )

    result = await run_rls_test(
        driver,  # type: ignore[arg-type]
        schema="app",
        table="orders",
        role="readonly",
        sample_size=0,
    )

    assert result.rls_enabled is False
    assert result.sample == []
    assert result.columns == []

"""Tests for the naming-convention linter (Phase 8.1)."""

from __future__ import annotations

from _fakes import FakeRoutingDriver

from mcpg.naming import (
    NamingFinding,
    NamingReport,
    classify_style,
    lint_naming_conventions,
)


def test_classify_style_recognises_common_conventions() -> None:
    assert classify_style("users") == "snake_case"
    assert classify_style("user_profile") == "snake_case"
    assert classify_style("userProfile") == "camelCase"
    assert classify_style("UserProfile") == "PascalCase"
    assert classify_style("CONST_VALUE") == "SCREAMING_SNAKE"
    assert classify_style("Strange-Name!") == "other"


async def test_lint_returns_empty_findings_for_uniformly_snake_case_schema() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relkind IN ('r', 'p') ORDER BY c.relname": [
                {"table_name": "users"},
                {"table_name": "user_profiles"},
                {"table_name": "orders"},
            ],
            "a.attisdropped": [
                {"table_name": "users", "column_name": "id"},
                {"table_name": "users", "column_name": "email"},
                {"table_name": "user_profiles", "column_name": "id"},
                {"table_name": "user_profiles", "column_name": "user_id"},
                {"table_name": "orders", "column_name": "id"},
                {"table_name": "orders", "column_name": "total"},
            ],
            "NOT ix.indisprimary": [
                {"table_name": "users", "index_name": "idx_users_email"},
                {"table_name": "orders", "index_name": "uq_orders_total"},
            ],
        }
    )

    report = await lint_naming_conventions(driver, "public")  # type: ignore[arg-type]

    assert isinstance(report, NamingReport)
    assert report.schema_majority_style == "snake_case"
    assert report.findings == []


async def test_lint_flags_an_outlier_table_against_the_schema_majority() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relkind IN ('r', 'p') ORDER BY c.relname": [
                {"table_name": "users"},
                {"table_name": "orders"},
                {"table_name": "ProductCatalogue"},  # outlier
            ],
            "a.attisdropped": [],
            "NOT ix.indisprimary": [],
        }
    )

    report = await lint_naming_conventions(driver, "public")  # type: ignore[arg-type]

    assert report.schema_majority_style == "snake_case"
    flagged = {f.object for f in report.findings if f.rule == "table_naming_inconsistent"}
    assert "public.ProductCatalogue" in flagged


async def test_lint_flags_an_outlier_column_within_a_table() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relkind IN ('r', 'p') ORDER BY c.relname": [{"table_name": "users"}],
            "a.attisdropped": [
                {"table_name": "users", "column_name": "id"},
                {"table_name": "users", "column_name": "email"},
                {"table_name": "users", "column_name": "createdAt"},  # outlier
            ],
            "NOT ix.indisprimary": [],
        }
    )

    report = await lint_naming_conventions(driver, "public")  # type: ignore[arg-type]

    flagged = [f for f in report.findings if f.rule == "column_naming_inconsistent"]
    assert len(flagged) == 1
    assert isinstance(flagged[0], NamingFinding)
    assert flagged[0].object == "public.users.createdAt"
    assert flagged[0].style == "camelCase"


async def test_lint_flags_an_index_with_no_recognised_prefix() -> None:
    driver = FakeRoutingDriver(
        {
            "c.relkind IN ('r', 'p') ORDER BY c.relname": [{"table_name": "users"}],
            "a.attisdropped": [],
            "NOT ix.indisprimary": [
                {"table_name": "users", "index_name": "idx_users_email"},
                {"table_name": "users", "index_name": "users_email_lookup"},  # no prefix
            ],
        }
    )

    report = await lint_naming_conventions(driver, "public")  # type: ignore[arg-type]

    flagged = [f for f in report.findings if f.rule == "index_unexpected_prefix"]
    assert len(flagged) == 1
    assert flagged[0].object == "public.users_email_lookup"

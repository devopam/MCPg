"""Tests for the demo-dataset generator (pure parts; seeding is integration)."""

from collections import defaultdict

from mcpg.demo import (
    _CATALOG,
    _FEATURES_BY_TYPE,
    _INSERTS,
    _TABLE_DDL,
    DEMO_SCHEMA,
    SUGGESTED_PROMPTS,
    generate_demo_dataset,
)


def test_generation_is_deterministic() -> None:
    """Two independent generations must be identical.

    The captured walkthrough in docs/demo.md relies on users seeing the
    same rows it was generated from.
    """
    assert generate_demo_dataset() == generate_demo_dataset()


def test_row_counts_match_the_documented_shape() -> None:
    dataset = generate_demo_dataset()
    assert len(dataset.customers) == 400
    assert len(dataset.products) == 120
    assert len(dataset.orders) == 3000
    assert len(dataset.reviews) == 900
    # 1-4 items per order.
    assert 3000 <= len(dataset.order_items) <= 12000


def test_referential_integrity_of_generated_rows() -> None:
    dataset = generate_demo_dataset()
    for customer_id, _status, _date, _total in dataset.orders:
        assert 1 <= customer_id <= len(dataset.customers)
    for order_id, product_id, quantity, unit_price in dataset.order_items:
        assert 1 <= order_id <= len(dataset.orders)
        assert 1 <= product_id <= len(dataset.products)
        assert 1 <= quantity <= 3
        assert unit_price > 0
    for product_id, customer_id, rating, text, source, _created in dataset.reviews:
        assert 1 <= product_id <= len(dataset.products)
        assert 1 <= customer_id <= len(dataset.customers)
        assert 1 <= rating <= 5
        assert text
        assert source in {"web", "mobile", "email_campaign"}


def test_order_totals_equal_the_sum_of_their_items() -> None:
    dataset = generate_demo_dataset()
    totals: dict[int, int] = defaultdict(int)
    for order_id, _product_id, quantity, unit_price in dataset.order_items:
        totals[order_id] += quantity * unit_price
    for index, (_customer, _status, _date, total_cents) in enumerate(dataset.orders, start=1):
        assert total_cents == totals[index]


def test_customer_emails_are_unique() -> None:
    dataset = generate_demo_dataset()
    emails = [email for _name, email, _phone, _country, _signup, _opt in dataset.customers]
    assert len(emails) == len(set(emails))


def test_planted_findings_are_present_in_the_ddl() -> None:
    """The curated flaws are the dataset's point — pin them.

    If someone "fixes" the missing index or the camelCase column, the
    walkthrough's teaching moments silently die; fail loud instead.
    """
    ddl = "\n".join(_TABLE_DDL)
    # The camelCase naming violation on reviews.
    assert '"reviewSource"' in ddl
    # order_items and reviews carry FK indexes; orders.customer_id must NOT.
    index_ddl = [stmt for stmt in _TABLE_DDL if stmt.lstrip().startswith("CREATE INDEX")]
    assert len(index_ddl) == 3
    assert not any("orders (customer_id" in stmt or "orders(customer_id" in stmt for stmt in index_ddl)
    # PII bait for the sensitive-columns advisor.
    assert "email" in ddl and "phone" in ddl


def test_insert_targets_cover_every_dataset_field() -> None:
    dataset = generate_demo_dataset()
    for table in _INSERTS:
        assert hasattr(dataset, table)


def test_suggested_prompts_reference_the_demo_schema() -> None:
    assert SUGGESTED_PROMPTS
    assert any(DEMO_SCHEMA in prompt for prompt in SUGGESTED_PROMPTS)


def test_every_product_type_has_plausible_features() -> None:
    """Review text must stay plausible — a yoga mat praised for its

    battery life reads as generated garbage and undermines the demo.
    Every product type in the catalog needs its own feature list, and
    the walkthrough's canonical FTS query needs enough hits.
    """
    for product_types, _price_range in _CATALOG.values():
        for product_type in product_types:
            assert len(_FEATURES_BY_TYPE[product_type]) >= 2
    battery_types = [t for t, features in _FEATURES_BY_TYPE.items() if "battery life" in features]
    assert len(battery_types) >= 4, "the docs/demo.md FTS example searches for 'battery life'"

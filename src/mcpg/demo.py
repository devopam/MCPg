"""The ``mcpg --demo`` dataset — a curated playground schema.

Seeds a small, deterministic e-commerce dataset into an ``mcpg_demo``
schema inside the database ``MCPG_DATABASE_URL`` points at, so a new
user's first five minutes with MCPg run against data that actually
shows the tools off instead of an empty database. Everything is
namespaced under one schema and removed with ``mcpg --demo-drop``.

The dataset is *curated, not random*: it plants specific teaching
moments the walkthrough (docs/demo.md) and the pivotal tools rely on —

- ``orders.customer_id`` is a foreign key with **no index**, so
  ``analyze_query_plan`` shows a sequential scan and
  ``recommend_indexes`` has a genuine, correct finding to make.
- ``customers.email`` / ``customers.phone`` give the sensitive-column
  advisor real hits; ``reviews."reviewSource"`` is a deliberate
  camelCase naming violation for the naming advisor.
- ``reviews.review_text`` and ``products.description`` carry natural
  prose for ``full_text_search`` / ``hybrid_search``.
- ``products.embedding`` (pgvector, 8-dim) is added only when the
  ``vector`` extension is already installed — never created here —
  so vector tools work when they can and everything else works anyway.
- Order dates skew recent and customers follow a heavy-tailed
  distribution, so time-window and top-N aggregates return the kind of
  shapes real dashboards have.

Generation is fully deterministic (fixed RNG seed, fixed anchor date):
re-seeding always produces byte-identical rows, so captured demos match
what users see on their own machine.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

DEMO_SCHEMA = "mcpg_demo"

# Stamped as the schema comment on seed; ``drop_demo`` refuses to drop a
# schema that doesn't carry it, so ``--demo-drop`` can never destroy a
# schema MCPg didn't create.
_MARKER_PREFIX = "MCPg demo dataset"
_MARKER = f"{_MARKER_PREFIX} — safe to remove with `mcpg --demo-drop`."

# Determinism anchors. Never derived from the wall clock: identical
# invocations must produce identical rows so the captured walkthrough in
# docs/demo.md matches a fresh local seed exactly.
_SEED = 20260630
_ANCHOR = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)

_CUSTOMER_COUNT = 400
_PRODUCT_COUNT = 120
_ORDER_COUNT = 3000
_REVIEW_COUNT = 900


class DemoError(Exception):
    """Raised when seeding or dropping the demo schema cannot proceed."""


@dataclass(frozen=True)
class DemoDataset:
    """The generated rows, before they touch a database."""

    customers: list[tuple[str, str, str, str, date, bool]]
    products: list[tuple[str, str, str, int, str]]
    orders: list[tuple[int, str, datetime, int]]
    order_items: list[tuple[int, int, int, int]]
    reviews: list[tuple[int, int, int, str, str, datetime]]


@dataclass(frozen=True)
class DemoSeedSummary:
    """What ``seed_demo`` created."""

    schema: str
    row_counts: dict[str, int]
    vector_column_included: bool


@dataclass(frozen=True)
class DemoDropSummary:
    """What ``drop_demo`` removed."""

    schema: str
    dropped: bool


_FIRST_NAMES = [
    "Aarav", "Beatriz", "Chen", "Diego", "Elena", "Farid", "Grace", "Hiro",
    "Ingrid", "Jamal", "Kavya", "Liam", "Mei", "Noah", "Olga", "Priya",
    "Quentin", "Rosa", "Santiago", "Tara", "Umar", "Valentina", "Wei", "Yuki",
]
_LAST_NAMES = [
    "Almeida", "Bauer", "Chatterjee", "Diallo", "Eriksson", "Fischer",
    "Garcia", "Haddad", "Ivanov", "Jensen", "Kim", "Lopez", "Mbeki",
    "Nakamura", "Okafor", "Patel", "Quinn", "Rahman", "Silva", "Tanaka",
    "Umarov", "Varga", "Wong", "Yilmaz",
]
_COUNTRIES = ["US", "US", "US", "IN", "IN", "DE", "GB", "BR", "JP", "AU", "CA", "FR"]

# category -> (product types, price range in cents). Feature phrases
# live per *product type* (below), not per category, so a yoga mat is
# never praised for its battery life — reviews and descriptions must
# read plausibly, because they're what FTS demos surface verbatim.
_CATALOG: dict[str, tuple[list[str], tuple[int, int]]] = {
    "Audio": (
        ["Wireless Headphones", "Bluetooth Speaker", "Studio Microphone", "Earbuds", "Soundbar"],
        (2500, 34900),
    ),
    "Computing": (
        ["Mechanical Keyboard", "4K Monitor", "USB-C Hub", "Ergonomic Mouse", "Laptop Stand"],
        (1900, 59900),
    ),
    "Home": (
        ["Air Purifier", "Smart Thermostat", "Robot Vacuum", "Espresso Machine", "Desk Lamp"],
        (3500, 79900),
    ),
    "Fitness": (
        ["Fitness Tracker", "Yoga Mat", "Adjustable Dumbbells", "Foam Roller", "Jump Rope"],
        (900, 42900),
    ),
    "Photography": (
        ["Camera Tripod", "Ring Light", "Camera Backpack", "Lens Filter Kit", "Gimbal Stabilizer"],
        (1500, 52900),
    ),
    "Accessories": (
        ["Phone Case", "Power Bank", "Cable Organizer", "Screen Protector", "Travel Adapter"],
        (500, 8900),
    ),
}

# What can plausibly be praised or panned about each product type.
# "battery life" recurs across several types on purpose — it's the
# walkthrough's canonical FTS query and needs a healthy hit count.
_FEATURES_BY_TYPE: dict[str, list[str]] = {
    "Wireless Headphones": ["battery life", "noise cancellation", "sound quality"],
    "Bluetooth Speaker": ["battery life", "bass response", "water resistance"],
    "Studio Microphone": ["recording clarity", "build quality", "background-noise rejection"],
    "Earbuds": ["battery life", "fit and comfort", "sound quality"],
    "Soundbar": ["bass response", "dialogue clarity", "setup simplicity"],
    "Mechanical Keyboard": ["typing feel", "build quality", "key stability"],
    "4K Monitor": ["colour accuracy", "response time", "stand ergonomics"],
    "USB-C Hub": ["port selection", "heat management", "build quality"],
    "Ergonomic Mouse": ["comfort", "battery life", "tracking precision"],
    "Laptop Stand": ["stability", "adjustability", "build quality"],
    "Air Purifier": ["quiet operation", "filter life", "air-quality sensing"],
    "Smart Thermostat": ["app control", "scheduling flexibility", "energy savings"],
    "Robot Vacuum": ["navigation", "suction power", "battery life"],
    "Espresso Machine": ["temperature stability", "ease of cleaning", "crema quality"],
    "Desk Lamp": ["brightness range", "colour temperature", "build quality"],
    "Fitness Tracker": ["battery life", "heart-rate accuracy", "sleep tracking"],
    "Yoga Mat": ["grip", "cushioning", "durability"],
    "Adjustable Dumbbells": ["build quality", "plate-change speed", "grip comfort"],
    "Foam Roller": ["firmness", "durability", "surface texture"],
    "Jump Rope": ["handle grip", "cable durability", "length adjustment"],
    "Camera Tripod": ["stability", "build quality", "portability"],
    "Ring Light": ["brightness range", "colour accuracy", "mounting options"],
    "Camera Backpack": ["padding", "capacity", "weather resistance"],
    "Lens Filter Kit": ["colour accuracy", "build quality", "case quality"],
    "Gimbal Stabilizer": ["stabilisation", "battery life", "app pairing"],
    "Phone Case": ["fit and finish", "drop protection", "grip"],
    "Power Bank": ["charging speed", "capacity", "portability"],
    "Cable Organizer": ["build quality", "adhesive strength", "capacity"],
    "Screen Protector": ["clarity", "ease of installation", "scratch resistance"],
    "Travel Adapter": ["plug compatibility", "build quality", "charging speed"],
}
_ADJECTIVES = ["Aurora", "Nimbus", "Vertex", "Solstice", "Kestrel", "Meridian", "Cascade", "Ember"]

_POSITIVE_REVIEWS = [
    "Absolutely love the {product} — the {feature} is outstanding and shipping was fast.",
    "The {product} exceeded expectations. Great {feature}, would happily buy again.",
    "Five months in and the {product} still performs like new. The {feature} stands out.",
    "Upgraded from a cheaper brand and the difference in {feature} is night and day.",
]
_MIXED_REVIEWS = [
    "The {product} is decent for the price, though the {feature} could be better.",
    "Solid {product} overall. Setup was fiddly but the {feature} works as advertised.",
    "Good {feature}, average everything else. The {product} does the job.",
]
_NEGATIVE_REVIEWS = [
    "Disappointed with the {product}. The {feature} degraded within weeks and support was slow.",
    "Returned the {product} for a refund — the {feature} never matched the listing.",
    "The {product} arrived with a damaged box and the {feature} is far below what was promised.",
]
_REVIEW_SOURCES = ["web", "web", "web", "mobile", "mobile", "email_campaign"]

_ORDER_STATUSES = ["delivered"] * 14 + ["shipped"] * 2 + ["pending"] * 3 + ["cancelled"]


def generate_demo_dataset() -> DemoDataset:
    """Build the full dataset in memory. Pure and deterministic."""
    rng = random.Random(_SEED)

    customers: list[tuple[str, str, str, str, date, bool]] = []
    for i in range(_CUSTOMER_COUNT):
        first = _FIRST_NAMES[rng.randrange(len(_FIRST_NAMES))]
        last = _LAST_NAMES[rng.randrange(len(_LAST_NAMES))]
        email = f"{first.lower()}.{last.lower()}.{i + 1}@example.com"
        phone = f"+1-555-{rng.randrange(100, 1000):03d}-{rng.randrange(10000):04d}"
        signup = (_ANCHOR - timedelta(days=rng.randrange(30, 900))).date()
        customers.append((f"{first} {last}", email, phone, _COUNTRIES[rng.randrange(len(_COUNTRIES))], signup, rng.random() < 0.6))

    products: list[tuple[str, str, str, int, str]] = []
    categories = list(_CATALOG)
    for i in range(_PRODUCT_COUNT):
        category = categories[i % len(categories)]
        product_types, (lo, hi) = _CATALOG[category]
        product_type = product_types[rng.randrange(len(product_types))]
        name = f"{_ADJECTIVES[rng.randrange(len(_ADJECTIVES))]} {product_type}"
        price = rng.randrange(lo, hi, 100) + 99
        feature_a, feature_b = rng.sample(_FEATURES_BY_TYPE[product_type], 2)
        description = (
            f"{name} for everyday {category.lower()} use. Engineered for {feature_a} "
            f"with class-leading {feature_b}, backed by a two-year warranty."
        )
        products.append((f"SKU-{category[:3].upper()}-{i + 1:04d}", name, category, price, description))

    # Heavy-tailed customer activity: a minority of customers place most
    # orders, so "top customers" queries return an interesting shape.
    orders: list[tuple[int, str, datetime, int]] = []
    order_items: list[tuple[int, int, int, int]] = []
    for order_id in range(1, _ORDER_COUNT + 1):
        customer_id = 1 + int(_CUSTOMER_COUNT * (rng.random() ** 1.8)) % _CUSTOMER_COUNT
        # Quadratic skew toward the anchor date: recent months are busier,
        # so time-window questions ("revenue in the last 90 days") pop.
        order_date = _ANCHOR - timedelta(days=int(540 * (rng.random() ** 2)), minutes=rng.randrange(1440))
        status = _ORDER_STATUSES[rng.randrange(len(_ORDER_STATUSES))]
        total = 0
        for _ in range(rng.randrange(1, 5)):
            product_id = 1 + rng.randrange(_PRODUCT_COUNT)
            quantity = rng.randrange(1, 4)
            unit_price = products[product_id - 1][3]
            if rng.random() < 0.15:  # occasional promotional discount
                unit_price = int(unit_price * 0.9)
            order_items.append((order_id, product_id, quantity, unit_price))
            total += quantity * unit_price
        orders.append((customer_id, status, order_date, total))

    reviews: list[tuple[int, int, int, str, str, datetime]] = []
    for _ in range(_REVIEW_COUNT):
        # Popular (low-id) products accumulate more reviews.
        product_id = 1 + int(_PRODUCT_COUNT * (rng.random() ** 1.5)) % _PRODUCT_COUNT
        customer_id = 1 + rng.randrange(_CUSTOMER_COUNT)
        rating = rng.choices([1, 2, 3, 4, 5], weights=[6, 7, 15, 32, 40])[0]
        templates = _POSITIVE_REVIEWS if rating >= 4 else _MIXED_REVIEWS if rating == 3 else _NEGATIVE_REVIEWS
        _, name, _category, _, _ = products[product_id - 1]
        # The adjective is the name's first word; the rest is the
        # product type, which keys the plausible-feature table.
        feature = rng.choice(_FEATURES_BY_TYPE[name.split(" ", 1)[1]])
        text = templates[rng.randrange(len(templates))].format(product=name, feature=feature)
        created = _ANCHOR - timedelta(days=rng.randrange(1, 520), minutes=rng.randrange(1440))
        reviews.append((product_id, customer_id, rating, text, rng.choice(_REVIEW_SOURCES), created))

    return DemoDataset(customers=customers, products=products, orders=orders, order_items=order_items, reviews=reviews)


def _deterministic_embedding(rng: random.Random, category_index: int, price_cents: int, rating_hint: float) -> str:
    """An 8-dim pseudo-embedding: category one-hot-ish + price + jitter.

    Not a real language model embedding — but nearest-neighbour over it
    still clusters by category and price band, which is exactly enough
    for ``hybrid_search`` / ``retrieve_with_context`` demos to return
    explainable results.
    """
    vec = [round(rng.uniform(0.0, 0.08), 4) for _ in range(8)]
    vec[category_index % 6] = round(0.85 + rng.uniform(0.0, 0.1), 4)
    vec[6] = round(min(price_cents / 80000.0, 1.0), 4)
    vec[7] = round(rating_hint, 4)
    return "[" + ",".join(str(v) for v in vec) + "]"


# Static DDL, formatted with the schema identifier at seed time. The
# deliberate deviations are annotated — they are the dataset's teaching
# moments, not oversights. (LiteralString because psycopg's sql.SQL()
# only accepts literals — these are, and the annotation proves it.)
_TABLE_DDL: list[LiteralString] = [
    # customers: email + phone are the sensitive-column advisor's bait.
    """
    CREATE TABLE {schema}.customers (
        customer_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        full_name        text NOT NULL,
        email            text NOT NULL,
        phone            text NOT NULL,
        country          text NOT NULL,
        signup_date      date NOT NULL,
        marketing_opt_in boolean NOT NULL
    )
    """,
    """
    CREATE TABLE {schema}.products (
        product_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        sku         text NOT NULL,
        product_name text NOT NULL,
        category    text NOT NULL,
        price_cents integer NOT NULL,
        description text NOT NULL
    )
    """,
    # orders.customer_id: FK with NO covering index — the planted
    # finding for analyze_query_plan / recommend_indexes.
    """
    CREATE TABLE {schema}.orders (
        order_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        customer_id integer NOT NULL REFERENCES {schema}.customers,
        status      text NOT NULL,
        order_date  timestamptz NOT NULL,
        total_cents integer NOT NULL
    )
    """,
    """
    CREATE TABLE {schema}.order_items (
        order_item_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        order_id        integer NOT NULL REFERENCES {schema}.orders,
        product_id      integer NOT NULL REFERENCES {schema}.products,
        quantity        integer NOT NULL,
        unit_price_cents integer NOT NULL
    )
    """,
    # "reviewSource": deliberate camelCase — the naming advisor's bait.
    """
    CREATE TABLE {schema}.reviews (
        review_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        product_id  integer NOT NULL REFERENCES {schema}.products,
        customer_id integer NOT NULL REFERENCES {schema}.customers,
        rating      integer NOT NULL CHECK (rating BETWEEN 1 AND 5),
        review_text text NOT NULL,
        "reviewSource" text NOT NULL,
        created_at  timestamptz NOT NULL
    )
    """,
    # order_items and reviews get proper FK indexes — contrast that
    # makes the missing one on orders.customer_id a *finding*, not a
    # theme.
    "CREATE INDEX order_items_order_id_idx ON {schema}.order_items (order_id)",
    "CREATE INDEX order_items_product_id_idx ON {schema}.order_items (product_id)",
    "CREATE INDEX reviews_product_id_idx ON {schema}.reviews (product_id)",
]

_INSERTS: dict[str, LiteralString] = {
    "customers": (
        "INSERT INTO {schema}.customers (full_name, email, phone, country, signup_date, marketing_opt_in) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    ),
    "products": (
        "INSERT INTO {schema}.products (sku, product_name, category, price_cents, description) "
        "VALUES (%s, %s, %s, %s, %s)"
    ),
    "orders": "INSERT INTO {schema}.orders (customer_id, status, order_date, total_cents) VALUES (%s, %s, %s, %s)",
    "order_items": (
        "INSERT INTO {schema}.order_items (order_id, product_id, quantity, unit_price_cents) VALUES (%s, %s, %s, %s)"
    ),
    "reviews": (
        'INSERT INTO {schema}.reviews (product_id, customer_id, rating, review_text, "reviewSource", created_at) '
        "VALUES (%s, %s, %s, %s, %s, %s)"
    ),
}


async def _schema_exists(conn: psycopg.AsyncConnection[TupleRow], schema: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [schema])
        return await cur.fetchone() is not None


async def _vector_extension_installed(conn: psycopg.AsyncConnection[TupleRow]) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return await cur.fetchone() is not None


async def seed_demo(dsn: str) -> DemoSeedSummary:
    """Create and populate the ``mcpg_demo`` schema.

    Refuses to touch a schema that already exists (drop it first with
    ``mcpg --demo-drop``); the whole seed runs in one transaction, so a
    failure part-way leaves nothing behind.

    Raises:
        DemoError: If the schema already exists or the DSN is unreachable.
    """
    dataset = generate_demo_dataset()
    try:
        conn = await psycopg.AsyncConnection.connect(dsn)
    except psycopg.OperationalError as exc:
        raise DemoError(f"cannot connect to the database: {exc}") from exc
    try:
        if await _schema_exists(conn, DEMO_SCHEMA):
            raise DemoError(
                f"schema {DEMO_SCHEMA!r} already exists — run `mcpg --demo-drop` first if you want a fresh seed"
            )
        schema_ident = sql.Identifier(DEMO_SCHEMA)
        async with conn.cursor() as cur:
            await cur.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))
            await cur.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(schema_ident, sql.Literal(_MARKER)))
            for ddl in _TABLE_DDL:
                await cur.execute(sql.SQL(ddl).format(schema=schema_ident))
            for table, insert in _INSERTS.items():
                rows: list[tuple[object, ...]] = getattr(dataset, table)
                await cur.executemany(sql.SQL(insert).format(schema=schema_ident), rows)

            vector_included = await _vector_extension_installed(conn)
            if vector_included:
                await cur.execute(sql.SQL("ALTER TABLE {}.products ADD COLUMN embedding vector(8)").format(schema_ident))
                rng = random.Random(_SEED + 1)
                categories = list(_CATALOG)
                updates = [
                    (
                        _deterministic_embedding(
                            rng, categories.index(category), price, round(rng.uniform(0.2, 1.0), 4)
                        ),
                        i + 1,
                    )
                    for i, (_, _, category, price, _) in enumerate(dataset.products)
                ]
                await cur.executemany(
                    sql.SQL("UPDATE {}.products SET embedding = %s::vector WHERE product_id = %s").format(schema_ident),
                    updates,
                )

            # Fresh planner statistics so the walkthrough's EXPLAIN output
            # reflects the data, not an unanalyzed guess.
            for table in _INSERTS:
                await cur.execute(sql.SQL("ANALYZE {}.{}").format(schema_ident, sql.Identifier(table)))
        await conn.commit()
    except psycopg.Error as exc:
        # The whole seed is one transaction, so nothing was left behind.
        raise DemoError(f"seeding failed ({type(exc).__name__}): {exc}") from exc
    finally:
        await conn.close()

    return DemoSeedSummary(
        schema=DEMO_SCHEMA,
        row_counts={table: len(getattr(dataset, table)) for table in _INSERTS},
        vector_column_included=vector_included,
    )


async def drop_demo(dsn: str) -> DemoDropSummary:
    """Drop the ``mcpg_demo`` schema, but only if MCPg created it.

    The schema-comment marker written by ``seed_demo`` is the proof of
    ownership; a schema named ``mcpg_demo`` without it is left alone.

    Raises:
        DemoError: If the schema exists but doesn't carry the MCPg
            marker, or the DSN is unreachable.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(dsn)
    except psycopg.OperationalError as exc:
        raise DemoError(f"cannot connect to the database: {exc}") from exc
    try:
        if not await _schema_exists(conn, DEMO_SCHEMA):
            return DemoDropSummary(schema=DEMO_SCHEMA, dropped=False)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname = %s",
                [DEMO_SCHEMA],
            )
            row = await cur.fetchone()
            comment = row[0] if row else None
            if not (comment or "").startswith(_MARKER_PREFIX):
                raise DemoError(
                    f"schema {DEMO_SCHEMA!r} exists but was not created by `mcpg --demo` "
                    f"(missing marker comment) — refusing to drop it"
                )
            await cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(DEMO_SCHEMA)))
        await conn.commit()
    except psycopg.Error as exc:
        raise DemoError(f"drop failed ({type(exc).__name__}): {exc}") from exc
    finally:
        await conn.close()
    return DemoDropSummary(schema=DEMO_SCHEMA, dropped=True)


# Printed by the CLI after a successful seed: the "what do I ask now?"
# bridge between having data and knowing what MCPg can do with it.
SUGGESTED_PROMPTS: list[str] = [
    "Summarise the mcpg_demo schema — what tables are there and how do they relate?",
    "What were the top 5 customers by total spend in the last 90 days?",
    "Why is this slow: SELECT * FROM mcpg_demo.orders WHERE customer_id = 42 ORDER BY order_date DESC",
    "Recommend indexes for the mcpg_demo schema and explain the reasoning.",
    "Search the product reviews for complaints about battery life.",
    "Run a database audit on the mcpg_demo schema — any naming or PII concerns?",
]

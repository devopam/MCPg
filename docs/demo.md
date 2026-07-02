# The MCPg demo dataset — a guided tour

Seed a small, curated e-commerce dataset into any scratch database and
take MCPg's pivotal tools for a spin against data engineered to show
them off:

```bash
MCPG_DATABASE_URL=postgresql://... mcpg --demo    # seed the mcpg_demo schema
MCPG_DATABASE_URL=postgresql://... mcpg --demo-drop   # remove it again
```

The dataset (~400 customers, 120 products, 3,000 orders, 900 reviews)
is deterministic — same rows every seed — and **curated**: it plants a
missing foreign-key index, PII-shaped columns, a naming violation, and
review prose worth searching, so every tool below has something real to
find. Everything lives in one `mcpg_demo` schema; nothing else in
your database is touched.

> Every output block below is captured from a real run against the
> seeded dataset (regenerate with
> `uv run python tools/generate_demo_walkthrough.py`). Numbers you see
> here are the numbers you'll get.

## Get oriented: what does this table look like?

**You ask:** *Summarise the orders table — shape, columns, indexes, a few sample rows.*

**MCPg runs:** summarize_table(schema="mcpg_demo", table="orders", sample_rows=3)

```json
{
  "schema": "mcpg_demo",
  "table": "orders",
  "columns": [
    {
      "name": "order_id",
      "data_type": "integer",
      "nullable": false,
      "default": null,
      "vector_dimension": null
    },
    {
      "name": "customer_id",
      "data_type": "integer",
      "nullable": false,
      "default": null,
      "vector_dimension": null
    },
    {
      "name": "status",
      "data_type": "text",
      "nullable": false,
      "default": null,
      "vector_dimension": null
    },
    {
      "name": "order_date",
      "data_type": "timestamp with time zone",
      "nullable": false,
      "default": null,
      "vector_dimension": null
    },
    {
      "name": "total_cents",
      "data_type": "integer",
      "nullable": false,
      "default": null,
      "vector_dimension": null
    }
  ],
  "primary_key": [
    "order_id"
  ],
  "foreign_keys": [
    {
      "name": "orders_customer_id_fkey",
      "from_table": "orders",
      "from_columns": [
        "customer_id"
      ],
      "to_schema": "mcpg_demo",
      "to_table": "customers",
      "to_columns": [
        "customer_id"
      ]
    }
  ],
  "constraints": [
    {
      "name": "orders_customer_id_fkey",
      "type": "foreign_key",
      "definition": "FOREIGN KEY (customer_id) REFERENCES mcpg_demo.customers(customer_id)"
    },
    {
      "name": "orders_pkey",
      "type": "primary_key",
      "definition": "PRIMARY KEY (order_id)"
    }
  ],
  "indexes": [
    {
      "name": "orders_pkey",
      "method": "btree",
      "definition": "CREATE UNIQUE INDEX orders_pkey ON mcpg_demo.orders USING btree (order_id)",
      "partitioned": false
    }
  ],
  "stats": {
    "estimated_row_count": 3000,
    "total_size_bytes": 327680,
    "table_size_bytes": 237568,
    "indexes_size_bytes": 90112,
    "seq_scans": 1,
    "index_scans": 7379,
    "last_vacuum": null,
    "last_autovacuum": null,
    "last_analyze": "2026-07-02 15:34:44.154858+00",
    "last_autoanalyze": null
  },
  "sample_rows": [
    {
      "order_id": 1,
      "customer_id": 25,
      "status": "delivered",
      "order_date": "2025-02-07 14:18:00+00:00",
      "total_cents": 54197
    },
    {
      "order_id": 2,
      "customer_id": 328,
      "status": "shipped",
      "order_date": "2025-07-13 02:00:00+00:00",
      "total_cents": 137591
    },
    {
      "order_id": 3,
      "customer_id": 320,
      "status": "delivered",
      "order_date": "2025-11-25 20:30:00+00:00",
      "total_cents": 265992
    }
  ]
}
```

---

## Ask an analytics question in SQL

**You ask:** *What's the monthly order volume and revenue for the last six months (excluding cancellations)?*

**MCPg runs:** run_select(sql=...)

```sql
SELECT date_trunc('month', order_date)::date AS month,
       count(*) AS orders,
       round(sum(total_cents) / 100.0, 2) AS revenue_usd
FROM mcpg_demo.orders
WHERE status <> 'cancelled'
GROUP BY 1 ORDER BY 1 DESC LIMIT 6
```

```json
{
  "columns": [
    "month",
    "orders",
    "revenue_usd"
  ],
  "rows": [
    {
      "month": "2026-06-01",
      "orders": 666,
      "revenue_usd": "793111.14"
    },
    {
      "month": "2026-05-01",
      "orders": 303,
      "revenue_usd": "349732.10"
    },
    {
      "month": "2026-04-01",
      "orders": 199,
      "revenue_usd": "228943.18"
    },
    {
      "month": "2026-03-01",
      "orders": 201,
      "revenue_usd": "223295.76"
    },
    {
      "month": "2026-02-01",
      "orders": 127,
      "revenue_usd": "155562.34"
    },
    {
      "month": "2026-01-01",
      "orders": 143,
      "revenue_usd": "175079.81"
    }
  ],
  "row_count": 6,
  "truncated": false
}
```

---

## Diagnose a slow query

**You ask:** *Why is this slow? `SELECT * FROM mcpg_demo.orders WHERE customer_id = 42 ORDER BY order_date DESC`*

**MCPg runs:** analyze_query_plan(sql="SELECT * FROM mcpg_demo.orders WHERE customer_id = 42 ORDER BY order_date DESC")

```json
{
  "total_cost": 62.77,
  "estimated_rows": 13,
  "node_types": [
    "Seq Scan",
    "Sort"
  ],
  "sequential_scans": [
    "orders"
  ],
  "actual_total_time_ms": null,
  "actual_rows": null,
  "shared_blocks_read": null,
  "shared_blocks_hit": null,
  "io_read_time_ms": null,
  "io_write_time_ms": null,
  "aio_read_blocks": null,
  "aio_write_blocks": null
}
```

The plan shows a **sequential scan over every order** to find one customer's rows — `orders.customer_id` is a foreign key with no covering index. That's the dataset's planted flaw, and exactly what the next tool catches.

---

## Let the index advisor find it

**You ask:** *Recommend indexes for this database and explain the reasoning.*

**MCPg runs:** recommend_indexes(min_live_tuples=1000)

```json
[
  {
    "schema": "mcpg_demo",
    "table": "orders",
    "seq_scans": 10,
    "live_tuples": 3000,
    "reason": "large table read mostly by sequential scan",
    "suggestions": [
      {
        "column": "status",
        "index_type": "gin_trgm",
        "rationale": "trigram GIN (pg_trgm) accelerates LIKE/ILIKE pattern search"
      }
    ],
    "partitioned": false
  }
]
```

---

## Search customer reviews in natural language

**You ask:** *Find reviews that mention battery life.*

**MCPg runs:** full_text_search(schema="mcpg_demo", table="reviews", column="review_text", search_query='"battery life"', limit=5)

```json
[
  {
    "value": "The Solstice Robot Vacuum is decent for the price, though the battery life could be better.",
    "rank": 0.09910322
  },
  {
    "value": "Absolutely love the Cascade Bluetooth Speaker \u2014 the battery life is outstanding and shipping was fast.",
    "rank": 0.09910322
  },
  {
    "value": "The Meridian Bluetooth Speaker arrived with a damaged box and the battery life is far below what was promised.",
    "rank": 0.09910322
  },
  {
    "value": "Five months in and the Kestrel Robot Vacuum still performs like new. The battery life stands out.",
    "rank": 0.09910322
  },
  {
    "value": "The Cascade Fitness Tracker is decent for the price, though the battery life could be better.",
    "rank": 0.09910322
  }
]
```

---

## Audit for PII and naming drift

**You ask:** *Scan the schema for sensitive columns and naming-convention violations.*

**MCPg runs:** find_sensitive_columns(schema="mcpg_demo") + lint_naming_conventions(schema="mcpg_demo")

```json
{
  "schema": "mcpg_demo",
  "columns": [
    {
      "schema": "mcpg_demo",
      "table": "customers",
      "column": "full_name",
      "data_type": "text",
      "categories": [
        "identifier"
      ],
      "confidence": "low",
      "reasons": [
        "personal name"
      ]
    },
    {
      "schema": "mcpg_demo",
      "table": "customers",
      "column": "email",
      "data_type": "text",
      "categories": [
        "contact"
      ],
      "confidence": "medium",
      "reasons": [
        "personal email address"
      ]
    },
    {
      "schema": "mcpg_demo",
      "table": "customers",
      "column": "phone",
      "data_type": "text",
      "categories": [
        "contact"
      ],
      "confidence": "medium",
      "reasons": [
        "personal phone number"
      ]
    }
  ]
}

{
  "schema": "mcpg_demo",
  "schema_majority_style": "snake_case",
  "findings": [
    {
      "rule": "column_naming_inconsistent",
      "object": "mcpg_demo.reviews.reviewSource",
      "style": "camelCase",
      "majority_style": "snake_case",
      "message": "column 'reviewSource' on 'reviews' uses camelCase but the table majority is snake_case"
    },
    {
      "rule": "index_unexpected_prefix",
      "object": "mcpg_demo.order_items_order_id_idx",
      "style": "other",
      "majority_style": "",
      "message": "index 'order_items_order_id_idx' on 'order_items' does not start with any of ['idx_', 'ix_', 'pk_', 'uq_', 'fk_', 'gin_', 'gist_', 'brin_', 'hnsw_']"
    },
    {
      "rule": "index_unexpected_prefix",
      "object": "mcpg_demo.order_items_product_id_idx",
      "style": "other",
      "majority_style": "",
      "message": "index 'order_items_product_id_idx' on 'order_items' does not start with any of ['idx_', 'ix_', 'pk_', 'uq_', 'fk_', 'gin_', 'gist_', 'brin_', 'hnsw_']"
    },
    {
      "rule": "index_unexpected_prefix",
      "object": "mcpg_demo.reviews_product_id_idx",
      "style": "other",
      "majority_style": "",
      "message": "index 'reviews_product_id_idx' on 'reviews' does not start with any of ['idx_', 'ix_', 'pk_', 'uq_', 'fk_', 'gin_', 'gist_', 'brin_', 'hnsw_']"
    }
  ]
}
```

`customers.email` / `customers.phone` are flagged as PII, and the camelCase `reviews."reviewSource"` column trips the naming linter — both planted on purpose.

---

## Project the schema into a property graph

**You ask:** *Model this schema as a graph — customers, products, orders as vertices; foreign keys as edges.*

**MCPg runs:** generate_graph_projection(schema="mcpg_demo")

```json
{
  "available": false,
  "schema": "mcpg_demo",
  "graph_name": "g",
  "row_limit": 0,
  "node_labels": [
    {
      "label": "customers",
      "source_table": "customers",
      "key_columns": [
        "customer_id"
      ],
      "property_columns": [
        "customer_id",
        "full_name",
        "email",
        "phone",
        "country",
        "... (2 more)"
      ]
    },
    {
      "label": "order_items",
      "source_table": "order_items",
      "key_columns": [
        "order_item_id"
      ],
      "property_columns": [
        "order_item_id",
        "order_id",
        "product_id",
        "quantity",
        "unit_price_cents"
      ]
    },
    {
      "label": "orders",
      "source_table": "orders",
      "key_columns": [
        "order_id"
      ],
      "property_columns": [
        "order_id",
        "customer_id",
        "status",
        "order_date",
        "total_cents"
      ]
    },
    {
      "label": "products",
      "source_table": "products",
      "key_columns": [
        "product_id"
      ],
      "property_columns": [
        "product_id",
        "sku",
        "product_name",
        "category",
        "price_cents",
        "... (1 more)"
      ]
    },
    {
      "label": "reviews",
      "source_table": "reviews",
      "key_columns": [
        "review_id"
      ],
      "property_columns": [
        "review_id",
        "product_id",
        "customer_id",
        "rating",
        "review_text",
        "... (2 more)"
      ]
    }
  ],
  "edge_types": [
    {
      "edge_type": "order_items_order_id_fkey",
      "from_label": "order_items",
      "to_label": "orders",
      "from_key": [
        "order_id"
      ],
      "to_key": [
        "order_id"
      ],
      "fk_name": "order_items_order_id_fkey"
    },
    {
      "edge_type": "order_items_product_id_fkey",
      "from_label": "order_items",
      "to_label": "products",
      "from_key": [
        "product_id"
      ],
      "to_key": [
        "product_id"
      ],
      "fk_name": "order_items_product_id_fkey"
    },
    {
      "edge_type": "orders_customer_id_fkey",
      "from_label": "orders",
      "to_label": "customers",
      "from_key": [
        "customer_id"
      ],
      "to_key": [
        "customer_id"
      ],
      "fk_name": "orders_customer_id_fkey"
    },
    {
      "edge_type": "reviews_customer_id_fkey",
      "from_label": "reviews",
      "to_label": "customers",
      "from_key": [
        "customer_id"
      ],
      "to_key": [
        "customer_id"
      ],
      "fk_name": "reviews_customer_id_fkey"
    },
    {
      "edge_type": "reviews_product_id_fkey",
      "from_label": "reviews",
      "to_label": "products",
      "from_key": [
        "product_id"
      ],
      "to_key": [
        "product_id"
      ],
      "fk_name": "reviews_product_id_fkey"
    }
  ],
  "cypher_statements": [
    "SELECT * FROM cypher('g', $$ CREATE (:customers {customer_id: $customer_id, full_name: $full_name, email: $email, phone: $phone, country: $country, signup_date: $signup_date, marketing_opt_in: $marketing_opt_in}) $$);",
    "SELECT * FROM cypher('g', $$ CREATE (:order_items {order_item_id: $order_item_id, order_id: $order_id, product_id: $product_id, quantity: $quantity, unit_price_cents: $unit_price_cents}) $$);",
    "SELECT * FROM cypher('g', $$ CREATE (:orders {order_id: $order_id, customer_id: $customer_id, status: $status, order_date: $order_date, total_cents: $total_cents}) $$);",
    "SELECT * FROM cypher('g', $$ CREATE (:products {product_id: $product_id, sku: $sku, product_name: $product_name, category: $category, price_cents: $price_cents, description: $description}) $$);",
    "SELECT * FROM cypher('g', $$ CREATE (:reviews {review_id: $review_id, product_id: $product_id, customer_id: $customer_id, rating: $rating, review_text: $review_text, reviewSource: $reviewSource, created_at: $created_at}) $$);",
    "... (5 more)"
  ],
  "warnings": [
    "AGE materializes this projection \u2014 running the statements LOADS a copy of the data into the graph (it is not a virtual view over the tables).",
    "Run the statements in order: all node CREATE statements before the edge MERGE statements.",
    "Apache AGE does not appear to be installed (ag_catalog.ag_graph is unavailable); install AGE and create the graph before running these statements."
  ],
  "detail": "schema-level projection plan for 'mcpg_demo' \u2192 graph 'g': 5 node label(s), 5 edge type(s). Template statements only; no rows were read."
}
```

The openCypher statements are **generated for review, never executed** — the same emit-don't-execute pattern `generate_test_data` and `recommend_redistribute` follow.

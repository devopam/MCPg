"""Load a TPC-H dataset into PostgreSQL, reproducibly, without committing GBs.

Uses DuckDB's ``tpch`` extension to generate the data at a given scale factor,
streams each table to a temporary CSV, and ``COPY``s it into PostgreSQL — so no
multi-gigabyte file is ever committed. The schema/index DDL and this loader
*are* committed; the data is regenerated locally.

    uv run python -m benchmarks.datasets.load_tpch \
        --database-url "$MCPG_TEST_DATABASE_URL" --scale-factor 1

``duckdb`` is a dev-only dependency (the ``bench`` group); it is not required to
run MCPg itself. Fallback for users who already have TPC-H ``dbgen`` output:
``COPY`` the ``.tbl`` files directly after applying ``tpch_schema.sql``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import psycopg

_DIR = Path(__file__).resolve().parent
_TABLES = ("region", "nation", "part", "supplier", "partsupp", "customer", "orders", "lineitem")


async def load(database_url: str, scale_factor: int) -> None:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - dev-only dependency
        raise SystemExit(
            "duckdb is required to generate TPC-H data. Install the bench group:\n"
            "  uv sync --group bench\n"
            "or COPY existing dbgen .tbl files after applying tpch_schema.sql."
        ) from exc

    schema_sql = (_DIR / "tpch_schema.sql").read_text()
    index_sql = (_DIR / "tpch_indexes.sql").read_text()

    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        print("applying schema ...")
        await conn.execute(schema_sql)

        with tempfile.TemporaryDirectory() as tmp:
            duck = duckdb.connect()
            duck.execute("INSTALL tpch; LOAD tpch;")
            print(f"generating TPC-H SF{scale_factor} (this can take a while) ...")
            duck.execute(f"CALL dbgen(sf={scale_factor})")
            for table in _TABLES:
                csv_path = Path(tmp) / f"{table}.csv"
                duck.execute(f"COPY {table} TO '{csv_path}' (FORMAT csv, HEADER false)")
                async with conn.cursor() as cur:
                    async with cur.copy(f"COPY {table} FROM STDIN (FORMAT csv)") as copy:
                        with csv_path.open("rb") as fh:
                            while chunk := fh.read(1 << 20):
                                await copy.write(chunk)
                csv_path.unlink()
                print(f"  loaded {table}")
            duck.close()

        print("applying indexes ...")
        await conn.execute(index_sql)
        print("VACUUM ANALYZE ...")
        for table in _TABLES:
            await conn.execute(f"VACUUM (ANALYZE) {table}")
    print(f"TPC-H SF{scale_factor} loaded.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load TPC-H into PostgreSQL for the MCPg benchmark.")
    parser.add_argument("--database-url", required=True, help="Target PostgreSQL DSN.")
    parser.add_argument("--scale-factor", type=int, default=1, help="TPC-H scale factor (1 dev, 10 published).")
    args = parser.parse_args(argv)
    asyncio.run(load(args.database_url, args.scale_factor))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

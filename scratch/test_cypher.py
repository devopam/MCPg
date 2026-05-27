import asyncio
import sys

import psycopg

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main():
    conn_string = "postgresql://postgres:postgres@localhost:5433/mcpg_test"
    print("Connecting...")
    async with await psycopg.AsyncConnection.connect(conn_string) as conn:
        async with conn.cursor() as cur:
            await cur.execute("LOAD 'age';")
            await cur.execute("SET search_path = ag_catalog, public;")
            # 1. Create a person node inside 'mcp_graph'
            await cur.execute("""
                SELECT * FROM cypher('mcp_graph', $$
                    CREATE (n:Person {name: 'Charlie', age: 35})
                    RETURN n
                $$) as (n agtype);
            """)
            row = await cur.fetchone()
            print("Charlie Row:", row)
            print("Charlie Type:", type(row[0]) if row else None)

            # 2. Query all nodes with jsonb cast
            await cur.execute("""
                SELECT (n::text)::jsonb FROM cypher('mcp_graph', $$
                    MATCH (n)
                    RETURN n
                $$) as (n agtype);
            """)
            rows = await cur.fetchall()
            print("All Rows (JSONB cast):")
            for r in rows or []:
                print(f"Val: {r[0]} | Type: {type(r[0])}")


if __name__ == "__main__":
    asyncio.run(main())

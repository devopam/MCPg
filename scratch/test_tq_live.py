import sys
import asyncio
import random
from mcpg.database import Database
from mcpg.config import load_settings
from mcpg import turboquant

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def main():
    db_url = "postgresql://postgres:postgres@localhost:5433/mcpg_test"
    settings = load_settings({"MCPG_DATABASE_URL": db_url})
    
    async with Database(settings) as db:
        driver = db.driver()
        print("Connected to database.")
        
        # 1. Clean up and recreate test table
        print("Recreating test table mcpg_demo.tq_test...")
        await db.run_unmanaged("SET search_path TO mcpg_demo, public; DROP TABLE IF EXISTS mcpg_demo.tq_test CASCADE")
        await db.run_unmanaged(
            "SET search_path TO mcpg_demo, public; "
            "CREATE TABLE mcpg_demo.tq_test ("
            "  id serial PRIMARY KEY,"
            "  title text NOT NULL,"
            "  embedding vector(128)"
            ")"
        )
        
        # 2. Seed 200 items
        print("Seeding 200 random 128-dim vectors...")
        records = []
        random.seed(42)
        for i in range(200):
            vec = [random.uniform(-1.0, 1.0) for _ in range(128)]
            # Normalize to unit length
            norm = sum(x*x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
            records.append((f"Article {i+1}", f"[{','.join(map(str, vec))}]"))
            
        pool = await db._pool.pool_connect()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET search_path TO mcpg_demo, public")
                await cur.executemany(
                    "INSERT INTO mcpg_demo.tq_test (title, embedding) VALUES (%s, %s::vector)",
                    records
                )
            await conn.commit()
        # Debug search path and OIDs
        pool = await db._pool.pool_connect()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET search_path TO mcpg_demo, public")
                await cur.execute("SHOW search_path")
                path = (await cur.fetchone())[0]
                await cur.execute("SELECT 'vector'::regtype::oid, 'halfvec'::regtype::oid")
                vec_oid, half_oid = await cur.fetchone()
                print(f"DEBUG: search_path={path}, vector_oid={vec_oid}, halfvec_oid={half_oid}")
            await conn.commit()

        # 3. Create a pg_turboquant index
        print("Building pg_turboquant ANN index (USING turboquant)...")
        # We run it unmanaged because CREATE INDEX CONCURRENTLY cannot run inside a transaction
        await db.run_unmanaged(
            "SET search_path TO mcpg_demo, public; "
            "CREATE INDEX tq_test_idx ON mcpg_demo.tq_test "
            "USING turboquant (embedding tq_l2_ops) WITH (bits=8, lists=10)"
        )
        print("Index build successful.")
        
        # 4. Verify list and metadata tools
        print("\nListing TurboQuant indexes:")
        indexes = await turboquant.list_turboquant_indexes(driver)
        for idx in indexes:
            print(f"  - {idx.schema}.{idx.index} on table {idx.table} (column: {idx.column})")
            print(f"    Options: {idx.index_options}")
            print(f"    Delta Enabled: {idx.delta_enabled} | Merge Recommended: {idx.delta_merge_recommended}")
            
        # 5. Perform approximate candidate retrieval
        print("\nPerforming approximate search...")
        query_vec = [random.uniform(-1.0, 1.0) for _ in range(128)]
        norm = sum(x*x for x in query_vec) ** 0.5
        if norm > 0:
            query_vec = [x / norm for x in query_vec]
        # We run standard vector operators, pgvector or tq queries
        rows = await driver.execute_query(
            "SELECT id, title, embedding <-> %s::vector AS distance "
            "FROM mcpg_demo.tq_test "
            "ORDER BY embedding <-> %s::vector "
            "LIMIT 5",
            params=[f"[{','.join(map(str, query_vec))}]", f"[{','.join(map(str, query_vec))}]"]
        )
        print("Results:")
        for r in rows:
            print(f"  - ID {r.cells['id']} ({r.cells['title']}): Distance = {r.cells['distance']:.4f}")
            
        # 6. Run maintenance advisor
        print("\nRunning pg_turboquant maintenance checks:")
        findings = await turboquant.recommend_turboquant_maintenance(driver)
        if not findings:
            print("  [OK] No maintenance recommended.")
        else:
            for f in findings:
                print(f"  [{f.severity}] {f.schema}.{f.index}: {f.evidence} (Action: {f.suggested_action})")

if __name__ == "__main__":
    asyncio.run(main())

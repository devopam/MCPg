import asyncio
import sys

import psycopg

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main():
    conn_string = "postgresql://postgres:postgres@localhost:5433/mcpg_test"
    print(f"Connecting to database at {conn_string}...")

    async with await psycopg.AsyncConnection.connect(conn_string) as conn:
        async with conn.cursor() as cur:
            print("Creating schema 'mcp_sample'...")
            await cur.execute("DROP SCHEMA IF EXISTS mcp_sample CASCADE;")
            await cur.execute("CREATE SCHEMA mcp_sample;")

            # Enable extensions in case they are not enabled yet
            await cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            await cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
            await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

            print("Creating tables...")
            # 1. Authors table
            await cur.execute("""
                CREATE TABLE mcp_sample.authors (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    email VARCHAR(100) UNIQUE,
                    bio TEXT
                );
            """)

            # 2. Books table with pgvector column (3 dimensions)
            await cur.execute("""
                CREATE TABLE mcp_sample.books (
                    id SERIAL PRIMARY KEY,
                    author_id INTEGER REFERENCES mcp_sample.authors(id) ON DELETE CASCADE,
                    title VARCHAR(200) NOT NULL,
                    description TEXT,
                    embedding vector(3),
                    price NUMERIC(10,2) CHECK (price >= 0),
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 3. Users table (to test sensitive column scanner)
            await cur.execute("""
                CREATE TABLE mcp_sample.users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) NOT NULL,
                    hashed_password VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 4. Reviews table (intentionally leave user_id unindexed to check advisor recommendations!)
            await cur.execute("""
                CREATE TABLE mcp_sample.reviews (
                    id SERIAL PRIMARY KEY,
                    book_id INTEGER NOT NULL REFERENCES mcp_sample.books(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES mcp_sample.users(id) ON DELETE CASCADE,
                    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                    comment TEXT
                );
            """)

            # 5. Stores table with PostGIS geometry location
            await cur.execute("""
                CREATE TABLE mcp_sample.stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    location geometry(Point, 4326)
                );
            """)

            print("Creating indexes...")
            # Trigram GIN index for title search
            await cur.execute("""
                CREATE INDEX idx_books_title_trgm ON mcp_sample.books USING gin (title gin_trgm_ops);
            """)

            # HNSW index for vector cosine similarity search
            await cur.execute("""
                CREATE INDEX idx_books_embedding_hnsw ON mcp_sample.books USING hnsw (embedding vector_cosine_ops);
            """)

            # GiST index for PostGIS coordinates
            await cur.execute("""
                CREATE INDEX idx_stores_location_gist ON mcp_sample.stores USING gist (location);
            """)

            # Index on foreign key reviews.book_id, but intentionally skip
            # reviews.user_id to test indexing recommendations
            await cur.execute("""
                CREATE INDEX idx_reviews_book_id ON mcp_sample.reviews (book_id);
            """)

            # Seeding data
            print("Seeding authors...")
            authors_data = [
                ("George Orwell", "orwell@example.com", "English novelist, essayist, journalist, and critic."),
                ("Jane Austen", "austen@example.com", "English novelist known primarily for her six major novels."),
                ("William Shakespeare", "shakespeare@example.com", "English playwright, poet, and actor."),
            ]
            author_ids = []
            for name, email, bio in authors_data:
                await cur.execute(
                    "INSERT INTO mcp_sample.authors (name, email, bio) VALUES (%s, %s, %s) RETURNING id;",
                    (name, email, bio),
                )
                author_ids.append((await cur.fetchone())[0])

            print("Seeding books...")
            # We use 3 dimensions: [Sci-fi/Politics, Romance/Classics, Drama/Tragedy]
            books_data = [
                (
                    author_ids[0],
                    "1984",
                    "A dystopian social science fiction novel and cautionary tale.",
                    "[0.95, 0.05, 0.10]",
                    14.99,
                ),
                (
                    author_ids[0],
                    "Animal Farm",
                    "A satirical allegorical novella about democratic socialism.",
                    "[0.85, 0.10, 0.40]",
                    9.99,
                ),
                (
                    author_ids[1],
                    "Pride and Prejudice",
                    "A romantic novel of manners following Elizabeth Bennet.",
                    "[0.05, 0.95, 0.20]",
                    12.50,
                ),
                (
                    author_ids[1],
                    "Sense and Sensibility",
                    "A classic romance novel depicting the lives of the Dashwood sisters.",
                    "[0.08, 0.90, 0.15]",
                    10.99,
                ),
                (
                    author_ids[2],
                    "Hamlet",
                    "A tragedy depicting the revenge Prince Hamlet wreaks on his uncle Claudius.",
                    "[0.10, 0.15, 0.95]",
                    8.99,
                ),
            ]
            book_ids = []
            for author_id, title, desc, embed, price in books_data:
                await cur.execute(
                    "INSERT INTO mcp_sample.books "
                    "(author_id, title, description, embedding, price) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id;",
                    (author_id, title, desc, embed, price),
                )
                book_ids.append((await cur.fetchone())[0])

            print("Seeding users...")
            users_data = [
                ("admin", "$2b$12$LDRv8xZp.1Gv12f3k9JmU.c5P/e7hY5rG0N0n3n9k2K3a2L4g5h6j"),  # Fake bcrypt hash
                ("alice", "$2b$12$K1d8v9Xz.2Gv13f4k0JmV.d6Q/f8iZ6sH1O1o4o0l3L4b3M5h7i8j"),
            ]
            user_ids = []
            for username, pwhash in users_data:
                await cur.execute(
                    "INSERT INTO mcp_sample.users (username, hashed_password) VALUES (%s, %s) RETURNING id;",
                    (username, pwhash),
                )
                user_ids.append((await cur.fetchone())[0])

            print("Seeding reviews...")
            reviews_data = [
                (book_ids[0], user_ids[1], 5, "An absolute masterpiece. Still highly relevant today."),
                (book_ids[2], user_ids[1], 5, "I love Elizabeth Bennet and Mr. Darcy's banter. Classic!"),
                (book_ids[4], user_ids[0], 4, "A bit long, but the dramatic tension is unmatched."),
            ]
            for book_id, user_id, rating, comment in reviews_data:
                await cur.execute(
                    "INSERT INTO mcp_sample.reviews (book_id, user_id, rating, comment) VALUES (%s, %s, %s, %s);",
                    (book_id, user_id, rating, comment),
                )

            print("Seeding stores...")
            # Locations using SRID 4326: Point(longitude, latitude)
            stores_data = [
                ("Downtown Bookshop", "SRID=4326;POINT(-73.9857 40.7484)"),  # Manhattan, Empire State
                ("Uptown Books", "SRID=4326;POINT(-73.9580 40.8003)"),  # Columbia Uni area
                ("Brooklyn Literary Hub", "SRID=4326;POINT(-73.9903 40.6929)"),  # DUMBO area
            ]
            for name, wkt in stores_data:
                await cur.execute(
                    "INSERT INTO mcp_sample.stores (name, location) VALUES (%s, ST_GeomFromEWKT(%s));", (name, wkt)
                )

            # Commit the transaction
            await conn.commit()
            print("Database setup and seeding completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())

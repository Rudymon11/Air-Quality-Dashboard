"""
setup_vector_table.py

Run once to create the aqi_summaries table that stores
weekly per-city text summaries alongside their embeddings.

Usage:
    python rag/setup_vector_table.py
"""

import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


def setup(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS aqi_summaries (
                id          SERIAL PRIMARY KEY,
                city        TEXT NOT NULL,
                week_start  DATE NOT NULL,
                week_end    DATE NOT NULL,
                summary     TEXT NOT NULL,
                embedding   vector(384),
                UNIQUE (city, week_start)
            );
        """)
    conn.commit()
    print("aqi_summaries table and index ready.")


if __name__ == "__main__":
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    try:
        setup(conn)
    finally:
        conn.close()

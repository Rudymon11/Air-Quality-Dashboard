"""
load_backfill_to_postgres.py

One-time migration script: loads the CSV produced by
backfill_openaq_from_aws() (raw_aqi_readings_backfill.csv) into the same
Postgres table used by the live pipeline (raw_aqi_readings).

Why this exists as a separate script:
The backfill function writes straight to CSV rather than going through
_save(), so it never touches Postgres on its own -- this closes that gap
as a one-time step, without changing the backfill's own behavior.

Uses Postgres's native COPY (via psycopg2) rather than pandas.to_sql(),
since COPY is dramatically faster for bulk-loading tens of thousands of
rows -- to_sql() inserts row by row (or in small batches) under the hood,
which gets slow fast at this scale.

Usage:
    python load_backfill_to_postgres.py
"""

import os
import io
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CSV_PATH = "raw_aqi_readings_backfill.csv"
TABLE_NAME = "raw_aqi_readings"


def create_table_if_not_exists(conn):
    """
    Explicit schema instead of letting pandas infer column types --
    guarantees reading_time_utc and ingested_at land as proper timestamp
    columns rather than text, regardless of what pandas guesses.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                city TEXT,
                station TEXT,
                pollutant TEXT,
                value DOUBLE PRECISION,
                unit TEXT,
                reading_time_utc TIMESTAMPTZ,
                source TEXT,
                ingested_at TIMESTAMPTZ
            );
        """)
    conn.commit()


def load_csv_via_copy(conn, csv_path):
    """
    Streams the CSV straight into Postgres using COPY, which is the
    fastest bulk-load path Postgres offers -- far quicker than inserting
    row by row.
    """
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df)} rows from {csv_path} into memory for transfer.")

    # Normalize timestamps so Postgres can parse them cleanly via COPY.
    df["reading_time_utc"] = pd.to_datetime(df["reading_time_utc"], errors="coerce", utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], errors="coerce", utc=True)

    buffer = io.StringIO()
    df.to_csv(buffer, index=False, header=False)
    buffer.seek(0)

    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {TABLE_NAME} ({', '.join(df.columns)}) FROM STDIN WITH CSV",
            buffer,
        )
    conn.commit()
    print(f"Copied {len(df)} rows into Postgres table '{TABLE_NAME}'.")


if __name__ == "__main__":
    if not os.path.exists(CSV_PATH):
        raise SystemExit(f"{CSV_PATH} not found -- nothing to load.")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set in .env -- can't connect to Postgres.")

    conn = psycopg2.connect(db_url)
    try:
        create_table_if_not_exists(conn)
        load_csv_via_copy(conn, CSV_PATH)
    finally:
        conn.close()
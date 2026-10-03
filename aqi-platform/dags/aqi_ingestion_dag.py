from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import timedelta
import pendulum
import os
import sys

PROJECT_DIR = os.getenv(
    "AQI_PROJECT_DIR",
    "/mnt/c/Users/5510s/Downloads/Data Projects/aqi-platform"  # fallback default
)


def run_ingestion_task():
    """
    The function Airflow actually executes. Imports are deliberately kept
    INSIDE this function rather than at the top of the DAG file --
    Airflow's scheduler re-parses this file on a fixed interval regardless
    of the DAG's own schedule, so top-level imports of pandas/requests would
    otherwise be paid repeatedly just for the file to sit in the dags folder.
    """
    sys.path.append(PROJECT_DIR)
    from ingest import fetch_all, _save

    print("Starting scheduled ingestion...")
    df = fetch_all()
    if not df.empty:
        _save(df, table_name="raw_aqi_readings")
    else:
        print("No new data fetched.")


def refresh_materialized_view():
    import psycopg2
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(dotenv_path=Path(PROJECT_DIR) / ".env")
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY stg_aqi_readings_mat")
    conn.close()
    print("stg_aqi_readings_mat refreshed.")


def run_dbt():
    import subprocess
    from pathlib import Path
    dbt_project_dir = str(Path(PROJECT_DIR) / "aqi_transform")
    result = subprocess.run(
        ["dbt", "run", "--project-dir", dbt_project_dir],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(f"dbt run failed:\n{result.stderr}")


def refresh_embeddings():
    """
    Re-embeds only when the current ISO week is not yet in aqi_summaries.
    Runs at most once per week regardless of how often the DAG fires.
    """
    import psycopg2
    import pendulum
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(dotenv_path=Path(PROJECT_DIR) / ".env")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    try:
        # Current week_start (Monday) in UTC
        current_week_start = pendulum.now("UTC").start_of("week").date()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM aqi_summaries WHERE week_start = %s LIMIT 1",
                (current_week_start,)
            )
            already_embedded = cur.fetchone() is not None

        if already_embedded:
            print(f"Embeddings for week {current_week_start} already exist — skipping.")
            return

        print(f"New week {current_week_start} detected — regenerating embeddings.")
        sys.path.append(str(Path(PROJECT_DIR) / "rag"))
        from embed import fetch_weekly_stats, upsert_summaries
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        rows = fetch_weekly_stats(conn)
        print(f"Fetched {len(rows)} city-week rows.")
        upsert_summaries(conn, rows, model)
    finally:
        conn.close()


default_args = {
    "owner": "rudy",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="india_aqi_ingestion",
    default_args=default_args,
    description="Hourly ingestion of CPCB and OpenAQ data",
    schedule="@hourly",
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
) as dag:

    ingest_task = PythonOperator(
        task_id="fetch_and_save_aqi",
        python_callable=run_ingestion_task,
        execution_timeout=timedelta(minutes=20),
    )

    refresh_task = PythonOperator(
        task_id="refresh_stg_mat_view",
        python_callable=refresh_materialized_view,
        execution_timeout=timedelta(minutes=30),
    )

    dbt_task = PythonOperator(
        task_id="run_dbt_models",
        python_callable=run_dbt,
        execution_timeout=timedelta(minutes=15),
    )

    embed_task = PythonOperator(
        task_id="refresh_embeddings",
        python_callable=refresh_embeddings,
        execution_timeout=timedelta(minutes=30),
    )

    ingest_task >> refresh_task >> dbt_task >> embed_task
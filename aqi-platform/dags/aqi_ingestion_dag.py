from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import timedelta
import pendulum
import os
import sys

# Resolve the project directory relative to this DAG file instead of
# hardcoding a machine-specific path -- keeps this portable across machines.
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
    catchup=False,  # don't backfill runs since 2024
    max_active_runs=1,  # prevent overlapping runs if one takes longer than an hour
) as dag:

    ingest_task = PythonOperator(
        task_id="fetch_and_save_aqi",
        python_callable=run_ingestion_task,
        execution_timeout=timedelta(minutes=20),  # fail fast instead of hanging on a stuck API call
    )
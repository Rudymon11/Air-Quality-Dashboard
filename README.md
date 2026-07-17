# Air-Quality-Dashboard
This project builds that pipeline: an automated system that pulls live air quality data for Indian cities, stores and transforms it properly, analyzes pollution trends and patterns, and forecasts future Air Quality Index (AQI) values, all served through an interactive dashboard.

HOW TO RUN:
STEP 1: python ingest.py --backfill     # run once, seeds ~90 days of real history from the AWS archive

STEP 2: python ingest.py                # what your Airflow DAG calls hourly, going forward
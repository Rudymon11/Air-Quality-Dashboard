# Air Quality Intelligence Platform

An end-to-end data engineering + analytics + GenAI project that ingests real-time air quality data for 20 Indian cities, stores and transforms it in a PostgreSQL warehouse, runs exploratory analysis, and serves a RAG-based Q&A system that answers plain-language questions grounded in real sensor data.

**Data sources:** CPCB (data.gov.in) — primary | OpenAQ v3 live — secondary | OpenAQ AWS S3 archive — historical backfill  
**Stack:** Python · PostgreSQL · Airflow · dbt · sentence-transformers · pgvector · Groq

---

## Architecture

```
CPCB API  ──┐
             ├──► ingest.py ──► raw_aqi_readings (Postgres)
OpenAQ API ─┘                        │
                                      ▼
OpenAQ AWS S3 ──► backfill CSV ──► load_backfill_to_postgres.py
                                      │
                                      ▼
                               dbt (aqi_transform/)
                               stg_aqi_readings
                               fct_city_daily_aqi
                                      │
                                      ▼
                               rag/ (embed → pgvector → Groq)
```

---

## Prerequisites

- Python 3.10+
- PostgreSQL 18 with the **pgvector** extension installed
- Git

### Installing pgvector on Windows (one-time)

Requires Visual Studio 2022 Build Tools with C++ and Windows SDK components.  
Open **x64 Native Tools Command Prompt for VS 2022** as administrator and run:

```
set "PGROOT=C:\Program Files\PostgreSQL\18"
cd %TEMP%
git clone --branch v0.8.6 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

Then in psql or pgAdmin:
```sql
CREATE EXTENSION vector;
```

---

## Setup

```bash
cd aqi-platform
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Copy `.env` and fill in your credentials:
```
CPCB_API_KEY=...
OPENAQ_API_KEY=...
DATABASE_URL=postgresql://user:password@host:5432/aqi
GROQ_API_KEY=...
```

---

## Running the Pipeline

### Step 1 — Seed historical data (run once)

Downloads ~90 days of OpenAQ archive data from AWS S3 into a local CSV, then bulk-loads it into Postgres. Resumable — safe to re-run if interrupted.

```bash
python ingest.py --backfill
python load_backfill_to_postgres.py
```

### Step 2 — Live ingestion (hourly, via Airflow)

Fetches current readings from CPCB and OpenAQ and appends them to `raw_aqi_readings`.

```bash
python ingest.py
```

To run on a schedule, start Airflow and enable the `india_aqi_ingestion` DAG:

```bash
airflow standalone
```

Then open `http://localhost:8080`, find `india_aqi_ingestion`, and toggle it on.

### Step 3 — Transform with dbt

Cleans, standardizes, and aggregates raw readings into analysis-ready tables.

```bash
cd aqi_transform
dbt run
dbt test
```

This produces:
- `stg_aqi_readings` — deduplicated, unit-corrected, hardware-filtered readings
- `fct_city_daily_aqi` — daily average pollutant levels per city

### Step 4 — RAG / GenAI Q&A

Embeds weekly per-city summaries into pgvector and enables plain-language Q&A over your sensor data.

**First time only — create the vector table:**
```bash
python rag/setup_vector_table.py
```

**First time only — generate and store embeddings:**
```bash
python rag/embed.py
```

This generates one natural-language summary per city per week (287 total across 18 cities × 17 weeks) from `fct_city_daily_aqi`, embeds them locally using `all-MiniLM-L6-v2`, and stores them in the `aqi_summaries` pgvector table.

Re-run `embed.py` whenever new weeks of data accumulate — it upserts, so no duplicates.

**Start the Q&A interface:**
```bash
python rag/cli.py
```

Example questions:
- `why is Guwahati's air quality bad?`
- `compare Delhi and Mumbai PM2.5 trends`
- `which city had the worst SO2 last month?`

---

## Project Structure

```
aqi-platform/
├── ingest.py                    # CPCB + OpenAQ ingestion, AWS backfill
├── load_backfill_to_postgres.py # one-time bulk load of backfill CSV → Postgres
├── aqi_transform/               # dbt project
│   └── models/
│       ├── staging/stg_aqi_readings.sql
│       └── marts/fct_city_daily_aqi.sql
├── dags/
│   └── aqi_ingestion_dag.py     # Airflow hourly DAG
├── rag/
│   ├── setup_vector_table.py    # creates aqi_summaries table in Postgres
│   ├── embed.py                 # generates summaries + embeddings → pgvector
│   ├── retrieve.py              # cosine similarity search
│   ├── generate.py              # Groq generation with retrieved context
│   └── cli.py                   # interactive Q&A loop
├── eda.ipynb                    # exploratory data analysis notebook
├── requirements.txt
└── .env                         # credentials (never commit this)
```

---

## Key Numbers

| Metric | Value |
|---|---|
| Cities covered | 18 |
| Raw readings in warehouse | 11M+ |
| Data window | April – August 2026 |
| Weekly summaries embedded | 287 |
| Embedding model | all-MiniLM-L6-v2 (384-dim, local) |
| Generation model | Groq (openai/gpt-oss-120b) |

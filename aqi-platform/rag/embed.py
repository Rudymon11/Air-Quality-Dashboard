"""
embed.py

Pulls weekly per-city aggregates directly from stg_aqi_readings (not
fct_city_daily_aqi), giving access to all pollutants including NOX, NO,
NH3, PM1, and weather parameters (TEMP, HUMIDITY, WIND_SPEED, WIND_DIR).
Also computes P90 per pollutant per week to capture spike behaviour that
a simple average would hide.

Usage:
    python rag/embed.py
"""

import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from pathlib import Path
from sentence_transformers import SentenceTransformer

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
os.environ["HF_TOKEN"] = os.getenv("HF_TOKEN", "")

MODEL_NAME = "all-MiniLM-L6-v2"

# Pollutants to include in summaries, split by category
POLLUTANTS = ["PM2.5", "PM10", "PM1", "NO2", "NO", "NOX", "SO2", "CO", "O3", "NH3"]
WEATHER    = ["TEMP", "HUMIDITY", "WIND_SPEED"]


def fetch_weekly_stats(conn):
    """
    Pivot stg_aqi_readings into one row per (city, week) with avg and P90
    for every pollutant and weather parameter.
    """
    all_params = POLLUTANTS + WEATHER
    avg_cols = ",\n".join(
        f"ROUND(AVG(CASE WHEN pollutant = '{p}' THEN pollutant_value END)::numeric, 1) AS avg_{p.lower().replace('.','').replace('-','')}"
        for p in all_params
    )
    p90_cols = ",\n".join(
        f"ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY CASE WHEN pollutant = '{p}' THEN pollutant_value END)::numeric, 1) AS p90_{p.lower().replace('.','').replace('-','')}"
        for p in all_params
    )

    query = f"""
        SELECT
            city,
            DATE_TRUNC('week', reading_time_utc)::date AS week_start,
            (DATE_TRUNC('week', reading_time_utc) + INTERVAL '6 days')::date AS week_end,
            {avg_cols},
            {p90_cols}
        FROM stg_aqi_readings
        GROUP BY city, DATE_TRUNC('week', reading_time_utc)
        ORDER BY city, week_start;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query)
        return cur.fetchall()


def _val(row, prefix, pollutant):
    key = f"{prefix}_{pollutant.lower().replace('.','').replace('-','')}"
    return row.get(key)


def format_summary(row):
    parts = [f"{row['city']}, week of {row['week_start']} to {row['week_end']}:"]

    # --- Particulate matter ---
    for p in ["PM2.5", "PM10", "PM1"]:
        avg = _val(row, "avg", p)
        p90 = _val(row, "p90", p)
        if avg is not None:
            spike = f", spiking to {p90} µg/m³ at P90" if p90 and p90 > avg else ""
            parts.append(f"{p} averaged {avg} µg/m³{spike}.")

    # --- Nitrogen compounds ---
    for p in ["NO2", "NO", "NOX"]:
        avg = _val(row, "avg", p)
        p90 = _val(row, "p90", p)
        if avg is not None:
            spike = f", P90 spike {p90} µg/m³" if p90 and p90 > avg else ""
            parts.append(f"{p} averaged {avg} µg/m³{spike}.")

    # --- Other gases ---
    for p in ["SO2", "CO", "O3", "NH3"]:
        avg = _val(row, "avg", p)
        p90 = _val(row, "p90", p)
        if avg is not None:
            spike = f", P90 spike {p90} µg/m³" if p90 and p90 > avg else ""
            parts.append(f"{p} averaged {avg} µg/m³{spike}.")

    # --- Weather context ---
    temp = _val(row, "avg", "TEMP")
    humidity = _val(row, "avg", "HUMIDITY")
    wind = _val(row, "avg", "WIND_SPEED")
    if temp is not None:
        parts.append(f"Average temperature was {temp}°C.")
    if humidity is not None:
        parts.append(f"Average humidity was {humidity}%.")
    if wind is not None:
        parts.append(f"Average wind speed was {wind} m/s.")

    return " ".join(parts)


def upsert_summaries(conn, rows, model):
    summaries = [(r, format_summary(r)) for r in rows]
    texts = [s for _, s in summaries]

    print(f"Embedding {len(texts)} summaries with {MODEL_NAME}...")
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)

    with conn.cursor() as cur:
        for (row, summary), embedding in zip(summaries, embeddings):
            cur.execute("""
                INSERT INTO aqi_summaries (city, week_start, week_end, summary, embedding)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (city, week_start) DO UPDATE
                    SET week_end  = EXCLUDED.week_end,
                        summary   = EXCLUDED.summary,
                        embedding = EXCLUDED.embedding;
            """, (
                row['city'],
                row['week_start'],
                row['week_end'],
                summary,
                embedding.tolist(),
            ))
    conn.commit()
    print(f"Upserted {len(summaries)} summaries into aqi_summaries.")


if __name__ == "__main__":
    model = SentenceTransformer(MODEL_NAME)
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    try:
        rows = fetch_weekly_stats(conn)
        print(f"Fetched {len(rows)} city-week rows from stg_aqi_readings.")
        upsert_summaries(conn, rows, model)
    finally:
        conn.close()

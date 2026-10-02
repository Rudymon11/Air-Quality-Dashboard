"""
app.py

Flask API + frontend server for the AQI RAG platform.

Endpoints:
    GET  /                          — serves the frontend
    GET  /health                    — confirms the service is up
    GET  /ask?query=<question>      — RAG Q&A, returns answer + sources + raw rows
    GET  /api/cities                — list of distinct cities
    GET  /api/daily?city=&date_from=&date_to=&limit=
                                    — paginated rows from fct_city_daily_aqi
    GET  /api/readings?city=&pollutant=&limit=
                                    — paginated rows from stg_aqi_readings

Usage:
    python rag/app.py
"""

import os
import sys
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
os.environ["HF_TOKEN"] = os.getenv("HF_TOKEN", "")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieve import retrieve
from generate import generate

FRONTEND_DIR = Path(__file__).resolve().parent / "static"
app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="", template_folder=str(FRONTEND_DIR))


def get_conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# RAG Q&A
# ---------------------------------------------------------------------------

@app.route("/ask", methods=["POST"])
def ask():
    body  = request.get_json(force=True)
    query = body.get("query", "").strip()
    history = body.get("history", [])
    if not query:
        return jsonify({"error": "query is required"}), 400

    conn = get_conn()
    try:
        results = retrieve(query, conn, k=18)
        answer = generate(query, results, history)
        sources = [
            {
                "city":       r["city"],
                "week_start": str(r["week_start"]),
                "week_end":   str(r["week_end"]),
                "summary":    r["summary"],
            }
            for r in results
        ]
    finally:
        conn.close()

    return jsonify({"answer": answer, "sources": sources})


# ---------------------------------------------------------------------------
# Cities list
# ---------------------------------------------------------------------------

@app.route("/api/cities")
def cities():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT city FROM fct_city_daily_aqi ORDER BY city")
            return jsonify([r[0] for r in cur.fetchall()])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily aggregates table  (fct_city_daily_aqi)
# ---------------------------------------------------------------------------

@app.route("/api/daily")
def daily():
    city      = request.args.get("city", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to", "").strip()
    pollutant = request.args.get("pollutant", "").strip()
    source    = request.args.get("source", "").strip()
    limit     = min(int(request.args.get("limit", 100)), 500)
    offset    = int(request.args.get("offset", 0))

    filters, params = [], []
    if city:
        filters.append("city = %s");      params.append(city)
    if date_from:
        filters.append("reading_date >= %s"); params.append(date_from)
    if date_to:
        filters.append("reading_date <= %s"); params.append(date_to)
    if source:
        filters.append("source = %s");    params.append(source)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # choose sort column based on pollutant filter
    sort_col_map = {
        "PM2.5": "avg_pm25", "PM10": "avg_pm10", "SO2": "avg_so2",
        "NO2": "avg_no2", "NO": "avg_no", "CO": "avg_co",
        "NH3": "avg_nh3", "O3": "avg_o3",
    }
    sort_col = sort_col_map.get(pollutant.upper(), "reading_date") if pollutant else "reading_date"

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT city,
                       reading_date::date AS reading_date,
                       source,
                       avg_pm25, avg_pm10, avg_so2, avg_no2,
                       avg_no, avg_co, avg_nh3, avg_o3,
                       active_stations
                FROM fct_city_daily_aqi
                {where}
                ORDER BY {sort_col} DESC NULLS LAST
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()

            cur.execute(f"SELECT COUNT(*) FROM fct_city_daily_aqi {where}", params)
            total = cur.fetchone()["count"]

        return jsonify({
            "columns": ["city", "reading_date", "source", "avg_pm25", "avg_pm10",
                        "avg_so2", "avg_no2", "avg_no", "avg_co", "avg_nh3",
                        "avg_o3", "active_stations"],
            "rows":    [dict(r) for r in rows],
            "total":   total,
            "offset":  offset,
            "limit":   limit,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Raw readings table  (stg_aqi_readings)
# ---------------------------------------------------------------------------

@app.route("/api/readings")
def readings():
    city      = request.args.get("city", "").strip()
    pollutant = request.args.get("pollutant", "").strip()
    station   = request.args.get("station", "").strip()
    source    = request.args.get("source", "").strip()
    limit     = min(int(request.args.get("limit", 100)), 500)
    offset    = int(request.args.get("offset", 0))

    filters, params = [], []
    if city:
        filters.append("city = %s");      params.append(city)
    if pollutant:
        filters.append("pollutant = %s"); params.append(pollutant)
    if station:
        filters.append("station ILIKE %s"); params.append(f"%{station}%")
    if source:
        filters.append("source = %s");    params.append(source)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT city, station, pollutant, pollutant_value, unit,
                       reading_time_utc, source
                FROM stg_aqi_readings_mat
                {where}
                ORDER BY reading_time_utc DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()

            cur.execute(f"SELECT COUNT(*) FROM stg_aqi_readings_mat {where}", params)
            total = cur.fetchone()["count"]

        return jsonify({
            "columns": ["city", "station", "pollutant", "pollutant_value",
                        "unit", "reading_time_utc", "source"],
            "rows":    [dict(r) for r in rows],
            "total":   total,
            "offset":  offset,
            "limit":   limit,
        })
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=5001)

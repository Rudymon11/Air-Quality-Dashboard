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
from router import classify
from sql_lookup import lookup

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
    body    = request.get_json(force=True)
    query   = body.get("query", "").strip()
    history = body.get("history", [])
    if not query:
        return jsonify({"error": "query is required"}), 400

    conn = get_conn()
    try:
        route_info = classify(query, history)
        route      = route_info.get("route", "needs_trend_context")
        cities     = route_info.get("cities", [])
        pollutants = route_info.get("pollutants", [])
        stations   = route_info.get("stations", [])
        sources    = route_info.get("sources", [])
        date_from  = route_info.get("date_from")
        date_to    = route_info.get("date_to")
        intent     = route_info.get("intent", "data_lookup")

        # Deterministic safeguards for common source wording. The LLM router
        # remains flexible, but an explicit source request must never be lost.
        query_lower = query.lower()
        source_aliases = {
            "cpcb": "CPCB",
            "openaq live": "OpenAQ",
            "openaq archive": "OpenAQ_AWS_Archive",
            "historical openaq": "OpenAQ_AWS_Archive",
        }
        for phrase, source_name in source_aliases.items():
            if phrase in query_lower and source_name not in sources:
                sources.append(source_name)

        if ("worst city" in query_lower or "most polluted" in query_lower) and intent == "trend":
            intent = "city_ranking"
            route = "needs_exact_lookup"

        # "Worst city to live in" is an air-quality ranking question without
        # an explicit pollutant. PM2.5 is the documented default metric.
        if intent == "city_ranking" and not pollutants:
            pollutants = ["PM2.5"]

        retrieved  = []
        sql_result = {"context": "", "rows_used": 0, "filters": {}}

        # Weekly embeddings combine sources. A source-constrained question
        # must use exact SQL only, otherwise the answer could contain data from
        # a different source despite the user's explicit filter.
        if sources:
            route = "needs_exact_lookup"

        if route in ("needs_trend_context", "both"):
            # if router identified specific cities, only retrieve those — avoids fetching all 18
            k = len(cities) if cities else 18
            retrieved = retrieve(query, conn, k=k, cities=cities if cities else None)

        if route in ("needs_exact_lookup", "both"):
            sql_result = lookup(
                conn, cities, stations, pollutants, sources,
                date_from, date_to, intent
            )

        sql_ctx = sql_result["context"]
        answer, tokens_used = generate(
            query, retrieved, history, sql_ctx,
            sql_result["rows_used"], sql_result["filters"]
        )
        sources = [
            {
                "city":       r["city"],
                "week_start": str(r["week_start"]),
                "week_end":   str(r["week_end"]),
                "summary":    r["summary"],
            }
            for r in retrieved
        ]
        # parse sql_ctx lines into structured rows for the UI
        sql_rows = []
        if sql_ctx:
            for line in sql_ctx.splitlines():
                if line.startswith("  "):
                    sql_rows.append(line.strip())
    finally:
        conn.close()

    return jsonify({
        "answer": answer,
        "sources": sources,
        "sql_rows": sql_rows,
        "route": route,
        "tokens_used": tokens_used,
        "rows_used": sql_result["rows_used"],
        "filters_applied": sql_result["filters"],
    })


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

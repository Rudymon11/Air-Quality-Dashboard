"""
sql_lookup.py

Runs a direct parameterised SQL query against stg_aqi_readings_mat
for exact data lookups (specific city, pollutant, date range).
Returns a plain-text context block for the generation step.
"""

import psycopg2
import psycopg2.extras


def _city_ranking(conn, pollutants: list, date_from: str, date_to: str) -> str:
    if not pollutants:
        return ""
    filters = ["pollutant = %s", "pollutant_value IS NOT NULL"]
    params = [pollutants[0]]
    if date_from:
        filters.append("reading_time_utc >= %s")
        params.append(date_from)
    if date_to:
        filters.append("reading_time_utc <= %s")
        params.append(date_to + " 23:59:59")
    sql = f"""
        SELECT city,
               ROUND(AVG(pollutant_value)::numeric, 2) AS avg_value,
               unit,
               COUNT(*) AS reading_count
        FROM stg_aqi_readings_mat
        WHERE {" AND ".join(filters)}
        GROUP BY city, unit
        HAVING COUNT(*) > 10
        ORDER BY avg_value DESC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return ""
    lines = [f"City ranking by average {pollutants[0]} (all cities, NULLs excluded):"]
    for i, r in enumerate(rows, 1):
        lines.append(f"  {i}. {r['city']}: avg={r['avg_value']} {r['unit']} ({r['reading_count']} readings)")
    return "\n".join(lines)


def _station_count(conn, cities: list) -> str:
    filters, params = [], []
    if cities:
        placeholders = ",".join(["%s"] * len(cities))
        filters.append(f"city IN ({placeholders})")
        params.extend(cities)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
        SELECT city, COUNT(DISTINCT station) AS station_count
        FROM stg_aqi_readings_mat
        {where}
        GROUP BY city
        ORDER BY city
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return ""
    lines = ["Station counts from sensor data:"]
    for r in rows:
        lines.append(f"  {r['city']}: {r['station_count']} station(s)")
    return "\n".join(lines)


def lookup(conn, cities: list, pollutants: list, date_from: str, date_to: str, intent: str = "data_lookup") -> str:
    """
    Queries stg_aqi_readings_mat with the given filters.
    Returns a formatted text block to pass as context to generate().
    Returns empty string if no rows found.
    """
    if intent == "station_count":
        return _station_count(conn, cities)
    if intent == "city_ranking":
        return _city_ranking(conn, pollutants, date_from, date_to)

    filters, params = [], []

    if cities:
        placeholders = ",".join(["%s"] * len(cities))
        filters.append(f"city IN ({placeholders})")
        params.extend(cities)

    if pollutants:
        placeholders = ",".join(["%s"] * len(pollutants))
        filters.append(f"pollutant IN ({placeholders})")
        params.extend(pollutants)

    if date_from:
        filters.append("reading_time_utc >= %s")
        params.append(date_from)

    if date_to:
        filters.append("reading_time_utc <= %s")
        params.append(date_to + " 23:59:59")

    if not filters:
        return ""

    where = "WHERE " + " AND ".join(filters)

    sql = f"""
        SELECT city, pollutant,
               ROUND(AVG(pollutant_value)::numeric, 2) AS avg_value,
               ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY pollutant_value)::numeric, 2) AS p90_value,
               unit,
               DATE_TRUNC('day', reading_time_utc)::date AS day,
               source
        FROM stg_aqi_readings_mat
        {where}
        GROUP BY city, pollutant, unit, DATE_TRUNC('day', reading_time_utc)::date, source
        ORDER BY day DESC, city, pollutant
        LIMIT 30
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        return ""

    lines = ["Exact daily aggregates from sensor readings:"]
    for r in rows:
        lines.append(
            f"  {r['city']} | {r['day']} | {r['pollutant']} | "
            f"avg={r['avg_value']} {r['unit']}, P90={r['p90_value']} {r['unit']} | source={r['source']}"
        )

    return "\n".join(lines)

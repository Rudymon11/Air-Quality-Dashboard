"""Exact, filterable SQL lookups for the RAG answer path."""

import psycopg2.extras

VALID_SOURCES = {"CPCB", "OpenAQ", "OpenAQ_AWS_Archive"}


def _result(context="", rows_used=0, filters=None):
    return {"context": context, "rows_used": int(rows_used or 0), "filters": filters or {}}


def _build_filters(cities, stations, pollutants, sources, date_from, date_to):
    clauses, params = [], []
    for column, values in (("city", cities), ("station", stations),
                           ("pollutant", pollutants), ("source", sources)):
        if values:
            if column == "source":
                invalid = set(values) - VALID_SOURCES
                if invalid:
                    raise ValueError(f"Unsupported source filter: {sorted(invalid)}")
            clauses.append(f"{column} IN (" + ",".join(["%s"] * len(values)) + ")")
            params.extend(values)
    if date_from:
        clauses.append("reading_time_utc >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("reading_time_utc <= %s")
        params.append(date_to + " 23:59:59")
    return clauses, params


def _city_ranking(conn, cities, stations, pollutants, sources, date_from, date_to):
    if not pollutants:
        return _result()
    clauses, params = _build_filters(cities, stations, pollutants[:1], sources, date_from, date_to)
    clauses.append("pollutant_value IS NOT NULL")
    query = f"""
        SELECT city, ROUND(AVG(pollutant_value)::numeric, 2) AS avg_value,
               unit, COUNT(*) AS reading_count
        FROM stg_aqi_readings_mat
        WHERE {' AND '.join(clauses)}
        GROUP BY city, unit
        HAVING COUNT(*) > 10
        ORDER BY avg_value DESC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    source_label = ", ".join(sources) if sources else "all sources"
    lines = [f"City ranking by average {pollutants[0]} ({source_label}, NULLs excluded):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"  {i}. {row['city']}: avg={row['avg_value']} {row['unit']} ({row['reading_count']} readings)")
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows),
                   {"cities": cities, "stations": stations, "pollutants": pollutants,
                    "sources": sources, "date_from": date_from, "date_to": date_to})


def _station_count(conn, cities, stations, sources, date_from, date_to):
    clauses, params = _build_filters(cities, stations, [], sources, date_from, date_to)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"""
        SELECT city, COUNT(DISTINCT station) AS station_count, COUNT(*) AS reading_count
        FROM stg_aqi_readings_mat {where}
        GROUP BY city ORDER BY city
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    lines = ["Station counts from sensor data:"]
    for row in rows:
        lines.append(f"  {row['city']}: {row['station_count']} station(s)")
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows),
                   {"cities": cities, "stations": stations, "sources": sources,
                    "date_from": date_from, "date_to": date_to})


def lookup(conn, cities, stations, pollutants, sources, date_from, date_to, intent="data_lookup"):
    """Run an exact lookup and return context plus audit metadata."""
    if intent == "station_count":
        return _station_count(conn, cities, stations, sources, date_from, date_to)
    if intent == "city_ranking":
        return _city_ranking(conn, cities, stations, pollutants, sources, date_from, date_to)

    clauses, params = _build_filters(cities, stations, pollutants, sources, date_from, date_to)
    if not clauses:
        return _result()
    clauses.append("pollutant_value IS NOT NULL")
    query = f"""
        SELECT city, pollutant, ROUND(AVG(pollutant_value)::numeric, 2) AS avg_value,
               ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY pollutant_value)::numeric, 2) AS p90_value,
               unit, DATE_TRUNC('day', reading_time_utc)::date AS day, source,
               COUNT(*) AS reading_count, SUM(COUNT(*)) OVER () AS total_reading_count
        FROM stg_aqi_readings_mat
        WHERE {' AND '.join(clauses)}
        GROUP BY city, pollutant, unit, DATE_TRUNC('day', reading_time_utc)::date, source
        ORDER BY day DESC, city, pollutant LIMIT 30
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    lines = ["Exact daily aggregates from sensor readings:"]
    for row in rows:
        lines.append(f"  {row['city']} | {row['day']} | {row['pollutant']} | avg={row['avg_value']} {row['unit']}, P90={row['p90_value']} {row['unit']} | source={row['source']} ({row['reading_count']} readings in this aggregate)")
    return _result("\n".join(lines), rows[0]["total_reading_count"],
                   {"cities": cities, "stations": stations, "pollutants": pollutants,
                    "sources": sources, "date_from": date_from, "date_to": date_to})

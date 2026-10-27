"""Compile a validated structured query plan into exact SQL."""

import psycopg2.extras

VALID_SOURCES = {"CPCB", "OpenAQ", "OpenAQ_AWS_Archive"}
AGGREGATIONS = {
    "average": "AVG(pollutant_value)",
    "maximum": "MAX(pollutant_value)",
    "p90": "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY pollutant_value)",
}


def _result(context="", rows_used=0, filters=None):
    return {"context": context, "rows_used": int(rows_used or 0), "filters": filters or {}}


def _build_filters(plan):
    clauses, params = [], []
    for column, key in (("city", "cities"), ("station", "stations"),
                        ("pollutant", "pollutants"), ("source", "sources")):
        values = plan.get(key) or []
        if values:
            if column == "source" and set(values) - VALID_SOURCES:
                raise ValueError(f"Unsupported source filter: {sorted(set(values) - VALID_SOURCES)}")
            clauses.append(f"{column} IN (" + ",".join(["%s"] * len(values)) + ")")
            params.extend(values)
    if plan.get("date_from"):
        clauses.append("reading_time_utc >= %s")
        params.append(plan["date_from"])
    if plan.get("date_to"):
        clauses.append("reading_time_utc <= %s")
        params.append(plan["date_to"] + " 23:59:59")
    return clauses, params


def _filters_for_response(plan):
    return {key: plan.get(key) for key in (
        "scope", "cities", "stations", "pollutants", "sources", "date_from", "date_to",
        "aggregation", "direction", "domain", "metric",
    ) if plan.get(key) not in (None, [], "")}


def _city_ranking(conn, plan):
    pollutants = plan.get("pollutants") or [plan.get("metric") or "PM2.5"]
    expression = AGGREGATIONS.get(plan.get("aggregation"), AGGREGATIONS["average"])
    direction = "ASC" if plan.get("direction") == "lowest" else "DESC"
    clauses, params = _build_filters({**plan, "pollutants": pollutants[:1]})
    clauses.append("pollutant_value IS NOT NULL")
    query = f"""
        SELECT city, ROUND(({expression})::numeric, 2) AS aggregate_value,
               unit, COUNT(*) AS reading_count
        FROM stg_aqi_readings_mat
        WHERE {' AND '.join(clauses)}
        GROUP BY city, unit
        HAVING COUNT(*) > 10
        ORDER BY aggregate_value {direction}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    source_label = ", ".join(plan.get("sources") or []) or "all sources"
    aggregation = plan.get("aggregation", "average")
    lines = [f"City ranking by {aggregation} {pollutants[0]} ({source_label}, NULLs excluded):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"  {i}. {row['city']}: {row['aggregate_value']} {row['unit']} ({row['reading_count']} readings)")
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows), _filters_for_response(plan))


SCREENING_CONFIG = {
    "corrosivity": {
        "table": "fct_station_daily_corrosivity",
        "value": "corrosivity_screening_score",
        "label": "corrosivity screening score",
        "category": "screening_category",
        "count_columns": ["so2_readings", "humidity_readings"],
    },
    "deliquescence": {
        "table": "fct_station_daily_deliquescence",
        "value": "sapr",
        "label": "SAPR deliquescence screening index",
        "category": "screening_category",
        "count_columns": ["pm25_readings", "nh3_readings", "so2_readings", "no2_readings", "humidity_readings"],
    },
    "electronics": {
        "table": "fct_station_daily_electronics_risk",
        "value": "electronics_screening_score",
        "label": "electronics-environment screening score",
        "category": "screening_category",
        "count_columns": ["so2_readings", "no2_readings", "o3_readings", "humidity_readings"],
    },
}


def _screening_filters(plan):
    clauses, params = [], []
    if plan.get("cities"):
        clauses.append("city IN (" + ",".join(["%s"] * len(plan["cities"])) + ")")
        params.extend(plan["cities"])
    if plan.get("stations"):
        clauses.append("station IN (" + ",".join(["%s"] * len(plan["stations"])) + ")")
        params.extend(plan["stations"])
    if plan.get("date_from"):
        clauses.append("reading_date >= %s"); params.append(plan["date_from"])
    if plan.get("date_to"):
        clauses.append("reading_date <= %s"); params.append(plan["date_to"])
    return clauses, params


def _screening_ranking(conn, plan):
    config = SCREENING_CONFIG[plan["domain"]]
    clauses, params = _screening_filters(plan)
    clauses.append(f"{config['value']} IS NOT NULL")
    where = " AND ".join(clauses)
    direction = "ASC" if plan.get("direction") == "lowest" else "DESC"
    group_column = "station" if plan.get("intent") == "station_ranking" else "city"
    query = f"""
        SELECT city, {group_column}, ROUND(AVG({config['value']})::numeric, 4) AS aggregate_value,
               COUNT(*) AS screening_rows, MIN(reading_date) AS first_date,
               MAX(reading_date) AS last_date
        FROM {config['table']}
        WHERE {where}
        GROUP BY city, {group_column}
        HAVING COUNT(*) > 0
        ORDER BY aggregate_value {direction} NULLS LAST
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result("No screening rows matched the requested filters.", 0, _filters_for_response(plan))
    scope_label = "station" if group_column == "station" else "city"
    lines = [f"{scope_label.title()} ranking by {config['label']} (screening proxy; daily rows):"]
    for i, row in enumerate(rows, 1):
        name = row[group_column]
        lines.append(f"  {i}. {name} ({row['city']}): {row['aggregate_value']} ({row['screening_rows']} station-days, {row['first_date']} to {row['last_date']})")
    return _result("\n".join(lines), sum(row["screening_rows"] for row in rows), _filters_for_response(plan))


def _screening_lookup(conn, plan):
    config = SCREENING_CONFIG[plan["domain"]]
    clauses, params = _screening_filters(plan)
    clauses.append(f"{config['value']} IS NOT NULL")
    where = " AND ".join(clauses)
    query = f"""
        SELECT city, station, reading_date, {config['value']} AS metric_value,
               {config['category']} AS screening_category, sources
        FROM {config['table']}
        WHERE {where}
        ORDER BY reading_date DESC, metric_value DESC
        LIMIT 100
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result("No screening rows matched the requested filters.", 0, _filters_for_response(plan))
    lines = [f"Exact {config['label']} rows (screening proxy):"]
    for row in rows:
        lines.append(f"  {row['city']} | {row['station']} | {row['reading_date']} | value={row['metric_value']} | category={row['screening_category']} | sources={row['sources']}")
    return _result("\n".join(lines), len(rows), _filters_for_response(plan))


def _screening_station_list(conn, plan):
    config = SCREENING_CONFIG[plan["domain"]]
    clauses, params = _screening_filters(plan)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"""
        SELECT city, station, COUNT(*) AS screening_rows,
               MIN(reading_date) AS first_date, MAX(reading_date) AS last_date
        FROM {config['table']} {where}
        GROUP BY city, station ORDER BY city, station
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    lines = [f"Stations present in {plan['domain']} screening data:"]
    for row in rows:
        lines.append(f"  {row['city']} | {row['station']} | {row['screening_rows']} station-days ({row['first_date']} to {row['last_date']})")
    return _result("\n".join(lines), sum(row["screening_rows"] for row in rows), _filters_for_response(plan))


def _screening_station_count(conn, plan):
    config = SCREENING_CONFIG[plan["domain"]]
    clauses, params = _screening_filters(plan)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"SELECT city, COUNT(DISTINCT station) AS station_count, COUNT(*) AS screening_rows FROM {config['table']} {where} GROUP BY city ORDER BY city"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    lines = [f"Station counts in {plan['domain']} screening data:"]
    for row in rows:
        lines.append(f"  {row['city']}: {row['station_count']} station(s)")
    return _result("\n".join(lines), sum(row["screening_rows"] for row in rows), _filters_for_response(plan))


def _station_count(conn, plan):
    clauses, params = _build_filters(plan)
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
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows), _filters_for_response(plan))


def _station_list(conn, plan):
    clauses, params = _build_filters(plan)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"""
        SELECT city, station, COUNT(*) AS reading_count,
               MIN(reading_time_utc)::date AS first_reading,
               MAX(reading_time_utc)::date AS last_reading
        FROM stg_aqi_readings_mat
        {where}
        GROUP BY city, station
        ORDER BY city, station
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    lines = ["Stations found in the filtered sensor data:"]
    for row in rows:
        lines.append(
            f"  {row['city']} | {row['station']} | {row['reading_count']} readings "
            f"({row['first_reading']} to {row['last_reading']})"
        )
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows), _filters_for_response(plan))


def _station_ranking(conn, plan):
    cities = plan.get("cities") or []
    if not cities:
        return _result("A city is required for station ranking.")
    pollutants = plan.get("pollutants") or [plan.get("metric") or "PM2.5"]
    expression = AGGREGATIONS.get(plan.get("aggregation"), AGGREGATIONS["average"])
    direction = "ASC" if plan.get("direction") == "lowest" else "DESC"
    clauses, params = _build_filters({**plan, "pollutants": pollutants[:1]})
    clauses.append("pollutant_value IS NOT NULL")
    query = f"""
        SELECT city, station, ROUND(({expression})::numeric, 2) AS aggregate_value,
               unit, COUNT(*) AS reading_count
        FROM stg_aqi_readings_mat
        WHERE {' AND '.join(clauses)}
        GROUP BY city, station, unit
        HAVING COUNT(*) > 10
        ORDER BY aggregate_value {direction}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return _result()
    aggregation = plan.get("aggregation", "average")
    source_label = ", ".join(plan.get("sources") or []) or "all sources"
    lines = [f"Station ranking in {', '.join(cities)} by {aggregation} {pollutants[0]} ({source_label}; stations with >10 readings):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"  {i}. {row['station']} ({row['city']}): {row['aggregate_value']} {row['unit']} ({row['reading_count']} readings)")
    return _result("\n".join(lines), sum(row["reading_count"] for row in rows), _filters_for_response(plan))


def _data_lookup(conn, plan):
    clauses, params = _build_filters(plan)
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
    return _result("\n".join(lines), rows[0]["total_reading_count"], _filters_for_response(plan))


def lookup(conn, plan):
    """Execute the validated query plan and return context plus audit metadata."""
    intent = plan.get("intent", "data_lookup")
    if plan.get("domain") in SCREENING_CONFIG:
        if intent == "station_list":
            return _screening_station_list(conn, plan)
        if intent == "station_count":
            return _screening_station_count(conn, plan)
        if intent in {"city_ranking", "station_ranking"}:
            return _screening_ranking(conn, plan)
        return _screening_lookup(conn, plan)
    if intent == "station_count":
        return _station_count(conn, plan)
    if intent == "station_list":
        return _station_list(conn, plan)
    if intent == "station_ranking":
        return _station_ranking(conn, plan)
    if intent == "city_ranking":
        return _city_ranking(conn, plan)
    return _data_lookup(conn, plan)

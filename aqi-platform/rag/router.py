"""Structured query planning for the AQI Q&A system."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

_client = None
VALID_SOURCES = ["CPCB", "OpenAQ", "OpenAQ_AWS_Archive"]
VALID_AGGREGATIONS = ["average", "maximum", "p90", "count"]
VALID_DIRECTIONS = ["highest", "lowest"]
VALID_SCOPES = ["all_cities", "selected_cities", "selected_stations"]
VALID_DOMAINS = ["air_quality", "corrosivity", "deliquescence", "electronics"]

ROUTER_PROMPT = """You are the structured query planner for an Indian air-quality database.
Return ONLY valid JSON. Build a complete plan for the current question, using
the prior plan as conversational state when the question is a follow-up.

The database supports cities, stations, pollutants/weather metrics, sources,
UTC date ranges, city rankings, station rankings, station lists, station
counts, trend context, explanations, and two environmental screening domains:
corrosivity and deliquescence. Valid sources are CPCB, OpenAQ, and
OpenAQ_AWS_Archive. Valid pollutants include PM2.5, PM10, SO2, NO2, NO, CO,
NH3, O3, NOX, PM1, UM003, TEMP, HUMIDITY, WIND_SPEED, and WIND_DIR.

Rules:
- A source request must be represented in sources and requires exact SQL.
- Ranking questions set intent=city_ranking, aggregation=average by default,
  and direction=highest or lowest.
- A question about the most/least polluted place or station within a named
  city sets intent=station_ranking, uses that city as the required scope, and
  defaults to PM2.5 when no pollutant is named.
- A request to list, show, or name stations/monitoring locations sets
  intent=station_list. A request for their number sets intent=station_count.
- "worst city to live in" defaults to metric PM2.5, aggregation average,
  direction highest, and scope all_cities unless the user specifies otherwise.
- "overall", "all cities", or "entire database" resets city scope to all_cities
  unless a city is explicitly named in the current question.
- Follow-ups inherit prior filters unless the current question changes or
  explicitly resets them.
- Explanations may accompany exact SQL, but causes are hypotheses unless the
  database contains supporting evidence.
- Corrosivity questions use domain=corrosivity and metric=CORROSIVITY_SCORE;
  deliquescence or under-deposit questions use domain=deliquescence and
  metric=SAPR; electronics, data-center, substation or ISA-71.04 questions use
  domain=electronics and metric=ELECTRONICS_SCORE. These are screening proxies,
  not official classifications or proven asset-failure predictions.
- For corrosion-domain questions, use the screening marts and keep the
  station/city/date filters. Do not put CORROSIVITY_SCORE or SAPR in pollutants.

JSON schema:
{
  "route": "needs_exact_lookup" | "needs_trend_context" | "both",
  "intent": "station_count" | "station_list" | "station_ranking" | "city_ranking" | "data_lookup" | "trend",
  "metric": "PM2.5",
  "domain": "air_quality" | "corrosivity" | "deliquescence",
  "aggregation": "average" | "maximum" | "p90" | "count",
  "direction": "highest" | "lowest" | null,
  "scope": "all_cities" | "selected_cities" | "selected_stations",
  "cities": [],
  "stations": [],
  "pollutants": [],
  "sources": [],
  "date_from": null,
  "date_to": null,
  "needs_explanation": true
}

Today's data-window reference date is 2026-08-25. Resolve relative dates.
"""


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def _normalise(plan):
    plan = plan if isinstance(plan, dict) else {}
    result = {
        "route": plan.get("route", "needs_trend_context"),
        "intent": plan.get("intent", "trend"),
        "metric": plan.get("metric"),
        "domain": plan.get("domain", "air_quality"),
        "aggregation": plan.get("aggregation", "average"),
        "direction": plan.get("direction"),
        "scope": plan.get("scope", "all_cities"),
        "cities": plan.get("cities") or [],
        "stations": plan.get("stations") or [],
        "pollutants": plan.get("pollutants") or [],
        "sources": plan.get("sources") or [],
        "date_from": plan.get("date_from"),
        "date_to": plan.get("date_to"),
        "needs_explanation": bool(plan.get("needs_explanation", True)),
    }
    result["sources"] = [s for s in result["sources"] if s in VALID_SOURCES]
    result["domain"] = result["domain"] if result["domain"] in VALID_DOMAINS else "air_quality"
    result["aggregation"] = result["aggregation"] if result["aggregation"] in VALID_AGGREGATIONS else "average"
    result["direction"] = result["direction"] if result["direction"] in VALID_DIRECTIONS else None
    result["scope"] = result["scope"] if result["scope"] in VALID_SCOPES else "all_cities"
    if result["sources"] or result["intent"] in {"city_ranking", "station_ranking", "station_list", "station_count", "data_lookup"}:
        result["route"] = "needs_exact_lookup"
    if result["cities"] and result["scope"] == "all_cities":
        result["scope"] = "selected_cities"
    if result["stations"]:
        result["scope"] = "selected_stations"
    if result["intent"] in {"city_ranking", "station_ranking"} and not result["pollutants"] and not result["metric"]:
        result["metric"] = "PM2.5"
        result["pollutants"] = ["PM2.5"]
    elif result["metric"] and not result["pollutants"]:
        result["pollutants"] = [result["metric"]]
    if result["domain"] == "corrosivity":
        result["metric"] = "CORROSIVITY_SCORE"
        result["pollutants"] = []
        result["route"] = "needs_exact_lookup"
    elif result["domain"] == "deliquescence":
        result["metric"] = "SAPR"
        result["pollutants"] = []
        result["route"] = "needs_exact_lookup"
    elif result["domain"] == "electronics":
        result["metric"] = "ELECTRONICS_SCORE"
        result["pollutants"] = []
        result["route"] = "needs_exact_lookup"
    return result


def _domain_from_query(query: str) -> str | None:
    """Reliable fallback for screening vocabulary the LLM must not miss."""
    text = (query or "").lower()
    if any(term in text for term in (
        "electronics", "electronic corrosion", "data center", "data centre",
        "substation", "isa-71", "isa 71", "control cabinet", "control room",
    )):
        return "electronics"
    if any(term in text for term in (
        "deliquescence", "under-deposit", "under deposit", "sapr", "wetting active",
    )):
        return "deliquescence"
    if any(term in text for term in (
        "corrosivity", "corrosive", "corrosion screening", "atmospheric corrosion",
    )):
        return "corrosivity"
    return None


def classify(query: str, history: list | None = None, prior_plan: dict | None = None) -> dict:
    """Return a validated, complete plan rather than a route-only guess."""
    try:
        state = json.dumps(prior_plan or {}, ensure_ascii=False)
        messages = [{"role": "system", "content": ROUTER_PROMPT}]
        messages.append({"role": "system", "content": f"Prior structured plan: {state}"})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": query})
        response = _get_client().chat.completions.create(
            model="openai/gpt-oss-120b", messages=messages, temperature=0.0
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        plan = _normalise(json.loads(raw))
        inferred_domain = _domain_from_query(query)
        if inferred_domain and plan["domain"] == "air_quality":
            plan["domain"] = inferred_domain
            plan["metric"] = {
                "corrosivity": "CORROSIVITY_SCORE",
                "deliquescence": "SAPR",
                "electronics": "ELECTRONICS_SCORE",
            }[inferred_domain]
            plan["pollutants"] = []
            plan["route"] = "needs_exact_lookup"
        return plan
    except Exception:
        return _normalise({})

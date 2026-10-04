"""
router.py

Classifies a user query into one of three routing types and extracts
structured parameters (city, pollutant, date range) from it.

Route types:
    needs_exact_lookup   — specific numbers for a city/date (→ SQL)
    needs_trend_context  — trends, causes, comparisons (→ vector retrieval)
    both                 — needs exact data AND trend context
"""

import os
import json
from groq import Groq
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

_client = None

ROUTER_PROMPT = """You are a query classifier for an air quality data system.
Classify the user query into exactly one of these route types:
- needs_exact_lookup: asks for specific numbers, a specific date/period, or a specific city's readings
- needs_trend_context: asks about trends, causes, comparisons, explanations, or general patterns
- both: needs both specific numbers AND trend/context explanation

Also extract any mentioned city names, station names, pollutant names, source
filters, and date references.

Valid pollutants: PM2.5, PM10, SO2, NO2, NO, CO, NH3, O3, WIND_SPEED, TEMP, HUMIDITY
For city_ranking queries, always populate "pollutants" with the relevant metric. Map natural language to the correct pollutant name: "windiest" → WIND_SPEED, "hottest" or "warmest" → TEMP, "most humid" → HUMIDITY, "most polluted" → PM2.5.
Valid cities: Ahmedabad, Bengaluru, Bhopal, Chennai, Dehradun, Delhi, Guwahati, Hyderabad, Indore, Kolkata, Lucknow, Ludhiana, Mumbai, Nagpur, Patna, Shillong, Srinagar, Visakhapatnam
Valid sources: CPCB, OpenAQ, OpenAQ_AWS_Archive
Map source wording exactly: "CPCB data"/"government data" -> CPCB;
"OpenAQ live" -> OpenAQ; "archive"/"historical OpenAQ" -> OpenAQ_AWS_Archive.
If the user says "only", "just", or "use ... data", populate sources with
only the requested source. Never leave a requested source filter implicit.

Respond with ONLY valid JSON in this exact format:
{
  "route": "needs_exact_lookup" | "needs_trend_context" | "both",
  "intent": "station_count" | "city_ranking" | "data_lookup" | "trend",
  "cities": ["City1", "City2"],
  "stations": ["station name"],
  "pollutants": ["PM2.5"],
  "sources": ["CPCB"],
  "date_from": "YYYY-MM-DD or null",
  "date_to": "YYYY-MM-DD or null"
}

Set intent to "station_count" when the query asks about number of stations, sensors, monitors, or locations.
Set intent to "city_ranking" when the query asks which city is highest/lowest/worst/best/windiest/hottest/most polluted for a specific pollutant or weather metric — i.e. a cross-city comparison requiring a ranked list.
Set intent to "data_lookup" for specific readings/values for known cities.
Set intent to "trend" for patterns, causes, comparisons, explanations.
Set route to "needs_exact_lookup" whenever a source filter is requested,
because weekly vector summaries combine multiple sources.

Today's date is 2026-08-25. Resolve relative dates like "last week", "last month", "yesterday" accordingly.
"""


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def classify(query: str, history: list = None) -> dict:
    """
    Returns a dict with keys: route, intent, cities, stations, pollutants,
    sources, date_from, date_to.
    Falls back to needs_trend_context on any parse failure.
    """
    try:
        messages = [{"role": "system", "content": ROUTER_PROMPT}]
        if history:
            messages.extend(history)  # full session history
        messages.append({"role": "user", "content": query})
        response = _get_client().chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        # strip markdown code fences if model wraps in ```json
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return {
            "route": "needs_trend_context",
            "cities": [],
            "stations": [],
            "pollutants": [],
            "sources": [],
            "date_from": None,
            "date_to": None,
        }

"""
ingest.py

Pulls real-time air quality readings for a set of Indian cities from two
sources and normalizes them into a single schema:

  1. CPCB (via data.gov.in)  -- primary source
  2. OpenAQ v3                -- secondary/cross-check source

Both fetch functions return a pandas DataFrame in the same shape:
  city, station, pollutant, value, unit, reading_time_utc, source, ingested_at

Run directly to fetch from both sources and write to CSV (or Postgres,
if DATABASE_URL is set):

  python ingest.py               # live fetch (CPCB + OpenAQ), what the hourly DAG calls
  python ingest.py --backfill    # one-time historical backfill from OpenAQ's AWS archive

---------------------------------------------------------------------------
CHANGELOG (fixes applied on top of the previous version)
---------------------------------------------------------------------------
FIX #1 - _save(): CSV mode="a" was writing a header row on every call,
         which corrupts the file across repeated hourly runs. Now only
         writes the header once, the first time the file is created.

FIX #2 - CPCB fetch had no retry/backoff and no delay between per-city
         calls. CPCB doesn't publish a documented rate limit, so this is
         a precaution, not a confirmed requirement. Retry/backoff logic
         now mirrors the pattern already used for OpenAQ.

FIX #3 - _discover_openaq_locations() was being called fresh on every
         single run (live fetch, backfill, resumed backfill...), spending
         a rate-limited API call each time on something that barely
         changes. Result is now cached locally for up to 7 days.

FIX #4 - backfill_openaq_from_aws() held everything in memory and had no
         way to resume after an interruption -- a crash partway through a
         90-day x N-location backfill meant starting over from scratch.
         It now tracks completed (location_id, date) pairs in a local
         progress file and writes results to CSV incrementally, so a
         re-run picks up where it left off instead of redoing everything.
         NOTE: because of this, the function no longer returns a
         DataFrame -- it streams straight to CSV. The __main__ block
         below has been updated to match.

FIX #5 - backfill_openaq_from_aws() was also just slow: fully serial
         requests, a fresh TCP+TLS connection per file, and an artificial
         sleep copied over from the rate-limited REST API code even
         though this endpoint (a static public file store) has no
         documented rate limit. For thousands of files against a
         us-east-1 bucket, that added up to well over an hour of mostly
         avoidable waiting. Now uses a thread pool (concurrent, since
         this is I/O-bound and each request is independent) plus a
         shared requests.Session (connection reuse) and no artificial
         delay. Progress checkpointing is also batched instead of
         rewritten after every single item, so it doesn't get slower
         as the run progresses.

FIX #6 - CPCB's pollutant value field names changed at the API level.
         Older third-party docs (and the original version of this code)
         assumed 'pollutant_avg'/'pollutant_min'/'pollutant_max', but a
         live response confirmed on 2026-07-17 shows the real keys are
         'avg_value'/'min_value'/'max_value'. This was a silent bug:
         every request succeeded, but every row's value read as None and
         got dropped, so CPCB always contributed zero valid rows with no
         visible error -- it looked like "nothing is working" even
         though the API itself was responding fine. _get_pollutant_value()
         now checks both key names, so a future reversion on their end
         doesn't silently reintroduce the same failure mode.
---------------------------------------------------------------------------
"""

import os
import time
import json  # FIX #3, #4: used for the location cache and backfill progress file
import requests
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

CITIES = ["Delhi", "Ludhiana", "Lucknow", "Srinagar", "Dehradun",
          "Mumbai", "Ahmedabad", "Panaji", "Kochi", "Visakhapatnam",
          "Chennai", "Bengaluru", "Hyderabad", "Patna", "Kolkata",
          "Guwahati", "Shillong", "Bhopal", "Indore", "Nagpur"]


# ---------------------------------------------------------------------------
# CPCB / data.gov.in
# ---------------------------------------------------------------------------

CPCB_RESOURCE_ID = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
CPCB_BASE_URL = f"https://api.data.gov.in/resource/{CPCB_RESOURCE_ID}"


def _fetch_cpcb_city(city, limit=500, max_retries=3):
    """
    Fetch all current station readings for a single city.
    filters[city] only accepts one exact value per request, so this
    must be called once per city rather than passing a city list.

    FIX #2: added retry/backoff. CPCB doesn't publish a documented rate
    limit, so this uses simple linear backoff (5s x attempt number)
    rather than trying to read a reset header that may not exist.
    """

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    params = {
        "api-key": os.getenv("CPCB_API_KEY"),
        "format": "json",
        "limit": limit,
        "filters[city]": city,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.get(CPCB_BASE_URL, params=params, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  CPCB rate limited on {city}. Waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("records", [])
        except requests.exceptions.RequestException as e:
            wait = 5 * (attempt + 1)
            print(f"  CPCB request failed for {city} (attempt {attempt + 1}/{max_retries}): {e}. "
                  f"Waiting {wait}s...")
            time.sleep(wait)

    print(f"  CPCB fetch permanently failed for {city} after {max_retries} attempts.")
    return []


def _safe_float(value):
    """pollutant_min/max/avg come back as strings, and are literally 'NA'
    when a station has no reading for that pollutant."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_pollutant_value(record):
    """
    FIX #6: CPCB's field names for the average pollutant reading changed
    at some point -- older third-party docs (and the first version of
    this code) assumed the key was 'pollutant_avg', but the live API
    actually returns it as 'avg_value'. Confirmed directly against a
    real response on 2026-07-17. This silently broke the pipeline: every
    request succeeded, but every row's value came back None and got
    dropped, so CPCB always contributed zero rows with no visible error.

    Tries both key names so a future schema reversion (or a temporary
    rollback on their end) doesn't silently break this again the same way.
    """
    raw = record.get("avg_value")
    if raw is None:
        raw = record.get("pollutant_avg")  # fallback, in case of schema reversion
    return raw


def _parse_cpcb_datetime(raw):
    """CPCB's last_update is 'DD-MM-YYYY HH:MM:SS', not ISO format."""
    try:
        return datetime.strptime(raw, "%d-%m-%Y %H:%M:%S")
    except (TypeError, ValueError):
        return None


def fetch_cpcb_data(cities=CITIES, sleep_between_calls=0.75):
    """
    Loops over each target city (one API call per city, per CPCB's
    filter behavior) and normalizes all station-pollutant readings
    into a single DataFrame.

    FIX #2: added a small delay between cities as a precaution, since
    retry/backoff now lives inside _fetch_cpcb_city() itself.
    """
    print("Fetching CPCB data via data.gov.in...")
    rows = []

    for city in cities:
        records = _fetch_cpcb_city(city)  # retry/backoff handled inside this call now

        for r in records:
            rows.append({
                "city": r.get("city"),
                "station": r.get("station"),
                "pollutant": r.get("pollutant_id"),
                "value": _safe_float(_get_pollutant_value(r)),
                "unit": "µg/m³",  # CPCB reports in µg/m³ except CO2 (mg/m³)
                "reading_time_utc": _parse_cpcb_datetime(r.get("last_update")),
                "source": "CPCB",
                "ingested_at": datetime.now(timezone.utc),
            })

        print(f"  {city}: {len(records)} raw records")
        time.sleep(sleep_between_calls)

    df = pd.DataFrame(rows)
    # Drop rows where the pollutant reading itself is missing/"NA"
    df = df.dropna(subset=["value"])
    print(f"CPCB: collected {len(df)} valid readings across {len(cities)} cities.")
    return df


# ---------------------------------------------------------------------------
# OpenAQ v3
# ---------------------------------------------------------------------------

OPENAQ_BASE_URL = "https://api.openaq.org/v3"
OPENAQ_INDIA_COUNTRY_ID = 9  # confirmed via /v3/countries

OPENAQ_HEADERS = {"X-API-Key": os.getenv("OPENAQ_API_KEY")}

# FIX #3: local cache for city -> location ID discovery, so repeated
# runs (live fetch, backfill, resumed backfill) don't all pay for a
# fresh API call to rediscover something that barely changes.
LOCATION_CACHE_PATH = "openaq_location_cache.json"
LOCATION_CACHE_MAX_AGE_DAYS = 7


def _openaq_get(url, params=None, max_retries=3):
    for attempt in range(max_retries):
        resp = requests.get(url, headers=OPENAQ_HEADERS, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("x-ratelimit-reset", 5))
            print(f"  Rate limited. Waiting {wait}s...")
            time.sleep(wait + 1)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def _discover_openaq_locations(cities=CITIES, force_refresh=False):
    """
    /v3/locations does NOT include sensor readings, only metadata
    (id, name, sensors[id, name, parameter]). Use it purely to find
    which location IDs correspond to our target cities.

    FIX #3: result is cached locally for up to LOCATION_CACHE_MAX_AGE_DAYS,
    keyed by the exact city list used, so it auto-refreshes if CITIES
    ever changes. Pass force_refresh=True to bypass the cache manually.
    """
    if not force_refresh and os.path.exists(LOCATION_CACHE_PATH):
        age_seconds = time.time() - os.path.getmtime(LOCATION_CACHE_PATH)
        if age_seconds < LOCATION_CACHE_MAX_AGE_DAYS * 86400:
            with open(LOCATION_CACHE_PATH, "r") as f:
                cached = json.load(f)
            if cached.get("cities") == sorted(cities):
                print(f"  Using cached OpenAQ locations ({len(cached['locations'])} matched, "
                      f"{age_seconds / 3600:.1f}h old).")
                return cached["locations"]

    print("  Fetching fresh OpenAQ location list (cache missing, stale, or city list changed)...")
    data = _openaq_get(f"{OPENAQ_BASE_URL}/locations", params={
        "countries_id": OPENAQ_INDIA_COUNTRY_ID,
        "limit": 1000,
    })
    matched = []
    for loc in data.get("results", []):
        name = loc.get("name") or ""
        locality = loc.get("locality") or ""
        combined = f"{name} {locality}".lower()
        if any(city.lower() in combined for city in cities):
            matched.append({"location_id": loc["id"], "location_name": name})

    with open(LOCATION_CACHE_PATH, "w") as f:
        json.dump({"cities": sorted(cities), "locations": matched, "cached_at": time.time()}, f)

    return matched


def _fetch_openaq_location_readings(location_id, location_name):
    """
    /v3/locations/{id}/sensors DOES include a nested 'latest' object
    per sensor -- this is the endpoint that actually has readings.
    """
    data = _openaq_get(f"{OPENAQ_BASE_URL}/locations/{location_id}/sensors")
    rows = []
    for s in data.get("results", []):
        latest = s.get("latest")
        if not latest:
            continue
        parameter = s.get("parameter", {})
        rows.append({
            "city": location_name,
            "station": location_name,
            "pollutant": parameter.get("name"),
            "value": latest.get("value"),
            "unit": parameter.get("units"),
            "reading_time_utc": latest.get("datetime", {}).get("utc"),
            "source": "OpenAQ",
            "ingested_at": datetime.now(timezone.utc),
        })
    return rows


def fetch_openaq_data(cities=CITIES, sleep_between_calls=1.1):
    """
    LIVE ingestion path -- use this from the hourly Airflow DAG.
    Costs API calls against your OpenAQ key and is rate-limited.
    """
    print("Fetching OpenAQ data via v3 API (live)...")
    locations = _discover_openaq_locations(cities)
    print(f"  Matched {len(locations)} OpenAQ locations across target cities.")

    all_rows = []
    for i, loc in enumerate(locations, start=1):
        try:
            rows = _fetch_openaq_location_readings(loc["location_id"], loc["location_name"])
            all_rows.extend(rows)
        except Exception as e:
            print(f"  Skipped location {loc['location_id']}: {e}")
        time.sleep(sleep_between_calls)  # stay under 60 req/min default limit

    df = pd.DataFrame(all_rows)
    print(f"OpenAQ (live): collected {len(df)} readings across {len(locations)} locations.")
    return df


# ---------------------------------------------------------------------------
# OpenAQ -- Open Data on AWS (historical backfill, no API key, no rate limit)
# ---------------------------------------------------------------------------
#
# Bucket structure (verified against docs.openaq.org/aws/about):
#   https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/
#       locationid={id}/year={YYYY}/month={MM}/location-{id}-{YYYYMMDD}.csv.gz
#
# Each file holds one day of readings for one location, across all its
# sensors, in narrow format:
#   location_id, sensors_id, location, datetime, lat, lon, parameter, units, value
#
# Files lag ~72 hours behind real time -- this is NOT a live feed, it's a
# backfill source. Run this ONCE (or occasionally, for a rolling window)
# to seed real history for the forecasting model, not on the hourly DAG.
#
# FIX #5 (performance): the original version of this backfill was a fully
# serial loop -- one request at a time, no connection reuse, plus a fixed
# sleep() copied over from the rate-limited REST API code even though this
# endpoint is a static public file store with no documented rate limit.
# For a 90-day x 30-40 location backfill (thousands of individual file
# downloads, each round-tripping to us-east-1), that added up to well
# over an hour of mostly avoidable waiting. Three changes fix this:
#   1. A thread pool downloads multiple days/locations concurrently --
#      safe here because these are independent, stateless GETs against
#      static files, not a shared rate-limited resource.
#   2. A single requests.Session() is reused across all downloads instead
#      of opening a fresh TCP+TLS connection per file.
#   3. The artificial sleep is removed for archive calls specifically
#      (it stays in place for the live OpenAQ REST API calls, which ARE
#      rate-limited).
# The progress checkpoint is also now batched (saved every N completions
# instead of after every single one) so it doesn't get slower as the run
# progresses, and results are still written in the main thread only, so
# there's no risk of two threads writing to the CSV at the same time.

import gzip
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

AWS_ARCHIVE_BASE_URL = "https://openaq-data-archive.s3.amazonaws.com"

BACKFILL_PROGRESS_PATH = "openaq_backfill_progress.json"
BACKFILL_OUTPUT_PATH = "raw_aqi_readings_backfill.csv"


def _fetch_archive_file(location_id, date, session=None):
    """
    Download and parse a single day's archive file for one location.
    Returns an empty DataFrame (not an error) if the file doesn't exist --
    not every station reports every day.

    Accepts an optional shared requests.Session for connection reuse --
    falls back to a plain requests.get() if none is given.
    """
    year = date.strftime("%Y")
    month = date.strftime("%m")
    date_str = date.strftime("%Y%m%d")
    url = (f"{AWS_ARCHIVE_BASE_URL}/records/csv.gz/locationid={location_id}/"
           f"year={year}/month={month}/location-{location_id}-{date_str}.csv.gz")

    http = session if session is not None else requests
    resp = http.get(url, timeout=30)
    if resp.status_code == 404:
        return pd.DataFrame()  # no file for this location/day -- normal, not an error
    resp.raise_for_status()

    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
        return pd.read_csv(f)


def _load_backfill_progress():
    """FIX #4: returns the set of (location_id, 'YYYY-MM-DD') pairs already completed."""
    if os.path.exists(BACKFILL_PROGRESS_PATH):
        with open(BACKFILL_PROGRESS_PATH, "r") as f:
            return set(tuple(x) for x in json.load(f))
    return set()


def _save_backfill_progress(done_set):
    """FIX #4/#5: checkpointed periodically during the run (not after every
    single item) so the write cost doesn't grow with how far the run has
    progressed. Also always called once more at the end (see finally block
    below) so nothing is lost even if the run is interrupted."""
    with open(BACKFILL_PROGRESS_PATH, "w") as f:
        json.dump(list(done_set), f)


def _normalize_archive_df(df, location_name):
    """Maps one archive file's columns onto the shared project schema."""
    df = df.rename(columns={
        "location": "station",
        "parameter": "pollutant",
        "units": "unit",
        "datetime": "reading_time_utc",
    })
    df["city"] = location_name
    df["source"] = "OpenAQ_AWS_Archive"
    df["ingested_at"] = datetime.now(timezone.utc)
    keep_cols = ["city", "station", "pollutant", "value", "unit",
                 "reading_time_utc", "source", "ingested_at"]
    return df[keep_cols]


def backfill_openaq_from_aws(cities=CITIES, days=90, max_workers=12, checkpoint_every=50):
    """
    ONE-TIME (or periodic) historical backfill path -- use this to seed
    months of real history before your live pipeline has had time to
    accumulate it hourly. No API key required, no rate limit, since this
    reads static files rather than calling the rate-limited REST API.

    FIX #4: resumable -- skips (location, day) pairs already recorded as
    done in BACKFILL_PROGRESS_PATH.

    FIX #5: parallelized with a thread pool + a shared session for
    connection reuse, with no artificial delay between requests (this
    endpoint has no documented rate limit, unlike the live REST API).
    CSV writes and progress-set updates only ever happen in the main
    thread (as results come back via as_completed), so there's no need
    for extra locking around the file I/O.
    """
    from datetime import timedelta

    print(f"Backfilling {days} days of OpenAQ history from the AWS archive "
          f"(up to {max_workers} concurrent downloads)...")
    locations = _discover_openaq_locations(cities)
    print(f"  Matched {len(locations)} OpenAQ locations across target cities.")

    done = _load_backfill_progress()
    if done:
        print(f"  Resuming: {len(done)} (location, day) pairs already completed.")

    end_date = datetime.now(timezone.utc).date() - timedelta(days=3)  # archive lags ~72h
    start_date = end_date - timedelta(days=days)

    # Build the full list of work up front, skipping anything already done,
    # so the thread pool has a clear, static list of tasks to chew through.
    tasks = []
    for loc in locations:
        current = start_date
        while current <= end_date:
            key = (loc["location_id"], current.isoformat())
            if key not in done:
                tasks.append((loc["location_id"], loc["location_name"], current))
            current += timedelta(days=1)

    print(f"  {len(tasks)} (location, day) pairs remaining to fetch.")
    if not tasks:
        print("Backfill: nothing left to do.")
        return

    output_exists = os.path.exists(BACKFILL_OUTPUT_PATH)
    total_new_rows = 0
    completed_since_checkpoint = 0

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=max_workers)
    session.mount("https://", adapter)

    def _worker(task):
        location_id, location_name, date = task
        try:
            df = _fetch_archive_file(location_id, date, session=session)
            return location_id, location_name, date, df, None
        except Exception as e:
            return location_id, location_name, date, None, e

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_worker, t) for t in tasks]

            for future in as_completed(futures):
                location_id, location_name, date, df, error = future.result()
                key = (location_id, date.isoformat())

                if error is not None:
                    print(f"  Skipped {location_name} on {date}: {error}")
                    # Deliberately NOT marked as done -- retried on the next run.
                else:
                    if df is not None and not df.empty:
                        normalized = _normalize_archive_df(df, location_name)
                        normalized.to_csv(BACKFILL_OUTPUT_PATH, index=False,
                                           mode="a", header=not output_exists)
                        output_exists = True
                        total_new_rows += len(normalized)
                    done.add(key)

                completed_since_checkpoint += 1
                if completed_since_checkpoint >= checkpoint_every:
                    _save_backfill_progress(done)
                    completed_since_checkpoint = 0
    finally:
        # Always checkpoint on the way out, including on Ctrl+C or an
        # unexpected exception, so a partially-finished batch isn't lost.
        _save_backfill_progress(done)
        session.close()

    print(f"Backfill: wrote {total_new_rows} new rows to {BACKFILL_OUTPUT_PATH}. "
          f"{len(done)} total (location, day) pairs completed across {len(locations)} locations.")

# ---------------------------------------------------------------------------
# Combined run
# ---------------------------------------------------------------------------

def fetch_all():
    """
    LIVE path -- what the hourly Airflow DAG should call.
    Does not touch the AWS archive at all.
    """
    cpcb_df = fetch_cpcb_data()
    openaq_df = fetch_openaq_data()
    combined = pd.concat([cpcb_df, openaq_df], ignore_index=True)
    print(f"\nTotal combined live readings: {len(combined)}")
    return combined


def _save(df, table_name="raw_aqi_readings"):
    """
    FIX #1: to_csv(mode="a") writes a header row on every call regardless
    of append mode -- previously this meant a duplicate header line got
    injected every hour when running without Postgres. Now the header is
    only written the first time the file is created.
    """
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        from sqlalchemy import create_engine
        engine = create_engine(db_url)
        df.to_sql(table_name, engine, if_exists="append", index=False)
        print(f"Written to Postgres table '{table_name}'.")
    else:
        out_path = f"{table_name}.csv"
        file_exists = os.path.exists(out_path)
        df.to_csv(out_path, index=False, mode="a", header=not file_exists)
        print(f"DATABASE_URL not set -- appended to {out_path} instead.")


if __name__ == "__main__":
    import sys

    if "--backfill" in sys.argv:
        # Run this ONCE up front (or occasionally) to seed real history:
        #   python ingest.py --backfill
        #
        # FIX #4: backfill_openaq_from_aws() now writes to CSV internally
        # and no longer returns a DataFrame, so there's nothing to pass
        # to _save() here anymore -- the call is the whole job.
        backfill_openaq_from_aws(days=90)
    else:
        # This is what the hourly Airflow DAG should call:
        #   python ingest.py
        df = fetch_all()
        _save(df, table_name="raw_aqi_readings")
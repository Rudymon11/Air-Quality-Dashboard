{{ config(materialized='table') }}

/*
   D: hygroscopic particulate / under-deposit pitting screening.

   SAPR is a project heuristic based on the available gas measurements.  It is
   not a validated corrosion or maintenance decision model.  Calculating it at
   station/day preserves the local context that city-level aggregates lose.
*/
WITH daily AS (
    SELECT
        city,
        station,
        DATE_TRUNC('day', reading_time_utc)::date AS reading_date,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'PM2.5') AS avg_pm25,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'PM10') AS avg_pm10,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'NH3') AS avg_nh3,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'SO2') AS avg_so2,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'NO2') AS avg_no2,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'HUMIDITY') AS avg_humidity,
        AVG(CASE WHEN pollutant = 'HUMIDITY' AND pollutant_value >= 60 THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE pollutant = 'HUMIDITY') AS humid_exposure_fraction,
        COUNT(*) FILTER (WHERE pollutant = 'PM2.5') AS pm25_readings,
        COUNT(*) FILTER (WHERE pollutant = 'NH3') AS nh3_readings,
        COUNT(*) FILTER (WHERE pollutant = 'SO2') AS so2_readings,
        COUNT(*) FILTER (WHERE pollutant = 'NO2') AS no2_readings,
        COUNT(*) FILTER (WHERE pollutant = 'HUMIDITY') AS humidity_readings,
        COUNT(DISTINCT reading_time_utc) AS observed_timestamps,
        STRING_AGG(DISTINCT source, ', ' ORDER BY source) AS sources
    -- Use the project's refreshed materialized staging view so this mart does
    -- not recompute the full raw-table deduplication on every build.
    FROM stg_aqi_readings_mat
    WHERE pollutant IN ('PM2.5', 'PM10', 'NH3', 'SO2', 'NO2', 'HUMIDITY')
    GROUP BY 1, 2, 3
),
scored AS (
    SELECT *,
        CASE
            WHEN pm25_readings = 0 OR humidity_readings = 0
              OR nh3_readings = 0 OR so2_readings = 0 OR no2_readings = 0
                THEN NULL
            ELSE COALESCE(avg_nh3, 0) * (COALESCE(avg_so2, 0) + COALESCE(avg_no2, 0)) / 1000.0
        END AS sapr,
        CASE
            WHEN pm25_readings = 0 OR humidity_readings = 0 THEN FALSE
            ELSE COALESCE(avg_pm25, 0) > 60
                 AND COALESCE(avg_humidity, 0) >= 60
                 AND COALESCE(humid_exposure_fraction, 0) >= 0.25
        END AS wetting_active
    FROM daily
)
SELECT
    city,
    station,
    reading_date,
    ROUND(avg_pm25::numeric, 2) AS avg_pm25,
    ROUND(avg_pm10::numeric, 2) AS avg_pm10,
    ROUND(avg_nh3::numeric, 2) AS avg_nh3,
    ROUND(avg_so2::numeric, 2) AS avg_so2,
    ROUND(avg_no2::numeric, 2) AS avg_no2,
    ROUND(avg_humidity::numeric, 2) AS avg_humidity,
    ROUND((humid_exposure_fraction * 100)::numeric, 2) AS humid_exposure_percent,
    ROUND(sapr::numeric, 4) AS sapr,
    wetting_active,
    CASE
        WHEN sapr IS NULL THEN 'insufficient_data'
        WHEN NOT wetting_active THEN 'low_screening'
        WHEN sapr < 1 THEN 'low_screening'
        WHEN sapr < 5 THEN 'moderate_screening'
        WHEN sapr < 10 THEN 'high_screening'
        ELSE 'critical_screening'
    END AS screening_category,
    pm25_readings,
    nh3_readings,
    so2_readings,
    no2_readings,
    humidity_readings,
    observed_timestamps,
    sources
FROM scored

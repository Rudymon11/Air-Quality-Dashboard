{{ config(materialized='table') }}

/*
   B: ISO-9223-inspired atmospheric corrosivity screening.

   This is intentionally not presented as an ISO-compliant corrosion-rate
   calculation.  ISO corrosion categories require validated exposure data,
   including salinity/chloride and first-year corrosion measurements.  This
   student-project feature uses the measurements currently available to flag
   relatively more corrosive environments.
*/
WITH daily AS (
    SELECT
        city,
        station,
        DATE_TRUNC('day', reading_time_utc)::date AS reading_date,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'SO2') AS avg_so2,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'PM10') AS avg_pm10,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'TEMP') AS avg_temp,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'HUMIDITY') AS avg_humidity,
        AVG(CASE WHEN pollutant = 'HUMIDITY' AND pollutant_value >= 80 THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE pollutant = 'HUMIDITY') AS humid_exposure_fraction,
        COUNT(*) FILTER (WHERE pollutant = 'SO2') AS so2_readings,
        COUNT(*) FILTER (WHERE pollutant = 'HUMIDITY') AS humidity_readings,
        COUNT(DISTINCT reading_time_utc) AS observed_timestamps,
        STRING_AGG(DISTINCT source, ', ' ORDER BY source) AS sources
    -- Use the project's refreshed materialized staging view so this mart does
    -- not recompute the full raw-table deduplication on every build.
    FROM stg_aqi_readings_mat
    WHERE pollutant IN ('SO2', 'PM10', 'TEMP', 'HUMIDITY')
    GROUP BY 1, 2, 3
),
scored AS (
    SELECT *,
        CASE
            WHEN so2_readings = 0 OR humidity_readings = 0 THEN NULL
            ELSE 100.0 * (
                0.60 * (1.0 - EXP(-COALESCE(avg_so2, 0) / 80.0))
                + 0.40 * COALESCE(humid_exposure_fraction, 0)
            )
        END AS corrosivity_screening_score
    FROM daily
)
SELECT
    city,
    station,
    reading_date,
    ROUND(avg_so2::numeric, 2) AS avg_so2,
    ROUND(avg_pm10::numeric, 2) AS avg_pm10,
    ROUND(avg_temp::numeric, 2) AS avg_temp,
    ROUND(avg_humidity::numeric, 2) AS avg_humidity,
    ROUND((humid_exposure_fraction * 100)::numeric, 2) AS humid_exposure_percent,
    ROUND(corrosivity_screening_score::numeric, 2) AS corrosivity_screening_score,
    CASE
        WHEN corrosivity_screening_score IS NULL THEN 'insufficient_data'
        WHEN corrosivity_screening_score < 20 THEN 'C1_screening'
        WHEN corrosivity_screening_score < 40 THEN 'C2_screening'
        WHEN corrosivity_screening_score < 60 THEN 'C3_screening'
        WHEN corrosivity_screening_score < 80 THEN 'C4_screening'
        WHEN corrosivity_screening_score < 95 THEN 'C5_screening'
        ELSE 'CX_screening'
    END AS screening_category,
    so2_readings,
    humidity_readings,
    observed_timestamps,
    sources
FROM scored

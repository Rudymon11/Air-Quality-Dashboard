{{ config(materialized='table') }}

/*
   C: electronics-environment screening proxy.

   The proposal relates airborne SO2, NO2, O3 and humidity to contamination
   risk for electronics. This model is not an ISA-71.04 classification: the
   current data is outdoor station data and has no enclosure, ventilation or
   asset-specific exposure context.
*/
WITH daily AS (
    SELECT
        city,
        station,
        DATE_TRUNC('day', reading_time_utc)::date AS reading_date,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'SO2') AS avg_so2,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'NO2') AS avg_no2,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'O3') AS avg_o3,
        AVG(pollutant_value) FILTER (WHERE pollutant = 'HUMIDITY') AS avg_humidity,
        AVG(CASE WHEN pollutant = 'HUMIDITY' AND pollutant_value >= 50 THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE pollutant = 'HUMIDITY') AS humid_exposure_fraction,
        COUNT(*) FILTER (WHERE pollutant = 'SO2') AS so2_readings,
        COUNT(*) FILTER (WHERE pollutant = 'NO2') AS no2_readings,
        COUNT(*) FILTER (WHERE pollutant = 'O3') AS o3_readings,
        COUNT(*) FILTER (WHERE pollutant = 'HUMIDITY') AS humidity_readings,
        COUNT(DISTINCT reading_time_utc) AS observed_timestamps,
        STRING_AGG(DISTINCT source, ', ' ORDER BY source) AS sources
    FROM stg_aqi_readings_mat
    WHERE pollutant IN ('SO2', 'NO2', 'O3', 'HUMIDITY')
    GROUP BY 1, 2, 3
),
scored AS (
    SELECT *,
        CASE
            WHEN so2_readings = 0 OR no2_readings = 0 OR o3_readings = 0 OR humidity_readings = 0 THEN NULL
            ELSE 100.0 * (
                0.25 * (1.0 - EXP(-COALESCE(avg_so2, 0) / 20.0))
                + 0.25 * (1.0 - EXP(-COALESCE(avg_no2, 0) / 40.0))
                + 0.20 * (1.0 - EXP(-COALESCE(avg_o3, 0) / 100.0))
                + 0.30 * COALESCE(humid_exposure_fraction, 0)
            )
        END AS electronics_screening_score
    FROM daily
)
SELECT
    city,
    station,
    reading_date,
    ROUND(avg_so2::numeric, 2) AS avg_so2,
    ROUND(avg_no2::numeric, 2) AS avg_no2,
    ROUND(avg_o3::numeric, 2) AS avg_o3,
    ROUND(avg_humidity::numeric, 2) AS avg_humidity,
    ROUND((humid_exposure_fraction * 100)::numeric, 2) AS humid_exposure_percent,
    ROUND(electronics_screening_score::numeric, 2) AS electronics_screening_score,
    CASE
        WHEN electronics_screening_score IS NULL THEN 'insufficient_data'
        WHEN electronics_screening_score < 20 THEN 'G1_screening'
        WHEN electronics_screening_score < 40 THEN 'G2_screening'
        WHEN electronics_screening_score < 70 THEN 'G3_screening'
        ELSE 'GX_screening'
    END AS screening_category,
    so2_readings,
    no2_readings,
    o3_readings,
    humidity_readings,
    observed_timestamps,
    sources
FROM scored

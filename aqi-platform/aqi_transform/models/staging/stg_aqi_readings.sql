WITH raw_data AS (
    SELECT * FROM {{ source('raw', 'raw_aqi_readings') }}
),

cleaned AS (
    SELECT 
        city,
        station,
        CAST(pollutant AS TEXT) AS pollutant,
        CAST(value AS NUMERIC) AS pollutant_value,
        unit,
        CAST(reading_time_utc AS TIMESTAMPTZ) AS reading_time_utc,
        source,
        CAST(ingested_at AS TIMESTAMPTZ) AS ingested_at
    FROM raw_data
    -- Filter out impossible negative readings and completely null values
    WHERE value IS NOT NULL 
      AND value >= 0
)

-- Deduplicate just in case the hourly Airflow run pulled the exact same timestamp twice
SELECT DISTINCT * FROM cleaned
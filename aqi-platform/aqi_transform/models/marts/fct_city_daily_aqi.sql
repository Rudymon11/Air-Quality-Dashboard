{{ config(materialized='table') }}

WITH clean_data AS (
    SELECT * FROM {{ ref('stg_aqi_readings') }}
)

SELECT 
    city,
    DATE_TRUNC('day', reading_time_utc) AS reading_date,
    source,
    
    -- Particulate Matter
    ROUND(AVG(CASE WHEN pollutant = 'PM2.5' THEN pollutant_value END), 2) AS avg_pm25,
    ROUND(AVG(CASE WHEN pollutant = 'PM10' THEN pollutant_value END), 2) AS avg_pm10,
    
    -- Gases
    ROUND(AVG(CASE WHEN pollutant = 'SO2' THEN pollutant_value END), 2) AS avg_so2,
    ROUND(AVG(CASE WHEN pollutant = 'NO2' THEN pollutant_value END), 2) AS avg_no2,
    ROUND(AVG(CASE WHEN pollutant = 'NO' THEN pollutant_value END), 2) AS avg_no,
    ROUND(AVG(CASE WHEN pollutant = 'CO' THEN pollutant_value END), 2) AS avg_co,
    ROUND(AVG(CASE WHEN pollutant = 'NH3' THEN pollutant_value END), 2) AS avg_nh3,
    ROUND(AVG(CASE WHEN pollutant = 'O3' THEN pollutant_value END), 2) AS avg_o3,
    
    -- Daily Metadata
    MAX(pollutant_value) AS max_pollutant_value,
    COUNT(DISTINCT station) AS active_stations
FROM clean_data
GROUP BY 1, 2, 3
ORDER BY reading_date DESC, city, source
WITH clean_data AS (
    SELECT * FROM {{ ref('stg_aqi_readings') }}
)

SELECT 
    city,
    DATE_TRUNC('day', reading_time_utc) AS reading_date,
    ROUND(AVG(CASE WHEN pollutant = 'PM2.5' THEN pollutant_value END), 2) AS avg_pm25,
    ROUND(AVG(CASE WHEN pollutant = 'PM10' THEN pollutant_value END), 2) AS avg_pm10,
    MAX(pollutant_value) AS max_pollutant_value,
    COUNT(DISTINCT station) AS active_stations
FROM clean_data
GROUP BY 1, 2
ORDER BY reading_date DESC, city
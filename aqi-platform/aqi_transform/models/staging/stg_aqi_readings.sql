WITH raw_data AS (
    SELECT * FROM {{ source('raw', 'raw_aqi_readings') }}
),

standardized AS (
    SELECT 
        -- 1. Standardize City Names
        CASE 
            WHEN city ILIKE '%delhi%' THEN 'Delhi'
            WHEN city ILIKE '%ludhiana%' THEN 'Ludhiana'
            WHEN city ILIKE '%lucknow%' THEN 'Lucknow'
            WHEN city ILIKE '%srinagar%' THEN 'Srinagar'
            WHEN city ILIKE '%dehradun%' THEN 'Dehradun'
            WHEN city ILIKE '%mumbai%' THEN 'Mumbai'
            WHEN city ILIKE '%ahmedabad%' THEN 'Ahmedabad'
            WHEN city ILIKE '%panaji%' THEN 'Panaji'
            WHEN city ILIKE '%kochi%' THEN 'Kochi'
            WHEN city ILIKE '%visakhapatnam%' THEN 'Visakhapatnam'
            WHEN city ILIKE '%chennai%' THEN 'Chennai'
            WHEN city ILIKE '%bengaluru%' THEN 'Bengaluru'
            WHEN city ILIKE '%hyderabad%' THEN 'Hyderabad'
            WHEN city ILIKE '%patna%' THEN 'Patna'
            WHEN city ILIKE '%kolkata%' THEN 'Kolkata'
            WHEN city ILIKE '%guwahati%' THEN 'Guwahati'
            WHEN city ILIKE '%shillong%' THEN 'Shillong'
            WHEN city ILIKE '%bhopal%' THEN 'Bhopal'
            WHEN city ILIKE '%indore%' THEN 'Indore'
            WHEN city ILIKE '%nagpur%' THEN 'Nagpur'
            ELSE city 
        END AS city,
        
        station,
        
        -- 2. Standardize Pollutant Names
        CASE 
            WHEN LOWER(REPLACE(CAST(pollutant AS TEXT), '.', '')) = 'pm25' THEN 'PM2.5'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'pm10' THEN 'PM10'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'pm1' THEN 'PM1'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'um003' THEN 'UM003'
            WHEN LOWER(CAST(pollutant AS TEXT)) IN ('o3', 'ozone') THEN 'O3'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'relativehumidity' THEN 'HUMIDITY'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'temperature' THEN 'TEMP'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'wind_speed' THEN 'WIND_SPEED'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'wind_direction' THEN 'WIND_DIR'
            ELSE UPPER(CAST(pollutant AS TEXT)) 
        END AS pollutant,
        
        -- 3. REMOVE EPA MATH: Indian data is already native µg/m³.
        CASE
            -- CO Logic Fix: If CO is surprisingly low (< 50) and not marked as ppb, 
            -- it was almost certainly recorded in mg/m³, so we multiply by 1000 to reach µg/m³
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'co' AND CAST(value AS NUMERIC) < 50 AND LOWER(unit) != 'ppb' THEN CAST(value AS NUMERIC) * 1000.0
            
            ELSE CAST(value AS NUMERIC)
        END AS pollutant_value,
        
        CASE 
            WHEN LOWER(unit) IN ('ppb', 'ppm', 'mg/m³') THEN 'µg/m³'
            WHEN LOWER(CAST(pollutant AS TEXT)) = 'co' AND CAST(value AS NUMERIC) < 50 THEN 'µg/m³'
            ELSE unit
        END AS unit,
        
        CAST(reading_time_utc AS TIMESTAMPTZ) AS reading_time_utc,
        source,
        CAST(ingested_at AS TIMESTAMPTZ) AS ingested_at
    FROM raw_data
        WHERE value IS NOT NULL 
      -- The Zombie Purge: drop any historical garbage older than 2026
        AND CAST(reading_time_utc AS TIMESTAMPTZ) >= '2026-01-01'
      -- The Freshness Purge: Drop old OpenAQ live data that we accidentally saved before our Python fix
      AND NOT (
          source = 'OpenAQ' 
          AND CAST(reading_time_utc AS TIMESTAMPTZ) < CAST(ingested_at AS TIMESTAMPTZ) - INTERVAL '48 hours'
      )
),
validated AS (
    SELECT * 
    FROM standardized
    -- 4. Apply strict sensor hardware boundary filters to drop broken/glitched readings
    WHERE 
        (pollutant = 'PM2.5'      AND pollutant_value > 0 AND pollutant_value < 998)
     OR (pollutant = 'PM10'       AND pollutant_value > 0 AND pollutant_value < 1498)
     OR (pollutant = 'PM1'        AND pollutant_value > 0 AND pollutant_value < 498)
     OR (pollutant = 'UM003'      AND pollutant_value > 0 AND pollutant_value < 99998)
     OR (pollutant = 'CO'         AND pollutant_value > 0 AND pollutant_value < 19998)
     OR (pollutant = 'NO2'        AND pollutant_value > 0 AND pollutant_value < 598)
     OR (pollutant = 'NO'         AND pollutant_value > 0 AND pollutant_value < 498)
     OR (pollutant = 'NOX'        AND pollutant_value > 0 AND pollutant_value < 1498)
     OR (pollutant = 'O3'         AND pollutant_value > 0 AND pollutant_value < 598)
     OR (pollutant = 'SO2'        AND pollutant_value > 0 AND pollutant_value < 798)
     OR (pollutant = 'NH3'        AND pollutant_value > 0 AND pollutant_value < 798)
     
     -- Weather Limits
     OR (pollutant = 'TEMP'       AND pollutant_value != 0.0 AND pollutant_value > -20.0 AND pollutant_value < 50.0)
     OR (pollutant = 'HUMIDITY'   AND pollutant_value >= 0 AND pollutant_value <= 100)
     OR (pollutant = 'WIND_SPEED' AND pollutant_value >= 0 AND pollutant_value < 50)
     OR (pollutant = 'WIND_DIR'   AND pollutant_value >= 0 AND pollutant_value <= 360)
     
     -- Fallback
     OR (pollutant NOT IN ('PM2.5', 'PM10', 'PM1', 'UM003', 'CO', 'NO2', 'NO', 'NOX', 'O3', 'SO2', 'NH3', 'TEMP', 'HUMIDITY', 'WIND_SPEED', 'WIND_DIR') AND pollutant_value >= 0)
),

-- 5. Prioritize high-quality data sources over unverified high-frequency streams
ranked_sources AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY station, pollutant, reading_time_utc 
            ORDER BY CASE 
                WHEN source = 'CPCB' THEN 1 
                WHEN source = 'OpenAQ' THEN 2 
                ELSE 3 
            END
        ) as source_priority
    FROM validated
)

-- Deduplicate
SELECT 
    city,
    station,
    pollutant,
    pollutant_value,
    unit,
    reading_time_utc,
    source,
    ingested_at
FROM ranked_sources
WHERE source_priority = 1
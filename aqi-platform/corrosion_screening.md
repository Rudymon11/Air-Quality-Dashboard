# Corrosion screening features

This document records how the proposal PDF, *Predictive Corrosion Intelligence:
Adapting the Air-Quality-Dashboard for Asset Integrity & Unmonitored Industrial
Zones*, relates to this student project. The PDF is design context, not a
source of verified measurements or automatic engineering rules.

## Implemented pathways

| Pathway | Relationship | Parameters used here | Status |
|---|---|---|---|
| B | Atmospheric exposure conditions can contribute to corrosion of exposed metals. | SO2, relative humidity and temperature context. | Implemented as an ISO-inspired bounded screening score. |
| C | Airborne contaminants and humidity can contribute to contamination/corrosion of electronics. | SO2, NO2, O3 and relative humidity. | Implemented as a bounded electronics-environment screening score. |
| D | Hygroscopic particles can absorb moisture and support under-deposit corrosion. | PM2.5, PM10, NH3, SO2, NO2 and relative humidity. | Implemented as the existing SAPR heuristic. |
| E | Ammonia exposure may contribute to SCC in susceptible copper/brass equipment. | NH3, O3, humidity and asset/material/stress data. | Not implemented. |

## Pathway B formula

This is not the ISO 9223 dose-response calculation. It is a bounded project
proxy using the available SO2 data and RH >= 80% as a simple time-of-wetness
proxy:

```text
corrosivity_score = 100 * (
    0.60 * (1 - exp(-average_SO2 / 80))
  + 0.40 * RH_80_exposure_fraction
)
```

The exponential term is a saturation function: increasing SO2 increases the
score but cannot make it explode. The score is always between 0 and 100.
`avg_humidity` is the actual average humidity. `humid_exposure_percent` is the
percentage of humidity observations at or above 80% RH, which represents
duration of humid conditions rather than the average alone.

Categories are project-defined: below 20 C1_screening, 20-39.99 C2_screening,
40-59.99 C3_screening, 60-79.99 C4_screening, 80-94.99 C5_screening and 95+
CX_screening.

## Pathway C formula

This is not an official ISA-71.04 classification. It is a bounded screening
proxy for the outdoor environment around a possible electronics asset:

```text
electronics_score = 100 * (
    0.25 * (1 - exp(-average_SO2 / 20))
  + 0.25 * (1 - exp(-average_NO2 / 40))
  + 0.20 * (1 - exp(-average_O3 / 100))
  + 0.30 * RH_50_exposure_fraction
)
```

The score is always between 0 and 100. The exponential terms prevent a single
large pollutant reading from producing an absurd linear score. RH_50 exposure
is the percentage of humidity observations at or above 50% RH. Categories are
project-defined: below 20 G1_screening, 20-39.99 G2_screening, 40-69.99
G3_screening and 70+ GX_screening.

## Pathway D formula

The existing raw SAPR heuristic is:

```text
SAPR = average_NH3 * (average_SO2 + average_NO2) / 1000
```

`wetting_active` requires average PM2.5 > 60, average humidity >= 60% and at
least 25% of humidity observations at or above 60%. SAPR categories remain the
existing project thresholds: missing inputs means insufficient_data, inactive
wetting or SAPR < 1 means low_screening, 1-4.99 means moderate_screening,
5-9.99 means high_screening and 10+ means critical_screening.

## Why this remains limited

The current data does not include chloride deposition, corrosion coupons,
first-year corrosion loss, indoor/enclosure conditions, ventilation, asset
materials, alloy composition, coatings, mechanical stress, maintenance history,
source apportionment or laboratory/field labels. Therefore these outputs are
environmental screening indicators, not corrosion rates, official ISO/ISA
classifications, asset-failure predictions or maintenance instructions.

The current station/day aggregation also does not prove that every pollutant
was measured at exactly the same instant.

## Example queries

```sql
SELECT *
FROM fct_station_daily_corrosivity
WHERE city = 'Delhi'
ORDER BY corrosivity_screening_score DESC NULLS LAST;

SELECT *
FROM fct_station_daily_electronics_risk
WHERE city = 'Delhi'
ORDER BY electronics_screening_score DESC NULLS LAST;
```

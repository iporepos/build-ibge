-- IBGE Census 2022 T2: aggregated population per municipio (sum of the census tracts in `basico`).
-- Run in the QGIS DB Manager SQL window (or sqlite/ogr) on IBGE_CENSO-2022_BD_BRASIL_2022.gpkg.
-- basico.V0001 = total residents, V0002 = total dwellings, V0007 = occupied private dwellings (checked against demografia V01006 in T2).
-- Flags (-9999 no data, -1 hidden, -2 uncertain) are excluded from the sums; basico is expected to carry few or none,
-- and `tracts_flagged` counts them so a municipio whose sum is a lower bound can be spotted.
-- Join to the `municipios` layer (key municipio_code) for geometry/QGIS mapping.
SELECT
    b.municipio_code,
    b.municipio_name,
    b.unidade_alias,
    COUNT(*)                                                      AS tracts,
    SUM(CASE WHEN b.V0001 < 0 THEN 1 ELSE 0 END)                  AS tracts_flagged,
    SUM(CASE WHEN b.V0001 >= 0 THEN b.V0001 ELSE 0 END)           AS population,
    SUM(CASE WHEN b.V0002 >= 0 THEN b.V0002 ELSE 0 END)           AS dwellings,
    SUM(CASE WHEN b.V0007 >= 0 THEN b.V0007 ELSE 0 END)           AS dwellings_occupied,
    SUM(CASE WHEN b.situacao_type = 'Urbana' AND b.V0001 >= 0 THEN b.V0001 ELSE 0 END) AS population_urban,
    SUM(CASE WHEN b.situacao_type = 'Rural'  AND b.V0001 >= 0 THEN b.V0001 ELSE 0 END) AS population_rural,
    SUM(b.area_km2)                                               AS area_km2,
    SUM(CASE WHEN b.V0001 >= 0 THEN b.V0001 ELSE 0 END) / NULLIF(SUM(b.area_km2), 0) AS population_per_km2
FROM basico AS b
GROUP BY b.municipio_code, b.municipio_name, b.unidade_alias
ORDER BY b.municipio_code;

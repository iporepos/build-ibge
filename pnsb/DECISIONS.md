# PNSB — decisions

Log of the decisions on the Pesquisa Nacional de Saneamento Básico (PNSB) datasets. The survey was first built
inside ATLASSAN (2026-10-26, `atlassan/DECISIONS.md` §2–7, 13–14); those items are kept here as they apply now.
"User" = decided by the user; "Claude" = taken by Claude, to be confirmed.

## 2026-10-27

1. **PNSB is its own warehouse entry, one dataset per survey year, like the censuses** (user: "PNSB is a separate
   entry in the warehouse … just like the census, we may split that by year, allowing any possible change in the
   column list"). Datasets `PNSB-2017`, `PNSB-2008`, `PNSB-2000`, each `V1` with its own SRC/T1 (work folders
   `data/PNSB-<year>/V1`). ATLASSAN is a separate, standalone product (the Atlas maps only), with data that
   repeat the PNSB.
   - One code folder `pnsb/` (same scripts for every year, like `censos/`), one config per year
     (`download_pnsb_<year>`, `assemble_pnsb_<year>`) and one pair of hand-kept catalogs per year
     (`IBGE_PNSB-<year>_LAYERS_X_X.csv` CRLF, `IBGE_PNSB-<year>_FIELDS_X_X.csv` LF) (Claude).
   - Files `IBGE_PNSB-<year>_T<table>_BRASIL_<year>.csv`, layer `t<table>` (Claude). No `year` column (the
     dataset is the year, as in the census files): columns `municipio_code;municipio_name;v…`.
   - LAYERS has one row per SIDRA table (as the census has one per file), description = SIDRA table name and
     subject, theme from the SIDRA subject (water, sewage, solidwaste, stormwater, management); `description_en`
     is NA for now (O2). The assembler stops if a table has no LAYERS row (config `layers_catalog`), so a new
     SIDRA table must be added to LAYERS by hand.
   - Tables of SIDRA spanning several years (2000–2008, 2008–2017, 2000–2017; 39 tables) are in every year they
     cover, with that year's values; TABLES gives the table period.
   - VARIABLES lists only the columns present in the file (SIDRA categories without any value are not
     columns), with no atlas-only fields.
   - The SRC of the night run was split by year, without downloading again (`download_pnsb.py` on one year
     gives the same bytes: checked on T187 and T1236 of 2008). T1 of the three years = the night T1 without the
     `year` column (304 files, compared cell by cell, identical; VARIABLES 7,317 rows as before).

## Kept from 2026-10-26 (were ATLASSAN §2–7, 13)

2. **Source: SIDRA survey `SB`**: 263 tables, all at municipal level (N6). Periods: 2000 (111), 2008 (59), 2017
   (54), 2000–2008 (30), 2008–2017 (7), 2000–2017 (2). Per year: 2017 63 tables / 3,364 columns, 2008 98 / 1,668,
   2000 143 / 2,285. Subjects: water supply, sewage, solid waste, storm water, municipal management.
3. **Download** (Claude): values API `apisidra.ibge.gov.br/values` with codes only (`/f/c/h/y`), metadata from
   `servicodados.ibge.gov.br/api/v3/agregados/<t>/metadados`. SIDRA refuses >50,000 values per request, so
   requests are cut by category. The v3 values API answers 500 for several variables at once, not used.
   Responses of a table-year = one reproducible gzip CSV (`SRC/SIDRA/values/T<t>_<year>_N6.csv.gz`) + manifest.
4. **Rows** (Claude): SIDRA lists today's 5,570 municipalities for every year (`...` before installation). T1 rows
   = municipalities of the DTB edition of the year (2000 → 5,507, 2008 → 5,564, 2017 → 5,570). Extra SIDRA
   codes dropped (CONSISTENCY row only if one carried a value).
5. **Symbols** (Claude): `-` → 0, `X` → −1, `..` and `...` → −9999. Integer columns stay integers.
6. **Column names** (Claude): `v<variable>` + `_<category>` per classification in SIDRA order (`v10676_48278`);
   names in VARIABLES. One file per table (not one wide municipal table: 3,364 columns for 2017 exceed the
   SQLite/GeoPackage 2,000-column limit). Alternative: a long table in T2.
7. **Count variables** (Claude): at N6 the "Municípios com …" variables are 1/0 dummies (warning otherwise; T1236
   of 2008 counts cooperatives, values up to 6, harmless).
8. **Sub-universe tables** (Claude): 34 table-years list only part of the municipalities (e.g. 5,548 with a water
   network). The others get −9999 (no data, not zero); one CONSISTENCY row per file (`not_in_sidra`, value =
   number of municipalities missing).

## 2026-10-27 (later)

9. **English descriptions of LAYERS** (user asked; Claude translated): all 263 SIDRA table names, in the three
   LAYERS files (304 rows). "economias" kept as "units (economias)"; ETA/ETE as WTP/STP where spelled out.
   VARIABLES stay Portuguese (SIDRA texts; 7,317 rows).
10. **T2** (user: "the builder script for all pnsb and atlassan"; design Claude): `censos/build_database_tables.py`,
    shared with ATLASSAN (next to `package_t1.py`, which also serves DTB and POP). One GeoPackage per year
    `IBGE_PNSB-<year>_BD_BRASIL_<year>.gpkg`, no geometry, one table per T1 file (`t<table>`, same columns and
    values, typed: codes and names text from FIELDS, value columns integer / real), the catalogs, and a T2
    layers table built from the T1 LAYERS. Unique index on `municipio_code`. Wide per table as in T1 (decision 6);
    a long table was not built. Built in `data/`: 2017 39.6 MB, 2008 39.8 MB, 2000 53.8 MB; all tables equal to T1.

## 2026-10-27 (geography)

11. **Plotting geography from the DTB, in T1 and T2** (user: "all datasets may need some spatial layers as auxiliar
    plotting assets. take from DTB. Both T1 and T2"; design Claude). `censos/geography_from_dtb_editions.py`
    (shared by PNSB and ATLASSAN, configs `geography_<dataset>`): per DTB edition used by the tables,
    `GEO-MUNICIPIOS`, `GEO-SEDES`, `GEO-UNIDADES`, `GEO-PAIS` (+ `GEO-REGIOESINTERMEDIARIAS` for 2017) as T1 gpkgs;
    the T2 builder copies them with geometry (`geo_<name>`).
    - The DTB has meshes for 2000, 2010 and 2022 only: edition 2000 ← mesh 2000, 2008 ← 2010 (Nazária 2206720,
      created 2009, dissolved into Teresina by the DTB linhagem; `merged_codes`), 2017 ← 2022 (same 5,570 codes).
      Lagoons 4300001/4300002 dropped. Codes checked equal to the rows of the tables.
    - Simplified for maps with shapely coverage_simplify 0.001° (~110 m; 2022 mesh 276 → 38 MB): not the
      official mesh (said in LAYERS). States, intermediate regions and country dissolved from the simplified
      municipalities (same borders), holes < 0.0001°² filled (gaps of the DTB mesh, 54,000 holes in 2010).
    - Intermediate regions of 2017: the quadro has NA until edition 2019; codes taken from 2019
      (`codes_from`, checked same municipalities); equal to the ATLASSAN region codes.
    - No world countries in the DTB: no geometry for the ATLASSAN PAISES tables (only the Brazil outline).
    - Theme `geography` in LAYERS; new FIELDS merged_codes, mesh_year, capital_type, geometry, names.

## Open
- O1. Map ↔ SIDRA correspondence with ATLASSAN (which atlas map repeats which PNSB column).
- O2. English descriptions of the variables (VARIABLES); the tables are done (LAYERS).
- O4. Keep the other subjects (storm water, solid waste, management) or only water and sewage? Kept for now
  (same source, cheap).
- O5. PNSB 1989: not in SIDRA (publication tables only); not built.
- O6. Comparability across years (a T2 or a crosswalk of the variables 2000/2008/2017): not started.

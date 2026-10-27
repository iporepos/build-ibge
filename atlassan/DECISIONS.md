# ATLASSAN — decisions

Log of the decisions on the Atlas de Saneamento dataset. Assessment and source description: `NOTES.md`.
"User" = decided by the user; "Claude" = taken by Claude while the user was away (2026-10-26 night), to be
confirmed — each says why and how to undo it.

## 2026-10-26

1. **Name ATLASSAN** (user). Work folder `data/ATLASSAN/V1` (SRC/T1/T2), warehouse `<IBGE root>/ATLASSAN/V1`
   on promotion. The old warehouse `SANEAMENTO/V1` (legacy CSVs only, empty T0/T1/T2) is not used; it can be
   deleted by the user once ATLASSAN is promoted.

2. **One source family where possible: SIDRA for everything the PNSB publishes** (user asked "retrieve
   everything possible from the same source, maybe SIDRA"; Claude checked):
   - SIDRA survey `SB` (PNSB) has 263 tables; **all at municipal level (N6)**. Periods: 2000 (111 tables),
     2008 (59), 2017 (54), multi-period 2000–2008 (30), 2008–2017 (7), 2000–2017 (2). For 2017: 63 tables,
     3,364 value columns, including the chapter-2 subjects absent from the GeoServer (treatment types and
     volumes, interruptions, rationing, losses, sewage treated, ETE/ETA counts, sludge, receiving bodies).
   - The municipal values are the published survey counts and volumes (e.g. T7479 volume of sewage treated per
     day by treatment type), not map classes, so they are better than the atlas maps for analysis.
   - **Decision (Claude): SIDRA PNSB is the primary source; the years 2017, 2008 and 2000 are all downloaded**
     (same source, same structure; 2008/2000 give the time comparison the atlas itself makes). T1 = one wide
     file per SIDRA table and year (`IBGE_ATLASSAN_PNSB-<table>_BRASIL_<year>.csv`). Undo: drop years from
     `download_pnsb` / `assemble_pnsb` configs.
   - **The GeoServer atlas layers are kept as a second family** (`ATLAS-*` files): they hold what SIDRA does not
     have — the chapter-4 health indicators (DATASUS-based rates 2008–2019 by municipality / intermediate
     region), international comparisons (WHO, ISO3), REGIC health travel 2018, census-based household shares,
     and indicators IBGE computed for the maps from the PNSB microdata at entity level (shares of entities,
     tariffs, staff by contract type) that no SIDRA table gives by municipality. All 194 layers are harvested
     and kept, including those that repeat SIDRA content: mapping each map to a SIDRA column is manual work
     left open (item O1).

3. **SIDRA download** (Claude): values API `apisidra.ibge.gov.br/values` with codes only (`/f/c/h/y`), metadata
   from `servicodados.ibge.gov.br/api/v3/agregados/<t>/metadados`. SIDRA refuses >50,000 values per request
   ("excedeu o limite"), so requests are cut by category (≤8 combinations × 5,600 municipalities). The v3
   values API answers 500 for several variables at once and is no smaller, so it is not used. Responses of a
   table-year are stored as one reproducible gzip CSV (`SRC/SIDRA/values/T<t>_<year>_N6.csv.gz`, codes only;
   names are in `SRC/SIDRA/meta/T<t>.json`) + manifest.

4. **SIDRA lists today's 5,570 municipalities for every year** (Claude, observed): municipalities not yet
   installed have `...`. T1 rows = municipalities of the DTB edition of the year (2000 → 5,507, 2008 → 5,564,
   2017 → 5,570; editions mapped in the config). All DTB codes of those editions are in SIDRA; the extra SIDRA
   codes are dropped (CONSISTENCY row only if one carried a value). Some 2008 tables list the 5,564 municipalities
   of 2008 directly.

5. **SIDRA symbols** (Claude, project flags of README): `-` (zero) → 0, `X` (hidden) → −1, `..` (not
   applicable) and `...` (not available) → −9999. Integer columns stay integers, decimals are kept.

6. **SIDRA column names** (Claude): `v<variable>` + `_<category>` per classification in SIDRA order
   (`v10676_48278`). Short, stable (SIDRA ids), unique within a file; the generated VARIABLES catalog gives
   variable, unit, classification and category names. Wide per table (not one wide municipal table) because
   3,364 columns for 2017 exceed the SQLite/GeoPackage limit of 2,000 columns and tables group related columns.
   Undo/alternative: a long table (municipio_code, table, variable, categories, value) in T2.

7. **Municipal "count" variables** (Claude, observed): at N6 the "Municípios com …" variables are 1/0 dummies
   for the municipality (the assembler warns if not). Kept as published.

8. **Atlas harvest** (Claude): `download_atlas.py` gets attributes (WFS CSV without geometry), schema, legend
   JSON, ISO record and capabilities. No geometry: every level joins to DTB (municipalities, states,
   intermediate regions) or ISO3. The endpoint is `geoserverCGEO/CGEO/wfs` (the one in the capabilities):
   `geoserver/CGEO/ows` and `geoserver/ows` answered "details omitted" exceptions at random; all requests are
   retried. The CSV `FID` column (a per-request id, different at every call) is dropped so that files are
   reproducible.

9. **Atlas T1 layout** (Claude): one wide file per level — `ATLAS-MUNICIPIOS_BRASIL_2017` (2017 base, DTB 2017),
   `ATLAS-MUNICIPIOS_BRASIL_2008` (PNSB 2008 maps on the 2008 base, DTB 2008), `ATLAS-UNIDADES`,
   `ATLAS-REGIOESINTERMEDIARIAS`, `ATLAS-PAISES` (`pais_code` = ISO3, `pais_name` kept) — columns `as<code>` as
   IBGE named them. Layers without a level (REGIC flows and cities, hydrographic regions, keyless) are written
   as published, one file each (`ATLAS-<code>_BRASIL_2017`). The year in the names is the atlas reference year
   (2017); the period of each map is in VARIABLES.

10. **Atlas merges** (Claude, from the assessment):
    - same column in several layers (population-size pieces 0113/0115/0125 CC1–4, state pieces 0122CC2/CC3) →
      union; values must agree where pieces overlap (checked: 1,458 / 268 / 42 overlapping municipalities, all
      agree); the size class is cut from the title of the merged column (this also removes the wrong "rede de
      abastecimento" title of 0125CC2). The 0514 pieces are separate columns (`as051401`, `as051403`…), kept;
    - complementary maps combined: `as010201` ← `as010401` (water network yes / no), `as010301` ← `as010403`
      (sewer network) — checked not overlapping; 17 municipalities are in neither sewer map (left NA);
    - duplicates dropped after an identity check: `as010302` = `as010402` = `as010202` (density);
    - the lagoons 4300001/4300002 dropped (CONSISTENCY);
    - `title_fixes` (config, logged) is available for wrong titles; none needed so far;
    - countries: the map repeats rows per polygon (10 identical rows per layer, dropped, logged); ISO3 codes still
      repeated (`-99` disputed areas, `CHI` Guernsey/Jersey) get `|name` (`-99|Aksai Chin`);
    - layer 041304 (REGIC cities) has a state code per city: levels need unique codes, so it stays "as published".

11. **Atlas values as published** (Claude): numeric when every value is a number (empty → −9999), else text
    (IBGE's category, empty → NA). No rescaling of shares (0–1 vs 0–100, flagged in VARIABLES `scale`). Empty
    has layer-specific meanings (no service / no answer / not applicable): it becomes NA/−9999 and the meaning,
    when the legend gives it, is in CATEGORIES. A municipality absent from a map has no value (NA/−9999).

12. **Catalogs** (Claude): generated into T1 by the assemblers (not hand-kept):
    `IBGE_ATLASSAN_VARIABLES_X_X.csv` (every column of every file: layer, year, column, SIDRA table/variable,
    names, unit, classifications, categories, source, period, value_type, scale, source layers, ISO title/date),
    `IBGE_ATLASSAN_TABLES_X_X.csv` (every PNSB file: SIDRA table, name, subject, rows, columns, dropped),
    `IBGE_ATLASSAN_CATEGORIES_X_X.csv` (every legend rule of the maps: category code or class break → label),
    `IBGE_ATLASSAN_CONSISTENCY_X_X.csv`. Hand-kept in this folder: `IBGE_ATLASSAN_LAYERS_X_X.csv` (CRLF) and
    `IBGE_ATLASSAN_FIELDS_X_X.csv` (LF), one row per file family (`pnsb`, `atlas-*`), since per-table texts are
    in TABLES/VARIABLES. LAYERS follows CLAUDE.md (CRLF); note the POP and DTB LAYERS copies are LF.

13. **SIDRA tables of a sub-universe** (Claude, observed): 34 table-years list only part of the municipalities
    (e.g. 5,548 = the municipalities with a water network). The others get −9999 (no data, not zero) and one
    CONSISTENCY row per file lists them (`not_in_sidra`).

14. **Scratch run** (Claude, 2026-10-26 22:10–23:16): SIDRA 304 table-years (2017: 63, 2008: 98, 2000: 143),
    1,300 requests, 0 errors, 104 MB gzip; GeoServer 194 layers, 9 without JSON legend (composites), harvest
    reproducible (same sha256 on a second run). T1: 304 PNSB files + 10 ATLAS files + 6 catalogs, 151 MB;
    VARIABLES 7,548 rows (7,317 PNSB, 231 atlas). Cross-check: SIDRA T7460 "with water network" = 5,548 =
    atlas map 010201 "Sim", municipality by municipality; T7461 sewer = 3,359 = map 010301 "Sim". Every PNSB file
    has the rows of its DTB edition, unique codes, no empty cell; every T1 column is described in FIELDS.

## Open
- O1. Map ↔ SIDRA correspondence (which atlas map repeats which SIDRA column) — manual, for the VARIABLES
  catalog; would allow dropping the redundant atlas columns.
- O2. English descriptions of the SIDRA tables and variables (VARIABLES/TABLES are Portuguese only).
- O3. T2: one GeoPackage of attribute tables (`pnsb_<table>_<year>`, `atlas_<level>`) + catalogs, as POP.
- O4. Labels of coded atlas categories that the legend repeats as codes (`caraImoAA`, `consenergAAeES`…):
  from the PDF.
- O5. PNSB 2000/2008 tables at municipal level carry the territory of those years: join with DTB editions
  2000/2008 (done by the rows), not with the 2017 mesh.
- O6. Other PNSB subjects (drainage, solid waste in 2000/2008) come with the download: keep (same source) or
  restrict to water and sewage like the atlas?

## 2026-10-27

15. **PNSB split off** (user: "PNSB is a separate entry in the warehouse. ATLASSAN is another, with duplicated
    data, standalone product, not an extension"). The SIDRA items above (§2 first part, 3–7, 13) now belong to the
    PNSB datasets `PNSB-<year>` (`pnsb/DECISIONS.md`); ATLASSAN keeps only the Atlas maps (§8–11).
    - Files without the family prefix: `IBGE_ATLASSAN_<LEVEL>_BRASIL_<year>.csv` and `IBGE_ATLASSAN_<code>_BRASIL_
      2017.csv`; layers `municipios`, `unidades`, `regioesintermediarias`, `paises`, `<code>` (Claude). The year in
      the names stays the data reference year (2017; 2008 for the maps on the 2008 base), not the edition 2021
      (Claude; the edition is in the dataset description and the layer prefix `AtlasSan2021_`).
    - VARIABLES lose the SIDRA fields (table_code, variable_code, unit, classifications, categories, source,
      description_pt): layer;year;column;variable_name;period;value_type;scale;source_layers;metadata_title;
      metadata_date. No TABLES catalog. LAYERS 10 rows, FIELDS 41.
    - The values are identical to the night run (10 files compared cell by cell).
    - The night `SRC/SIDRA` and T1 were moved (not deleted) to `data/_TRASH_20261027/ATLASSAN_V1/` (255 MB); the
      SRC was split into `data/PNSB-<year>/V1/SRC/SIDRA`.
    - O1 (map ↔ SIDRA) becomes a link between two datasets; O2/O5/O6 above move to `pnsb/DECISIONS.md`.

16. **Chapters 2 and 3 found** (user spotted `CGEO:atlassan2021_020704`). The GeoServer publishes chapters 2 and 3
    with a lowercase prefix (`atlassan2021_`, 230 layers: 129 of chapter 2, 101 of chapter 3) and the other
    chapters as `AtlasSan2021_` (194). The harvest matched the prefix case-sensitively and missed them; the
    "chapter 2 is not on the GeoServer" finding of the assessment was wrong. Fix: prefix matched ignoring case
    (`download_atlas.py`); 424 layers harvested, 0 failures, 6 without JSON legend (centroid / composite layers).
    - Levels: new key columns added to the config: `geocodigo_uf` (UNIDADES), `geocodigo` with 4 digits
      (REGIOESINTERMEDIARIAS). Result: MUNICIPIOS 2017 296 value columns (+110 of chapter 2, +89 of chapter 3),
      UNIDADES 105 (+21), REGIOESINTERMEDIARIAS 26 (+3). The old columns are unchanged (compared). The centroid
      layers (`…cc1centroi`) repeat the values of the polygon layers: unioned with the agreement check.
    - As published (no unique DTB code), 12 more loose files: census-tract density 2010 (030201 and
      03_setor_centroide, same values, 316,574 rows of which 92 without code; 62 MB, census 2010 material),
      water intake / treatment points (030501/02, 030601/02, 030801: 12-digit locality codes, several points per
      code), ocean outfalls (032601), river basin committees (033501–033503, 03_cbhs_estaduais = 033501).
    - T1 now 22 data files + catalogs, 87 MB; VARIABLES 466 rows, CATEGORIES 2,110, CONSISTENCY 279.
    - Open: drop the two census-tract layers (repeat CENSO-2010, 72 MB) and the duplicate 03_cbhs_estaduais?

17. **Repeated layers dropped** (user): config `skip_layers` (logged in CONSISTENCY `skipped_layer`): 030201 and
    03_setor_centroide (2010 census-tract density, the same values, already in CENSO-2010; 72 MB) and
    03_cbhs_estaduais (byte-identical to 033501). T1 19 data files, 17 MB.
18. **Assessment of missing parts** (2026-10-27, after chapters 2–3): nothing critical.
    - All five chapters are in; map numbers run without gaps in chapters 2–5. Chapter 1 numbers 01, 07, 17–20,
      23 have no layer; the PDF contents list plates with charts and tables between maps, so these are taken as
      non-map plates (not verified one by one).
    - 22 municipal columns are published only as class labels ("0,01 a 10,00"), not values (e.g. as020701,
      as021301): kept as text; the values are in the PNSB tables where SIDRA has them (O1).
    - Cross-check chapter 2: map 020201 (ETAs in operation 2017) = PNSB T7482 v10888_48514 in 5,567 of 5,570
      municipalities (sums 4,962 / 4,959; the map has 1 ETA in Marcelino Vieira, Rafael Fernandes, Puxinanã
      where SIDRA has 0 — likely ETAs serving several municipalities). Not corrected (values as published).
    - 6 layers without a JSON legend (centroids / composites): no CATEGORIES rows for them.
19. **T2** (Claude): `censos/build_database_tables.py` (shared with PNSB), `IBGE_ATLASSAN_BD_BRASIL_2017.gpkg`
    (25 MB, no geometry): tables `atlas_<level|code>_<year>` (lowercase; prefix so that no name starts with a
    digit), catalogs, layers. Unique index on the level code; the loose tables have no key. All tables equal
    to T1.

20. **Chapter first, then level** (user: "these files must follow a chapter split as a major hierarchy then a
    level split"). Files `IBGE_ATLASSAN_CAP<chapter>-<LEVEL>_BRASIL_<year>.csv` and, for the maps without a level,
    `IBGE_ATLASSAN_CAP<chapter>-<code>_BRASIL_<year>.csv`; chapter = first two digits of the map code. Layers
    `cap<chapter>-<level|code>` (VARIABLES, CONSISTENCY); T2 tables `cap<chapter>_<level|code>_<year>`.
    26 data files: cap01 municipios/unidades; cap02 municipios/unidades/regioesintermediarias; cap03 municipios +
    10 loose; cap04 municipios/regioesintermediarias/paises + 4 loose; cap05 municipios (2017 and 2008)/unidades.
    Every chapter file of a level has the same rows and key columns. LAYERS: one row per chapter and level, with
    the chapter titles of the publication (themes saneamento, meioambiente, saude, gestao), plus
    `cap03-<código>` / `cap04-<código>` for the loose maps. Checked: the chapter files of a level put together
    equal the previous level files (19 of 19). Previous files moved to `data/_TRASH_20261027/ATLASSAN_V1/
    T1_before_chapters/`.

21. **Plotting geography from the DTB, in T1 and T2** (user: "all datasets may need some spatial layers as auxiliar
    plotting assets. take from DTB. Both T1 and T2"; design Claude). `censos/geography_from_dtb_editions.py`
    (shared by PNSB and ATLASSAN, configs `geography_<dataset>`): per DTB edition used by the tables,
    `GEO-MUNICIPIOS`, `GEO-SEDES`, `GEO-UNIDADES`, `GEO-PAIS` (+ `GEO-REGIOESINTERMEDIARIAS` for 2017) as T1 gpkgs;
    the T2 builder copies them with geometry (`geo_<name>`_<year>; 2008: municipios and sedes).
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
- (2026-10-27, user) Geography layer names follow the DTB: `ibge_dtb_<name>_<edition>` (e.g. `ibge_dtb_municipios_2008`)
  inside the T1 gpkgs, as T2 tables and as LAYERS names; the file names stay under the dataset
  (`IBGE_<DATASET>_GEO-<NAME>_BRASIL_<year>.gpkg`), since the layers are simplified derivatives, not DTB files.

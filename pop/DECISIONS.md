# POP T1 — critical judgements (2026-10-24)

Source: `<work root>/POP/V1/SRC` (estimates 1992–2025) and `<work root>/DTB/V1/SRC/dtb_evolucao_pop_1872-2010.csv` (census
populations). Output: `<work root>/POP/V1/T1/ASSETS`. Run after the DTB tables (needs the T1 quadro and genealogia):
`python assemble_population.py --config assemble_population.local.json --overwrite` (~30 s).

## Judgements
- **Census series 1872–2010 come from the IBGE genealogy table** (same publication as the DTB historical meshes), so
  `municipio_code` of each census year matches the DTB municipios snapshot of that year — POP and DTB join by (year, code).
- `-` = 0 (IBGE convention); `...`/`…`/empty = -9999. 1960 values are the Sinopse preliminar (IBGE note).
- **2010 column is corrupted in the source** (thousand dots read as decimals: `24.392` = 24,392, `13.97` = 13,970, `815` = 815).
  Each value: dot → ×1000; no dot → literal or ×1000 (both readings for every value; ×1000 tried first below 3
  digits), choosing total = urban + rural. Result: national total **190,755,799 = published exactly**; every
  municipality has total = urban + rural (fixed 2026-10-26: 10 small rural counts such as `43` had only the
  ×1000 reading and were stored as 43,000; 2010 rural total 30,377,242 → 29,829,995). 1991 and 2000 totals also match exactly.
- 1872/1900/1920 totals are as typed by IBGE (they warn of transcription errors); not checked against published totals.
- **Estimates 1975 and 1989 PDFs excluded**: scanned images with unusable OCR.
- Years without estimate: 1996 (count, not in SRC), 2007 (count), 2010 and 2022 (censuses), 2023 — 2007/2010/2022/2023
  are not on the IBGE site (user, 2026-10-24). 2026 added (`estimativa_dou_2026.ods`, 5,571, sum = published Brasil).
- **Census 2022** (user decision): copy of the census T1 tract table `SRC/IBGE_CENSO-2022_BASICO_BRASIL_2022.csv`
  (from `<IBGE root>/CENSO-2022/V300/T1/ASSETS`). Municipal population = sum of V0001 over tracts; urban /
  rural by `situacao_type` (Urbana 1–3, Rural 5–8; the 1,103 tracts without situation have 0 residents). Total
  203,080,756 = published; urban 177,508,417, rural 25,572,339. 2 tracts without municipio code (0 residents) dropped.
  Codes = DTB 2022 quadro (5,570).
- **Codes without check digit (1992–2006 files)**: 7-digit code taken from the DTB (codes since 1991 with the same 6
  leading digits; older Mato Grosso codes collide with current ones); every code then found in the DTB editions around
  the year, except Boa Esperança do Norte (MT, 2025, after DTB 2022).
- Footnote marks are separated from values/names (`note_mark`) and the footnotes kept verbatim in `estimativas-notas`.
  **The published estimate stays in `population`** even where a judicial population exists (in the notes).
- Published Brasil/region/UF totals kept in `estimativas-unidades`; 6 UF totals differ from the municipal sums
  (judicial populations, PI/CE and AL/PE differences explained in the notes).
- Municipality names kept as published (256 spelling differences vs DTB logged).
- No POP geopackage: population is attribute data; join to the DTB municipios layer of the year by code.
- **One file per layer and year** (user, 2026-10-26): `IBGE_POP_<LAYER>_BRASIL_<year>.csv` (the `year` column is
  kept, so the files concatenate), so a new estimate or census updates T1 by adding its own files:
  add it to the config and run `assemble_population.py --years <year>` (other files and CONSISTENCY rows untouched).
  `pop/IBGE_POP_LAYERS_X_X.csv` is the hand-kept catalog (the T1 copy had unquoted ';' in the censos row).

## Visual checks (`check_visuals.py`, figures in `pop/visual_checks`)
- Census sums match published totals; estimates follow the censuses. Steps in the estimate series are IBGE re-basings
  (2000 census, 2007 count → 2008, 2010 census, 2013 projection revision, 2022 census → 2024), not parsing errors.
- 1992 estimate vs census 1991: 217 municipalities differ > 25 %; 209 of them lose territory in the DTB lineage 1991→2000
  (1993 installations) → POP and DTB agree.
- Growth outliers (> 15 %/yr) listed in `visual_checks/growth_outliers.csv`.
- 2022 census (added later): the 2021 estimate (213.3 M) is 10.2 M above the census (203.1 M) → 2021→22 has 908 municipalities
  with |growth| > 15 %/yr and 379 with 2021 estimate vs census > 25 %; 2024 estimate sits on the census 2022 (0 > 25 %). These are
  the IBGE overestimation of the 2010s, not parsing errors.

## Package and T2 (2026-10-26, user)
- **Package** (`censos/package_t1.py --config package_t1_pop.local.json`, run from `censos/`): one reproducible zip per
  layer with all its year files (`IBGE_POP_<LAYER>_BRASIL_<first>-<last>.zip`: censos, estimativas-municipios,
  estimativas-unidades, estimativas-notas) plus the catalogs as they are, manifest and SHA256SUMS, into
  `POP/V1/SRC/ZENODO`. A new year renames the zip of its layer (log: "replaces", remove the old one from the deposit).
- **T2** (`pop/build_database_pop.py`): one GeoPackage of attribute tables, minimal tables: `municipios` (censuses +
  estimates + interpolated years in one long table, flag column `source`, `series` = 1 for the annual series row,
  `interpolation` documents each interpolated value), `unidades`, `notas`, and the catalogs.
- **Interpolation** of the estimate years without estimate (1996, 2007, 2023; config), geometric between the nearest
  series years, on the territory of the earlier year: `interpolado` (same code), `interpolado_territorio` (parent of
  municipalities created between the anchors: later value + children), `interpolado_rateio` (children of several
  parents: group interpolated, shared by the earlier population). Parents from the DTB genealogia (reference codes
  resolved to the trajectory code, e.g. Araguacema 5201900 -> 1701903), else the linhagem. 1996: 4,974 municipalities
  of 1995 (533 created in 1997): 4,494 / 344 / 136; 2007: Teresina + Nazária; 2023: 5,570 plain.
  Not interpolated: urban/rural (-9999 in interpolated rows), unidades (published totals only), years before 1991.

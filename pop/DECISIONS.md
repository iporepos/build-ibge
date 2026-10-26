# POP T1 — critical judgements (2026-10-24)

Source: `<work root>/POP/V1/SRC` (estimates 1992–2025) and `<work root>/DTB/V1/SRC/dtb_evolucao_pop_1872-2010.csv` (census
populations). Output: `<work root>/POP/V1/T1/ASSETS`. Run after the DTB tables (needs the T1 quadro and genealogia):
`python assemble_population.py --config assemble_population.local.json --overwrite` (~30 s).

## Judgements
- **Census series 1872–2010 come from the IBGE genealogy table** (same publication as the DTB historical meshes), so
  `municipio_code` of each census year matches the DTB municipios snapshot of that year — POP and DTB join by (year, code).
- `-` = 0 (IBGE convention); `...`/`…`/empty = -9999. 1960 values are the Sinopse preliminar (IBGE note).
- **2010 column is corrupted in the source** (thousand dots read as decimals: `24.392` = 24,392, `13.97` = 13,970, `815` = 815).
  Each value: dot → ×1000; no dot → literal or ×1000, choosing total = urban + rural. Result: national total
  **190,755,799 = published exactly**; 10 municipalities without a consistent reading (logged). 1991 and 2000 totals also match exactly.
- 1872/1900/1920 totals are as typed by IBGE (they warn of transcription errors); not checked against published totals.
- **Estimates 1975 and 1989 PDFs excluded**: scanned images with unusable OCR.
- Years without estimate: 1996 (count, not in SRC), 2007 (count), 2010 and 2022 (censuses), 2023 — 2007/2010/2022/2023
  are not on the IBGE site (user, 2026-10-24). 2026 added (`estimativa_dou_2026.ods`, 5,571, sum = published Brasil).
- **Census 2022** (user decision): copy of the census T1 tract table `SRC/IBGE_CENSO-2022_BASICO_BRASIL_2022.csv`
  (from `<IBGE root>/CENSO-2022/V200/T1/ASSETS`). Municipal population = sum of V0001 over tracts; urban /
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

## Visual checks (`check_visuals.py`, figures in `pop/visual_checks`)
- Census sums match published totals; estimates follow the censuses. Steps in the estimate series are IBGE re-basings
  (2000 census, 2007 count → 2008, 2010 census, 2013 projection revision, 2022 census → 2024), not parsing errors.
- 1992 estimate vs census 1991: 217 municipalities differ > 25 %; 209 of them lose territory in the DTB lineage 1991→2000
  (1993 installations) → POP and DTB agree.
- Growth outliers (> 15 %/yr) listed in `visual_checks/growth_outliers.csv`.
- 2022 census (added later): the 2021 estimate (213.3 M) is 10.2 M above the census (203.1 M) → 2021→22 has 908 municipalities
  with |growth| > 15 %/yr and 379 with 2021 estimate vs census > 25 %; 2024 estimate sits on the census 2022 (0 > 25 %). These are
  the IBGE overestimation of the 2010s, not parsing errors.

# DTB + POP T1 — status and missing pieces (2026-10-24)

Code: `dtb/` and `pop/` (every script `--config <json>`). Data: `<work root>/DTB/V1`, `<work root>/POP/V1` (SRC → T1/ASSETS).
Judgements: `dtb/DECISIONS.md`, `pop/DECISIONS.md`. Findings: `*_CONSISTENCY_X_X.csv` in each T1.
Nothing was written to the warehouse; promotion/backup is on you.

## Rebuild order
```
cd dtb && python assemble_tables.py    --config assemble_tables.local.json    --overwrite   # ~1 min
          python assemble_geography.py --config assemble_geography.local.json --overwrite   # ~1.5 min
          python build_lineage.py      --config build_lineage.local.json      --overwrite   # ~1.5 min
          python plot_lineage.py       --config plot_lineage.local.json
          python check_visuals.py      --config check_visuals.local.json
cd pop && python assemble_population.py --config assemble_population.local.json --overwrite # ~30 s, needs DTB tables
          python check_visuals.py       --config check_visuals.local.json
```

## Status

### DTB T1 (`<work root>/DTB/V1/T1/ASSETS`)
| Layer | Years | Notes |
|---|---|---|
| municipios (gpkg) | 1872, 1900, 1911, 1920, 1933, 1940, 1950, 1960, 1970, 1980, 1991, 2000, 2010, 2022 | 1872–1991 = IBGE "Evolução" base (nested); 2000/2010/2022 independent bases |
| sedes (gpkg) | 1872–2000 (12 snapshots) | **no 2010, no 2022** (not in SRC) |
| unidades (gpkg) | all 14 snapshots | 2000/2010 dissolved from municipios |
| pais, regioes, meso/micro, intermediárias/imediatas (gpkg) | 2022 only | 2000/2010 meso/micro meshes in SRC not converted |
| quadro-municipios (csv) | editions 1940–1980, 1994, 2000, 2003–2022 | no editions 1991, 1995–1999, 2001–2002, 2023+ |
| quadro-distritos / -subdistritos (csv) | 1994–2022 | tables only, no geometry |
| genealogia (csv) | 1872–2010 | 5,522/5,549 note references resolved |
| linhagem (csv) | 1872→…→2022 (41,792 edges) | the splitting graph; plots in `dtb/lineage_report/plots` |

### POP T1 (`<work root>/POP/V1/T1/ASSETS`)
| Layer | Years | Notes |
|---|---|---|
| censos-municipios | 1872, 1900, 1920, 1940–1980, 1991, 2000, 2010, 2022 | 1991/2000/2010/2022 totals = published; 2022 from census T1 tracts |
| estimativas-municipios | 1992–2026 | missing 1996, 2007, 2010, 2022, 2023 (2007/2010/2022/2023 do not exist on the IBGE site) |
| estimativas-unidades / -notas | 1992–2026 | published totals and footnotes |

## Missing pieces / actions (yours = needs a file or a decision; mine = code only)

### DTB — high value
1. **Sedes 2022** (yours: copy) — the census 2022 T1 has `IBGE_CENSO-2022_SEDES_BRASIL_2022.gpkg` on the warehouse
   (`CENSO-2022/V200/T1/ASSETS`). Copy it to `<work root>/DTB/V1/SRC/` and I add it as the 2022 seats (codes = DTB 2022).
2. **Sedes 2010** (yours: download) — IBGE "Localidades 2010" (`BR_Localidades_2010_v1`, point layer with
   `NM_CATEGORIA = CIDADE` = municipal seats), on the IBGE geociências FTP (organizacao_do_territorio /
   estrutura_territorial / localidades). The census 2010 T1 on the warehouse has no sedes layer.
3. **Recent meshes 2023–2025** (yours: download "Malha Municipal" 2024 or 2025 + DTB 2024/2025) — closes the
   one open code, Boa Esperança do Norte (MT, 5101837, installed 2025), present in the 2025/2026 estimates
   but in no DTB edition here. Extends the lineage to 2022→2025.
4. **Áreas mínimas comparáveis (AMC)** (mine) — connected components of the linhagem for a chosen start year
   (e.g. 1872, 1940, 1970, 1991, 2000). Gives the stable units for long population series; it is the main
   bridge between DTB and POP. Needs one decision from you: which start years, and whether `same_base = 0`
   transfers (cartographic noise between 1991/2000/2010/2022) should merge units (my default: no).

### DTB — quality
5. Visual QA of the **2000→2010 and 2010→2022 transfers** (847 transfer edges, many cartographic). Mine; a
   per-interval zoom figure in `check_visuals.py`.
6. **Seats with problems** (logged): 3 outside their polygon in 2000 (1200435, 2603603, 2702108); 6 codes with
   several points in 1991 (one kept). Yours to judge if any correction source exists; otherwise keep.
7. **2000 mesh datum**: no .prj, labelled EPSG:4674 without transformation (likely SAD69, ~60 m). If the
   original 2000 malha with .prj is somewhere, swap it in.
8. **Genealogy**: 20 unresolved + 2 ambiguous note references (`genealogia` rows in CONSISTENCY). Manual
   lookup table in the config would close them (mine, ~1 h, but needs your OK on spellings).
9. **Mesh 2010 hole** in Maciço de Baturité (CE, 309 km² in no municipality) — kept as hole. Could be filled
   from the 2022 mesh if you prefer a full partition (decision).
10. Quadro gaps: editions 1991, 1995–1999, 2001–2002 are not in SRC. If IBGE has them (DTB 1997 is used as
    2000), drop them in SRC/DTB_<year> and add them to `assemble_tables.json`.
11. 2000/2010 meso/microregion meshes in SRC (per UF) not converted; can be dissolved from municipios +
    quadro codes (mine, small).
12. Distritos/subdistritos geometry: only tables. The census 2022 T1 has DISTRITOS/SUBDISTRITOS gpkg
    (copy, yours) — 2022 only; historic district geometry does not exist in SRC.

### POP — high value
13. **Harmonized population on AMC** (mine, after item 4): census 1872–2022 + estimates summed by AMC, giving
    comparable series and growth rates without split artefacts (the 1995→97, 2000→01 outlier peaks).
14. **Census 2010 ambiguous values** (10 municipalities where the corrupted source has no consistent
    reading): fix from the census 2010 T1 on the warehouse (`IBGE_CENSO-2010_BASICO_BRASIL_2010.csv`, tract sums,
    like 2022). Copy it to `<work root>/POP/V1/SRC/` (yours) and I use it for 2010 (the genealogy totals already
    match, so this is a cross-check plus the 10 fixes).
15. **Contagem 1996** (yours, if found: SIDRA or IBGE FTP "Contagem_da_Populacao/1996") — the only missing
    estimate-era year that exists as a source. Would go into a separate layer `contagens-municipios`.

### POP — quality
16. Early census totals 1872–1980 not checked against published totals (IBGE "Estatísticas do Século XX" /
    Anuário). Add them to `census_totals` in the config if you have the numbers.
17. 1960 = Sinopse preliminar (as in IBGE's table). Final 1960 municipal results would replace them if found.
18. Estimates 1975 and 1989 PDFs: scanned, unusable OCR. Only with a proper OCR pass or another source.
19. Judicial populations live only in `estimativas-notas` (text). A `population_judicial` column parsed from
    the notes is possible (mine) if you need it.
20. Growth outliers (`pop/visual_checks/growth_outliers.csv`, 2,913) are explained by splits, re-basings and
    the 2021→2022 census correction; no action unless the AMC series still shows them.

### Both — delivery
21. Promotion of `<work root>/DTB/V1` and `<work root>/POP/V1` to the warehouse (yours) and, if wanted, packaging/Zenodo pages like
    the census projects (`censos/package_t1.py` configs + zenodo html) — mine to draft.
22. T2 (single gpkg per dataset, as in the census projects) — not started; natural after AMC.

## Suggested order
1 → 2 → 14 (copies) → 4 → 13 → 5 → 3 → the rest as needed.

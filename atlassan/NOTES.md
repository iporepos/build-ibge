# Atlas de Saneamento 2021 (ATLASSAN) — assessment (2026-10-26)

Status: built (`download_atlas.py`, `assemble_atlas.py`; the PNSB part moved to `pnsb/` on 2026-10-27; chapters 2–3 are on the GeoServer as `atlassan2021_*`, see DECISIONS §16 — statements below that chapter 2 is absent are wrong); the
decisions, including the switch to SIDRA PNSB as the main source, are in `DECISIONS.md`.

Source: IBGE, *Atlas de Saneamento: abastecimento de água e esgotamento sanitário 2021* (2021-11-18;
https://biblioteca.ibge.gov.br/index.php/biblioteca-catalogo?view=detalhes&id=2101885). Reference year 2017;
main tabular source PNSB 2017 (Pesquisa Nacional de Saneamento Básico), geometry from the IBGE Base Territorial
2017. Work folder `data/ATLASSAN/V1` (SRC only; T1 empty).

## What is in SRC
- `liv101885_completo.pdf` — the full publication (195 pages, 5 chapters + maps).
- `_LEGACY/` — 40 CSVs exported by hand from the IBGE GeoServer (8 of chapter 1, 32 of chapter 4), a list of
  WMS GetMap URLs of chapter 1 and a README with titles/abstracts of the chapter 4 maps. Superseded by a
  scripted harvest (below): the server has 194 layers.

## The GeoServer (the real source of the data)
`https://geoservicos.ibge.gov.br/geoserver`, workspace `CGEO`, layers `AtlasSan2021_<code>`, WFS + WMS.
Per layer: attributes (WFS GetFeature, outputFormat=csv), schema (DescribeFeatureType), legend with class
labels and filters (GetLegendGraphic, format=application/json; 185 of 194), title (capabilities) and an ISO
19115 record at INDE (MetadataURL, almost every layer: title, date, abstract, lineage).
Layer code `CCSSNN` = chapter, section, map; the value column is `as<code>`. `…CCn` suffix = a composite map
(several pieces or several value columns).

| Level | Layers | Key | Rows |
|---|---|---|---|
| municipality (2017 base) | ~110 | `geocodigo` / `codmunic` / `cd_geocmu` (7 digits) | 5,570 (+ lagoons 4300001/2 → 5,572) or a subset |
| municipality (2008 base, PNSB 2008) | 4 | `geocodig_m` | 5,564 (= DTB edition 2008 exactly) |
| state | 41 | `geocodigo` / `codigo` / `cod_uf` / `geoc_uf` (2 digits) | 27 |
| intermediate region | 23 | `rgint_cod` | 133 (= DTB 2019/2022 codes) |
| country (international comparisons) | 11 | `iso_a3` | 267 |
| hydrographic region | 2 | `rhi_nm` / none | 12 |
| REGIC flows / cities (health travel 2018) | 2 | `cod_ori`/`cod_dest`, `cod_cidade` | 6,293 / 1,915 |
| other (no key) | 2 | — | 6 |

All municipal codes are unique per layer and are in the DTB 2017 list (plus the two lagoons). 183 layers have
one value column; 11 have 2–8 (composites, e.g. `0508CC1` staff by contract type, `0301CC1` hydrographic areas).

## Problems found
1. **Coverage gap**: chapters by layer code: 01 = 52, 03 = 1, 04 = 42, 05 = 99, **02 = 0**. Chapter 2 "Qualidade
   e eficiência das redes" (pp. 37–90: ETA types, water quality, interruption, rationing, losses, sewage generated
   and treated) and most of chapter 3 (pp. 91–) are **only in the PDF**, as maps (classes, not values).
   Likely source of their municipal values: the PNSB 2017 tables (SIDRA) — to check.
2. **One variable split over several layers**:
   - complementary maps: `010201` (with water network, 5,548 "Sim") + `010401` (without, 22: Não / Não aplicável /
     Não respondeu) = 5,570; `010301` + `010403` = 5,553 (17 municipalities in neither).
   - population-size pieces `0113CC1–4`, `0115CC1–4`, `0125CC1–4`: the pieces overlap (`CC1` alone has all 1,768
     municipalities with data), values never conflict → take the union. The `0125` titles say "rede de
     abastecimento" for some pieces while the column is the sewer network (`as012502`): title error.
   - identical layers: `010202` = `010302` = `010402` (demographic density, same values).
3. **Values**: numbers stored as text; `''` mixed in with meanings that differ per layer (no service, no answer,
   not applicable, or the lagoons); `'0'` used as a category ("sem registro" in `052201`); category codes such as
   `caraImoAA`, `consenergAAeES`, `sem_tarif` (legend repeats the code for some → labels from the PDF);
   proportions as 0–1 in some layers (`0505`, `0511`, `0512`), percentages 0–100 in others (`010501`).
4. **Periods differ**: most 2017; PNSB 2008 layers (`0509x1/3`, `0507x1/3`); health 2008–2019 aggregates
   (chapter 4, municipal ones `0405–0408`, the rest by intermediate region); REGIC 2018/2019.
5. Abstracts are empty in the WMS capabilities; the ISO records carry them (and chapter-4 notes, e.g. the DRSAI
   disease list).

## Proposed design (superseded: implemented with SIDRA as the main source, see DECISIONS.md)
- **SRC**: `download_atlassan.py` harvests every `AtlasSan2021_*` layer: attributes CSV without geometry, schema,
  legend JSON, ISO record, capabilities; dated folder + manifest. No geometry (all levels join to DTB / POP).
- **T1** (CSV, `municipio_code` text 7 digits):
  - `IBGE_ATLASSAN_MUNICIPIOS_BRASIL_2017.csv` — one row per DTB-2017 municipality (5,570), one column per
    indicator (`as<code>`, IBGE's own code as the variable name); split layers merged into one column;
    duplicates dropped (one density column); lagoons dropped (logged).
  - `IBGE_ATLASSAN_MUNICIPIOS_BRASIL_2008.csv` — the PNSB 2008 columns (5,564 municipalities of 2008).
  - `IBGE_ATLASSAN_UNIDADES_BRASIL_2017.csv` (`unidade_code`), `..._REGIOESINTERMEDIARIAS_...` (`regiaointermediaria_code`),
    `..._PAISES_...` (`pais_code` = ISO3), `..._REGIOESHIDROGRAFICAS_...`, and the two REGIC tables (flows with
    `municipio_code` of origin/destination where the city is one municipality).
  - Values: numbers as numbers, `-9999` no data; categories as text labels; `''` mapped per layer to `NA` or a
    label ("Sem serviço", "Não respondeu"...) from the legend, the rule logged.
  - Catalogs: LAYERS, FIELDS, **VARIABLES** (code, table, title pt, abstract, period, unit, scale 0–1/0–100,
    source survey, original layer(s)), **CATEGORIES** (variable, code, label pt/en, from the legend/PDF),
    CONSISTENCY (merges, title errors, dropped rows).
- **T2**: one GeoPackage of attribute tables (as POP), joined to DTB by code.
- Open questions: include chapter 2/3 from PNSB/SIDRA (other source) or document the gap; dataset name
  (ATLASSAN vs the existing warehouse `SANEAMENTO/V1`, which holds the same legacy CSVs).

# build-ibge

Builders for an IBGE data warehouse: the scripts that download, assemble, harmonize, catalog, check and
package the IBGE censuses (1991, 2000, 2010, 2022), the DTB (Divisão Territorial Brasileira) and the
population series (POP), the national sanitation survey (PNSB, one dataset per year) and the Atlas de Saneamento (ATLASSAN).

The data are not in this repository. They live in a warehouse folder, one dataset per folder, in tiers:

```
<IBGE root>/CENSO-2010/V300/
    SRC/    raw downloads from IBGE (and the packaged files of a Zenodo deposit)
    T0/     raw assembled tables (only where the source needs it, e.g. 2010)
    T1/ASSETS/   harmonized tables (CSV), layers (GeoPackage) and catalogs: the published product
    T2/ASSETS/   one GeoPackage database built from T1
```

`V100`, `V200`... are versions of a dataset; DTB, POP, PNSB-<year> and ATLASSAN use `V100`.

## Layout

One folder per dataset, flat. Each script sits next to its configs.

| Folder | What it builds |
|---|---|
| `censos/` | Shared census tools: the T2 builder (`build_database.py`), the T2 builder of attribute-table datasets (`build_database_tables.py`: PNSB, ATLASSAN), their plotting geography from the DTB (`geography_from_dtb_editions.py`), the Zenodo packager (`package_t1.py`), state and country layers from the DTB (`geography_from_dtb.py`) and the sanity checks (`check_*.py`). Configs for every census year. |
| `censo_1991/` | Census 1991: sample microdata, tract universe tables (DVD), municipal aggregates, geography, comparability with 2022, tract inference for the microdata, catalogs and a dedicated T2 builder. |
| `censo_2000/` | Census 2000: tract tables, geography (rural + urban tracts, range polygons), VARIABLES catalog. |
| `censo_2010/` | Census 2010: download of the IBGE release, assembly (T0), harmonization (T1); 2010 district meshes. |
| `censo_2022/` | Census 2022: tract tables, geography, VARIABLES catalog, geometry clean-up. |
| `dtb/` | DTB: municipal tables 1940–2022, meshes/seats/states per snapshot 1872–2022, municipal lineage graph. |
| `pop/` | Population series: census municipal populations 1872–2022, estimates 1992–2026. |
| `pnsb/` | PNSB (Pesquisa Nacional de Saneamento Básico): SIDRA tables by municipality, one dataset per year (`PNSB-2000`, `PNSB-2008`, `PNSB-2017`), one config and one pair of catalogs per year. |
| `atlassan/` | Atlas de Saneamento 2021: the map values of the IBGE GeoServer, by municipality, state, intermediate region and country, joined to the DTB by code. |

Notes kept with the code:
- `MEMORY.md`: what was done, decisions, gotchas, open items (read this first).
- `<folder>/DECISIONS.md`: judgements taken on the sources (dtb, pop, pnsb, atlassan).
- `<folder>/NOTES.md`: working plans and status from the bench (censos, censo_1991, dtb), the ATLASSAN source assessment.
- `<folder>/IBGE_*_LAYERS_X_X.csv`, `IBGE_*_FIELDS_X_X.csv`: the hand-kept catalogs (the source copies; the user copies them into T1).
- `<folder>/zenodo_<year>.html` (`pop/zenodo_pop.html`, `dtb/zenodo_dtb.html`): the description of the Zenodo deposit.

## Configs

Every script takes `--config <file.json>`; no path or parameter is hardcoded. Each config comes in up to three forms:

| File | Use |
|---|---|
| `<name>.example.json` | Same keys and values, data paths under `/path/to/IBGE` (or `/path/to/outputs`). Start here on a new machine. |
| `<name>.local.json` | The configuration of the warehouse machine, with its absolute data paths. |
| `<name>.zenodo.json` | (`build_database_<year>`) the config shipped in a Zenodo deposit as `build_database_<year>.json`, with relative paths, run from the download folder. |

Paths to files of this repository (spec JSONs, glossaries, report folders) are relative: **run each script from
its own folder**. Report folders (`*_report`, `visual_checks`) are created next to the script.

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cd censo_2010
../.venv/bin/python assemble_2010.py --config assemble_2010.local.json
```

Shared tools take the config of the year they work on, from `censos/`:

```
cd censos
../.venv/bin/python build_database.py --config build_database_2010.local.json --overwrite
```

System tools: GDAL command line (`ogr2ogr`), poppler (`pdftotext`, census 2000 VARIABLES) and Graphviz (`dot`, DTB plots).

## Conventions

- File names: `IBGE_<DATASET>_<NAME>_<AREA>_<PERIOD>.<ext>`, e.g. `IBGE_CENSO-2010_BASICO_BRASIL_2010.csv`;
  GeoPackage layer `ibge_censo_2010_basico`. Names have no underscores (the underscore is the delimiter);
  catalogs are `..._X_X.csv`.
- Tract key `setor_code` (15 digits, text). Columns `<entity>_code` / `<entity>_name`
  (`unidade` = state, `municipio`, `distrito`, `regiaointermediaria`...). Variables `V<number>`.
- Flags in numeric columns: `-9999` no data, `-1` hidden by IBGE (confidentiality), `-2` uncertain. Text no data: `NA`.
- Catalogs in every T1: `LAYERS` (one row per layer and theme), `FIELDS` (non-V columns), `VARIABLES`
  (V columns: table, code, name, pt/en descriptions); 1991 also `CATEGORIES`, several have `CONSISTENCY`
  (every fix applied to the source, one row each).
- Scripts write `<name>.part` and rename when complete, never overwrite without `--overwrite`, and log to
  `report_folder`.

## Pipelines (run order)

Census 2022 (`censo_2022/`, then `censos/`; needs DTB T1 for step 5):
1. `assemble_tables.py` → tract tables, 2. `assemble_geography.py` → setores, distritos, subdistritos,
   municipios, sedes, 3. `purge_geometry_attributes.py` → setores keeps only key + geometry,
4. `build_variables.py` → VARIABLES, 5. `censos/geography_from_dtb.py` (2022) → unidades, uniao,
6. copy LAYERS/FIELDS to T1, 7. `censos/build_database.py` (2022), 8. `censos/package_t1.py` (2022).

Census 2010 (`censo_2010/`, then `censos/`; needs DTB T1 for step 4):
1. `download_2010.py` → SRC, 2. `assemble_2010.py` → T0, 3. `harmonize_2010.py` → T1,
4. `censos/geography_from_dtb.py` (2010), 5. copy LAYERS/FIELDS, 6. `build_database.py`, 7. `package_t1.py`.
(`download_malhas.py` / `merge_malhas.py`: the 2010 district and subdistrict meshes per state → national layers.)

Census 2000 (`censo_2000/`, then `censos/`):
1. `assemble_tables.py`, 2. `assemble_geography.py` (needs T1 basico), 3. `build_variables.py`,
4. copy LAYERS/FIELDS, 5. `build_database.py` (2000, 4 catalogs incl. consistency), 6. `package_t1.py`.

Census 1991 (`censo_1991/`):
1. `profile_microdata.py` (report used by build_variables), 2. `assemble_microdata.py`,
3. `assemble_geography.py` (uses census 2000 T1 names and seats), 4. `assemble_aggregates.py`,
5. `assemble_tracts.py`, 6. `infer_tracts.py` (needs the microdata lineage report), 7. `assemble_comparability.py`,
8. `build_variables.py`, 9. copy LAYERS/FIELDS, 10. `build_database_1991.py` (dedicated builder),
11. `censos/package_t1.py` (1991).

DTB (`dtb/`): `assemble_tables.py` → `assemble_geography.py` → `build_lineage.py` → `plot_lineage.py` → copy LAYERS → `censos/package_t1.py` (dtb) → `build_database_dtb.py` (T2) → `check_visuals.py`.

POP (`pop/`, needs DTB T1 and a copy of the census 2022 T1 basico in POP SRC): `assemble_population.py` → copy LAYERS → `check_visuals.py` → `censos/package_t1.py` (pop) → `build_database_pop.py`.

PNSB (`pnsb/`, needs DTB T1, configs `<script>_<year>`): `download_pnsb.py` (SIDRA) → `assemble_pnsb.py` → `censos/geography_from_dtb_editions.py` (plotting layers from DTB, config `geography_pnsb_<year>`) → copy LAYERS/FIELDS of the year → `censos/build_database_tables.py` (T2, config `build_database_pnsb_<year>`) → `censos/package_t1.py` (Zenodo, config `package_t1_pnsb_<year>`).

ATLASSAN (`atlassan/`, needs DTB T1): `download_atlas.py` (GeoServer) → `assemble_atlas.py` → `censos/geography_from_dtb_editions.py` (config `geography_atlassan`) → copy LAYERS/FIELDS → `censos/build_database_tables.py` (T2, config `build_database_atlassan`) → `censos/package_t1.py` (config `package_t1_atlassan`).

Checks (`censos/`, after the T1s): `check_population.py`, `check_coverage.py`, `check_pyramids.py`, `check_income.py`.

`run_suite.py` (repo root, venv activated) runs these pipelines in order, each script from its own folder, with the
copies of the catalogs and census inputs (`run_suite.local.json` lists the steps):

```
python run_suite.py --config run_suite.local.json --list
python run_suite.py --config run_suite.local.json dtb pop --overwrite --dry-run
python run_suite.py --config run_suite.local.json censo_2010 --overwrite --from censo_2010:3
```

Moving a dataset built in the repo's `data/` into the warehouse: `python move_datasets.py --config move_datasets.local.json` (dry run), then with `--execute` (copy, verify every file by SHA-256, then delete the source).

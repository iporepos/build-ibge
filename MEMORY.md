# MEMORY — build-ibge

Distilled from the bench sessions (2026-10-02 to 2026-10-26) that wrote these scripts. Longer working notes:
`censos/NOTES.md` (cross-census plan, checks, 2010 repair), `censo_1991/NOTES.md` (1991 decisions and status),
`dtb/NOTES.md` (DTB + POP status and 22 open items), `dtb/DECISIONS.md`, `pop/DECISIONS.md`. Those notes were
written in the bench, so their paths and config names may still show the bench layout.

## Warehouse (this machine)
- Placeholders used in the notes: `<IBGE root>` = the warehouse IBGE folder (`/path/to/IBGE` in the example
  configs; the real path is in the `.local.json` configs); `<work root>` = the working data folder where DTB and
  POP were built before promotion.
- Census data: `<IBGE root>/CENSO-<year>/<version>/{SRC,T0,T1/ASSETS,T2/ASSETS}`;
  versions: 1991 V100, 2000 V100, 2010 V300, 2022 V300 (found under V300 instead of V200 on 2026-10-26).
- PNSB-<year> and ATLASSAN: built in the repo's `data/{PNSB-<year>,ATLASSAN}/V1`, to be moved by the user to `<IBGE root>/<DATASET>/V100` (move_datasets.py; configs already point there) (gitignored; the `.local.json` configs point
  there as `../data/...`); DTB was promoted by the user to `<IBGE root>/DTB/V100` (2026-10-27; all configs point there). POP was promoted by the user to
  `<IBGE root>/POP/V100` (2026-10-26; the POP configs point there).
- Rule: the user writes the warehouse; Claude tests on scratch outputs (`.claude/settings.json` denies edits there).
- T1 is the master of the catalogs it holds, except the hand-kept LAYERS/FIELDS in this repo, which the user copies in.

## Shared census tools (`censos/`)
- `build_database.py`: any year (2000, 2010, 2022). Layers from the LAYERS catalog (theme geography → copied
  with ogr2ogr; other themes → attribute tables; `skip_themes`); catalogs as tables (open-ended, 2000 adds
  consistency). Types: `*_code` TEXT, `area_km2` REAL, V columns INTEGER unless a non-integer value is
  found (REAL; enforce with `real_variables`). Always from zero (`--overwrite`), `.part.gpkg` then rename,
  unique index on the key, cross-check of keys vs setores/basico, quick_check. Reads packaged `.gz` files too.
  1991 has its own builder (`censo_1991/build_database_1991.py`); the user wanted the generic one untouched.
- `package_t1.py`: one reproducible gzip per csv/gpkg (fixed header, same bytes every run), catalogs copied,
  verify, incremental by manifest (`<prefix>_MANIFEST_X_X.csv` + SHA256SUMS), log lists what to upload.
  Output to the dataset's SRC (which also holds raw downloads; only manifest files go to Zenodo).
- `geography_from_dtb.py` (2026-10-26): `unidades` (DTB states + alias + region + area_km2 in ESRI:102033)
  and `uniao` (= Brazil outline: "União" is the federal entity; the old EN text "federal lands" was a
  mistranslation) for 2010 and 2022, EPSG:4674. 2010 uniao keeps the Baturité CE mesh gap (249 + 60 km²)
  and a 1.3 km² hole MT/PA as holes.
- Checks (population, coverage, pyramids, income), results logged in `censos/NOTES.md` §2:
  all national populations = IBGE totals; coverage 0/89 flagged after the 2010 repair; pyramid totals vs
  population 1991 0, 2000 −0.09%, 2010 −0.15%, 2022 −1.13% (hidden cells); income p90/p10 8.0 (1991),
  6.8 (2000), 4.96 (2010), 4.6 (2022). `check_coverage.json` `expected_partial` lists known source gaps.
- 1991 T2 table names use `_` where the T1 names have `-` (user decision, as in atlasaguas).

## Census 2022 (`censo_2022/`)
- Sector keys differ per source file; tables `assemble_tables.py` (flags: empty −9999, X −1, '.' −2; in basico
  '.' in CD_* means not applicable → NA). Hidden (−1) is NOT whole-sector only: person/household tables have
  8,684 fully hidden sectors plus many scattered hidden cells → sums that skip −1 are lower bounds.
- Geography: setores T1 keeps only `setor_code` + geometry (`purge_geometry_attributes.py`; basico carries the
  attributes; rerunning assemble_geography --overwrite brings the columns back). setores has 472,780 rows for
  468,099 codes (multi-part sectors as rows: no unique index). municipios 5,572 (5,570 + lagoons 4300001/2),
  sedes 5,570 points, bairros added by the user. setoresindigenas/quilombolas deliberately not layers.
- The old hand-made 2022 `unidades` (no script; `regiao_name` with trailing "\n", `area_sqkm`) is replaced by
  `geography_from_dtb.py` (identical geometry). LAYERS 28 rows, FIELDS 48 rows (+unidade_alias, pais_name).
- REAL variables: basico V0005/V0006, responsavelrenda V06003–V06006.

## Census 2010 (`censo_2010/`)
- Rebuilt 2026-10-26 from the IBGE release of 2026-06-15 (`<UF>_20260615.zip`, Cod_setor corruption fixed by
  IBGE): download_2010 (58 files) → assemble_2010 (T0) → harmonize_2010 (T1). The old T1 had no Pernambuco
  (26) in 26 of 27 tables (the published Zenodo deposit is affected), values rounded to ~7 digits, small gaps.
  **Solved** (user confirmed 2026-10-26): PE closed, new Zenodo 2010 version published, backups deleted.
- Source quirks handled by config: pessoa02 numbered V001–V170 except the SP files (catalog V086–V255, offset
  85); domicilio01 V030–V032 in the data but not in the doc/catalog (kept); decimals like `,5` → `0.5`;
  2 garbage cells → −9999 (RO entorno05 V1016, SP pessoa07 V101='V201'). RO entorno05: 6 populated tracts
  absent at the source (expected gap). sinopse covers 938 tracts that basico doesn't.
- The `*_backup_20261026` folders are deleted. `harmonize_2010` `other_files_folder` and `assemble_2010`
  `variables_catalog` / `reference_folder` point at `T1/ASSETS` itself (gpkgs, catalogs, previous tables).
- 2010 VARIABLES catalog has no builder (legacy T1 file). 2010 FIELDS is generic (code/name/alias...), it does
  not list geography columns.

## Census 2000 (`censo_2000/`)
- 215,811 sectors in every table. Blank = IBGE confidentiality → −1; blanks in zero-population sectors → 0.
- Geometry: rural 1:500,000 mesh with holes exported as separate polygons (even-odd repair), range polygons
  (`setoresgrupos`), urban tracts from the user's legacy layer; 6 municipalities in the wrong UTM zone fixed
  (2708808 still ~50 km off, flagged). CRS labelled EPSG:4674 without transformation (likely SAD69).
  Every fix is a row of `IBGE_CENSO-2000_CONSISTENCY_X_X.csv`.
- Known T1 name bugs to fix: basico regiaomicro_name 31042 "PiŒí" (Piuí), 41018 "Wesceslau Braz".

## Census 1991 (`censo_1991/`)
- See `censo_1991/NOTES.md` §1 "Decisions already taken". Microdata (27 DBF, 17.0 M persons) go to T1 with
  real values; own household_code; names always from DTB `NOME_EM_MI`. Tract universe tables from the IBGE
  DVD (161,697 tracts, setor_code = municipio7+DIST2+SUBD2+SETOR4); no 1991 tract geometry (by decision).
  Microdata tract is inferred (`infer_tracts.py`, `setor_code_inferred`); aggregations from microdata are T2-only.
- Spec JSONs are the source of truth: `recode_1991.json`, `tracts_1991.json`, `aggregates_1991.json`,
  `variables_1991.json`, `labels_1991.json`, `codes_1991.json`.
- `build_variables.local.json` reads `profile_report/profile_values.csv` and `infer_tracts.local.json` reads
  `microdata_report/lineage_households.csv.gz`: run `profile_microdata.py` and `assemble_microdata.py` first.

## DTB and POP (`dtb/`, `pop/`)
- DTB snapshots 1872–2022 (municipios, sedes, unidades), quadro tables 1940–2022, genealogia, linhagem graph.
  Judgements in `dtb/DECISIONS.md` (DTB_1993 folder = 1933; DTB_2000 = DTB 1997; ...).
- POP: census municipal populations 1872–2022 (2010 source thousand-dot corruption fixed via urban+rural),
  estimates 1992–2026 (no 1996/2007/2010/2022/2023). 2022 from the census T1 basico copied into POP SRC.

## PNSB (`pnsb/`) and ATLASSAN (`atlassan/`)
- Two separate warehouse products (user, 2026-10-27). PNSB = SIDRA survey SB (all 263 tables municipal), one
  dataset per year like the censuses: `PNSB-2017`, `PNSB-2008`, `PNSB-2000` (V1), per-year configs and per-year
  hand-kept LAYERS (one row per table `t<table>`, CRLF) / FIELDS; files `IBGE_PNSB-<year>_T<table>_BRASIL_<year>.csv`
  (`v<var>_<cat>...`, no year column), generated VARIABLES / TABLES / CONSISTENCY. Decisions `pnsb/DECISIONS.md`.
- ATLASSAN = only the 424 GeoServer maps (`AtlasSan2021_*` and, chapters 2–3, `atlassan2021_*`) of the Atlas de Saneamento 2021 (standalone, repeats PNSB data):
  `IBGE_ATLASSAN_<LEVEL|code>_BRASIL_<year>.csv` (`as<code>`), VARIABLES / CATEGORIES / CONSISTENCY, LAYERS /
  FIELDS. Decisions `atlassan/DECISIONS.md`. Rows = DTB edition of the year in both.
- SIDRA: max 50,000 values per request; servicodados answers gzip unasked; lists today's municipalities for
  every year (`...` before installation). GeoServer: use `geoserverCGEO/CGEO/wfs`; random "details omitted"
  exceptions → retries; drop the per-request `FID`.

## Open items
- 1991 Zenodo v1.1.0 upload (warehouse done 2026-10-26; files in `censos/NOTES.md` §0/§1.1; check whether the
  v1.0.0 comparabilidade-2022 on Zenodo already has setor_code_1991/2022, else upload it too).
- 2022: new Zenodo version (unidades/uniao, FIELDS); add the two layers to the layer list of `zenodo_2022.html`.
- DTB/POP: 20 items in `dtb/NOTES.md` (AMC, recent meshes, promotion to the warehouse, T2...); sedes 2010/2022
  coded, waiting for the user's SRC copy + rerun.
- 1991: descricoes-setores, extended municipal table, comparability tables of the other states (IBGE).

## Session log
- 2026-10-26: repository assembled from the bench folders `censos/`, `censos/checks/`, `censos/templates/`,
  `censo_1991/`, `censo_2000/`, `dtb/`, `pop/`. Configs split into `.local.json` / `.example.json`
  (in-repo paths made relative to the script folder; docstring usage lines updated), Zenodo build templates
  renamed `build_database_<year>.zenodo.json`. Smoke test: every script `--help` OK (download_malhas needs
  `requests`); check_population and a download_2010 dry run OK from a copy of the repo.
  State of the warehouse at that time: 2010 T2 rebuilt with unidades/uniao (13:51); 2022 T1 has the new
  unidades/uniao gpkgs but its T2 (13:56) was built with the old 26-layer catalog → copy the 2022 LAYERS and
  FIELDS from `censo_2022/` into T1 and rebuild 2022 T2.
- 2026-10-26: user confirmed the 2010 Pernambuco problem is solved; marked closed in MEMORY.md and
  `censos/NOTES.md` (§0 item 2, §1.2), stale "no PE" notes removed (NOTES §2, check_income configs).
- 2026-10-26: user confirmed the 2010 Zenodo version is published and the 2010 backups deleted; 2010 configs
  (harmonize_2010, assemble_2010, local + example) repointed from `ASSETS_backup_20261026` to `T1/ASSETS`.
- 2026-10-26: 1991 v1.1.0 retested in the scratchpad (all 5 steps, results in `censos/NOTES.md` §1.1); warehouse
  still at v1.0.0. `package_t1_1991` configs repointed `SRC` → `SRC/ZENODO` (where the v1.0.0 package and manifest
  are); `censo_1991/profile_report/` copied from Bench. User to run the 5 commands, then Claude switches the
  1991 population checks to basico.V06.
- 2026-10-26: repo `.venv` created (`python3 -m venv .venv`, `pip install -r requirements.txt`, Python 3.12.3, all
  pins incl. requests); same outputs as the Bench venv (assemble_tracts byte-identical), every script `--help` OK.
  Use `../.venv/bin/python` from the dataset folders; the Bench venv is no longer needed.
- 2026-10-26: user ran 1991 v1.1.0 (15:37-15:43); T1/T2/package byte-identical to the scratch test. 2010 and 2022
  found under V300 (no V200 left): every config (.local + .example), README, NOTES and docstrings repointed
  V200 -> V300. 1991 population checks switched to basico.V06 (pessoa01.V0114 as alternative). All four checks
  rerun from the repo .venv: exit 0, results unchanged (NOTES §2.3).
- 2026-10-26: DTB/POP `.local.json` configs repointed from the deleted `Bench/data` to `../data` (repo data/
  folder). DTB sedes 2010/2022: `assemble_geography.py` reads GeoPackage sources ("gpkg"/"layer") and drops
  seats not in the year's mesh by code ("drop_unknown"); configs 2010/2022 get "seats" (census 2022 seats).
  Scratch run --years 2010 2022: 2 new sedes files, all other layers identical. New hand-kept
  `dtb/IBGE_DTB_LAYERS_X_X.csv` (sedes text, LF like the T1 copy). User: copy the census seats into
  data/DTB/V1/SRC/DTB_2022/, rerun assemble_geography (full), copy LAYERS into T1.
- 2026-10-26: POP T1 split into one file per layer and year (user decision, pop/DECISIONS.md):
  `assemble_population.py` writes `IBGE_POP_<LAYER>_BRASIL_<year>.csv` and takes `--years` (only those files and
  CONSISTENCY rows of those years); `check_visuals.py` reads the per-year files by glob. Scratch: 92 files, rows
  identical to the combined files; --years 2026 update byte-identical to the full build. New hand-kept
  `pop/IBGE_POP_LAYERS_X_X.csv` (fixes the unquoted ';' row). User: rebuild, delete the 4 combined files, copy LAYERS.
- 2026-10-26: POP package + T2. `censos/package_t1.py` gained "bundles" (reproducible zip per group of files,
  {YEARS} in the name; census configs unaffected: 1991 rerun 21 unchanged); `censos/package_t1_pop.*.json` = 4
  zips + catalogs into POP/V1/SRC/ZENODO. New `pop/build_database_pop.py` (+ configs): T2 gpkg with municipios
  (censos + estimativas + interpolated 1996/2007/2023, flags source/series/interpolation), unidades, notas,
  catalogs; scratch build OK (218,878 rows, quick_check ok). User: rebuild POP T1 per year, then package and T2.
- 2026-10-26: POP 2010 census fix: `census_2010` gave values under 3 digits only the ×1000 reading, so 10 rural
  counts were 1000× too big (e.g. `43` → 43,000); now both readings, all 5,565 rows total = urban + rural, the 20
  CONSISTENCY rows of those 10 go away (dtb/NOTES.md item 14 done). Urban/rural exists only in the censuses
  1940+ (the estimates publish totals only). New `run_suite.py` (+ `run_suite.{local,example}.json`) at the repo
  root: pipelines dtb, pop, censo_2022, censo_2010, censo_2010_malhas, censo_2000, censo_1991, censos_checks,
  each script run from its folder with the active Python; copy steps for catalogs / census inputs; --list,
  --dry-run, --overwrite (not passed to package_t1), --from <pipeline>:<n>, --with-optional (downloads).
  Tested: copies, failure stop, checks pipeline all ok. User: `python run_suite.py --config run_suite.local.json pop --overwrite`.
- 2026-10-26: user ran `run_suite.py ... pop --overwrite` (18:31): all 6 steps ok, 2010 file and CONSISTENCY
  byte-identical to scratch; package: changed CONSISTENCY + censos zip. User zipped T2 as
  `T2_IBGE_POP_BD_BRASIL_1872-2026.gpkg.zip`. New `pop/zenodo_pop.html` (deposit description, v1.0.0; assumes
  the T2 zip is uploaded; no builder script in the deposit).
- 2026-10-26: DTB T2. New `dtb/build_database_dtb.py` (+ configs; step dtb:7 of run_suite): 17 tables in
  `IBGE_DTB_BD_BRASIL_1872-2022.gpkg` (895 MB): municipios / sedes / unidades = the 14 snapshots in one layer
  each with `year` (key year + code, partial unique index skipping the NA codes of the special areas), pais and
  the 2022 regions as they are, quadro_municipios / quadro_distritos / quadro_subdistritos, genealogia, linhagem
  (T2 names use `_`), layers (T1 LAYERS rows, snapshot texts replaced in config), fields, consistency. Types from
  T1 FIELDS (dtype_stored; NA in numeric → no_data -9999). Scratch: every value and geometry identical to T1,
  0 seats without polygon, quick_check ok, 7 s. User: `cd dtb && python build_database_dtb.py --config
  build_database_dtb.local.json`.
- 2026-10-26: DTB package + Zenodo page. `censos/package_t1_dtb.*.json` (step dtb:7 of run_suite): 3 zips
  (MUNICIPIOS / SEDES / UNIDADES_BRASIL_1872-2022, one gpkg per year), 6 regional gpkg.gz, 5 csv.gz, 3 catalogs
  → `data/DTB/V1/SRC/ZENODO` (~625 MB, level 6). Scratch: rerun all unchanged, forced repack same bytes,
  sha256sum -c ok. New `dtb/zenodo_dtb.html` (v1.0.0; lists `T2_IBGE_DTB_BD_BRASIL_1872-2022.gpkg.zip`, zipped by
  the user as for POP). User: run the package, build T2, zip T2.
- 2026-10-26: DTB T2 changed (user: split the yearly layers, not merged): `build_database_dtb.py` writes one layer
  per T1 file, `<name>_<year>` (municipios_1872 … unidades_2022, 42 layers, no year column, unique index on the
  code skipping NA) + 2022 regional layers + 5 tables + 3 catalogs = 56 layers; layers catalog one row per layer
  (T1 text + "Snapshot <year>"). Scratch: every layer identical to its T1 file, quick_check ok, 894 MB.
  zenodo_dtb.html Database section updated.
- 2026-10-26: Atlas de Saneamento 2021 assessment → `atlassan/NOTES.md` (no code yet). SRC `data/ATLASSAN/V1`: PDF +
  `_LEGACY` 40 hand-exported CSVs (identical to warehouse `SANEAMENTO/V1/SRC`). The GeoServer (CGEO workspace)
  has 194 layers (WFS attributes, legends, INDE ISO records): ~114 municipal (2017 base, 4 on 2008 base), 41 state,
  23 intermediate region, 11 country, REGIC 2; chapter 2 (pp. 37-90) has no layer. Proposed: harvest script →
  municipal wide table (as<code> columns) + level tables + VARIABLES/CATEGORIES. Open: chapter 2 via PNSB/SIDRA?,
  name ATLASSAN vs SANEAMENTO. Noticed: POP moved to warehouse `POP/V100` (data/POP gone); pop configs still
  point at ../data/POP.
- 2026-10-26 (night, user asleep: "advance as much as possible, log a file of decisions, reach a decent T1";
  name ATLASSAN; "retrieve everything possible from the same source, maybe SIDRA"). POP `.local.json` configs
  (assemble_population, check_visuals, build_database_pop, package_t1_pop, run_suite) repointed to
  `<IBGE root>/POP/V100`, examples to `/path/to/IBGE/POP/V100` (not run, as asked). New `atlassan/`:
  download_pnsb.py, download_atlas.py, assemble_pnsb.py, assemble_atlas.py (+ configs), LAYERS/FIELDS,
  DECISIONS.md; run_suite pipeline `atlassan` (downloads optional); README. Scratch run all ok (DECISIONS §14):
  304 PNSB files + 10 ATLAS files, SIDRA = atlas maps for water/sewer networks municipality by municipality.
  User: run the suite into `data/ATLASSAN/V1` (or copy the scratch SRC), review the Claude decisions and the
  open items O1–O6 (map↔SIDRA mapping, English texts, T2, coded labels, PNSB subjects to keep).
- 2026-10-27: user ran the `atlassan` suite at 08:00 (all ok, = scratch). Then: "PNSB is a separate entry in the
  warehouse; ATLASSAN another, standalone"; PNSB split by year like the census. New `pnsb/` (download_pnsb,
  assemble_pnsb single-year, configs per year, LAYERS/FIELDS per year, DECISIONS.md); atlassan trimmed to the
  Atlas (names without `ATLAS-`, VARIABLES without SIDRA fields); run_suite pipelines `pnsb_2017/2008/2000` +
  `atlassan`. Built by Claude in `data/` as the user asked (SRC split from the night download, no new download):
  values identical to the night T1. Old SIDRA SRC + T1 moved to `data/_TRASH_20261027/ATLASSAN_V1/` (user deletes).
  Open: English table names (LAYERS description_en NA), T2, map↔PNSB mapping.
- 2026-10-27 (later): chapters 2–3 of the Atlas are on the GeoServer with lowercase prefix `atlassan2021_` (230 layers,
  missed by a case-sensitive filter; user found `CGEO:atlassan2021_020704`). download_atlas matches the prefix ignoring
  case; 424 layers harvested into data/ATLASSAN; assemble_atlas config keys `geocodigo_uf`, 4-digit `geocodigo`; T1 22
  files (MUNICIPIOS 2017 296 columns). Open: drop the census-tract layers 030201/03_setor_centroide (repeat CENSO-2010)?
- 2026-10-27 (later): dropped repeated atlas layers (config skip_layers: 030201, 03_setor_centroide, 03_cbhs_estaduais);
  English descriptions for all PNSB LAYERS (263 tables); assessment: nothing critical missing (atlassan DECISIONS §18).
  New T2 builder `censos/build_database_tables.py` (configs build_database_pnsb_<year>, build_database_atlassan), steps
  added to the suite pipelines; built in data/ (PNSB 2017/2008/2000, ATLASSAN), all tables equal to T1; suite 16 steps ok.
  Pending: Zenodo packaging (package_t1) of PNSB/ATLASSAN; promotion to the warehouse; delete data/_TRASH_20261027.
- 2026-10-27 (later): ATLASSAN files split by chapter then level (`IBGE_ATLASSAN_CAP<nn>-<LEVEL|code>_BRASIL_<year>`,
  26 files, layers cap<nn>-<level|code>; atlassan DECISIONS §20). User promoted DTB to `<IBGE root>/DTB/V100`
  (data/DTB gone): all `.local.json` configs repointed (17 files; examples to /path/to/IBGE/DTB/V100; the census
  geography_from_dtb configs pointed at a missing Bench folder, now the warehouse too). Suite pnsb_* + atlassan ok.
- 2026-10-27 (geography): plotting layers from DTB for PNSB-<year> and ATLASSAN (`censos/geography_from_dtb_editions.py`,
  configs geography_pnsb_<year> / geography_atlassan): GEO-MUNICIPIOS/SEDES/UNIDADES/PAIS (+ REGIOESINTERMEDIARIAS 2017)
  per edition, from the nearest later DTB mesh (2008 ← 2010 with Nazária dissolved into Teresina; 2017 ← 2022),
  coverage-simplified 0.001°. T2 builder copies them (geo_*). Suite steps added; 20 steps ok. Checked visually.
- 2026-10-27: geography layers renamed `ibge_dtb_<name>_<edition>` (T1 layer, T2 table, LAYERS); files keep `IBGE_<DATASET>_GEO-<NAME>_BRASIL_<year>.gpkg`.
- 2026-10-27 (promotion prep): `move_datasets.py` (+ configs; copy, SHA-256 verify, delete source; dry run by default)
  for data/{ATLASSAN,PNSB-2017,PNSB-2008,PNSB-2000}/V1 -> <IBGE root>/<DATASET>/V100. All PNSB/ATLASSAN configs
  (local + example, 34) repointed to V100 BEFORE the move. New package_t1_pnsb_<year> / package_t1_atlassan
  (SRC/ZENODO; PNSB: TABELAS zip + GEO zip; ATLASSAN: one zip per chapter + GEO), tested on scratch; suite step
  added (overwrite false). Note: `grep` here is ugrep and skips gitignored files (*.local.json): use find/awk.
  User to run: move (dry run, then --execute), then `python run_suite.py --config run_suite.local.json pnsb_2017
  pnsb_2008 pnsb_2000 atlassan --from pnsb_2017:6` style or the package steps, and delete data/_TRASH_20261027.

- 2026-10-27 (Zenodo): user ran the move (log move_report/move_20261027_104834.log: 4 moves, all files verified,
  data/ now empty) and the packaging (SRC/ZENODO of the 4 datasets). Zenodo pages written:
  `pnsb/zenodo_pnsb_<2017|2008|2000>.html`, `atlassan/zenodo_atlassan.html` (POP/DTB style, short); they list the
  T2 database as `T2_IBGE_<DS>_BD_BRASIL_<year>.gpkg.zip`, which the user zips and ships by hand (as POP/DTB).
- 2026-10-27 (exploration): PNSB time series = SIDRA multi-period tables (T1364 treatment type and T1773 volume in
  all 3 years; economias v722/v754/v755 in all 3; ~550 cols 2000-2008, 94 cols 2008-2017). Units check: not
  comparable as published (O7 in pnsb/DECISIONS.md). ATLASSAN 030801 treatment points come from ANA Atlas Águas
  (~44% placed at the municipal seat); user will use ANA as source for intakes/treatment points.

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
  versions: 1991 V100, 2000 V100, 2010 V200, 2022 V200.
- DTB and POP T1 were built in the bench (`<work root>/{DTB,POP}/V1`); promotion to
  the warehouse is pending (the empty `<IBGE root>/DTB/V1/T1/ASSETS` is not used yet).
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

## Open items
- 1991 v1.1.0 on the warehouse (`censos/NOTES.md` §1.1) and its Zenodo upload.
- 2022: new Zenodo version (unidades/uniao, FIELDS); add the two layers to the layer list of `zenodo_2022.html`.
- `requests` is not in the warehouse venv (only `censo_2010/download_malhas.py` needs it).
- DTB/POP: 22 items in `dtb/NOTES.md` (sedes 2010/2022, AMC, recent meshes, promotion to the warehouse, T2...).
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

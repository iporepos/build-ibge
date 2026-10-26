# Censo 1991 — plan and status

Last updated: 2026-10-23. **Status: tract T1 scripts ready (see §11); T2 still to revise.**

Paths:
- Data: `<IBGE root>/CENSO-1991/V100/{SRC,T1/ASSETS,T2/ASSETS}`
- Scripts: `censo_1991/`. Every script takes `--config <json>`.
- Rule: the user runs anything that writes to the warehouse. Claude tests in the scratchpad only.
  The scratchpad is session-specific, so its test outputs are gone in a new session.

---

## 1. Decisions already taken (do not re-litigate)

**Microdata**
- The sample microdata go to T1 with real values. Booleans are 1/0. Flags are -9999 for no data/NA and -2 for ignored.
- Our own `household_code` = municipio_code (7) + 6-digit sequence. `person_code` = household_code + PESSOAN.
  Grouping must never depend on record order.
- Layer names are `microdados-domicilios` and `microdados-pessoas`, so the files are `MICRODADOS-DOMICILIOS` and `MICRODADOS-PESSOAS`.

**Names and codes**
- Names always come from the DTB excel column `NOME_EM_MI`, never from the shapefile.
- Codes follow the 2000+ pattern.
- Islands are kept as unassigned.

**T1 vs T2**
- T1 is a near-copy of the source. Shipped IBGE aggregates (`agregados-municipios`, V001..V008) are kept
  apart from computed aggregates, which belong to T2 only.
- The `sedes` layer stays clean: only `municipio_code` and `municipio_name`.

**T2 builder**
- T2 uses a dedicated builder, `build_database_1991.py`. The generic `censos/build_database.py` was restored unchanged.
  A copy with link checks was saved as `scratchpad/build_database.generic_with_links.py`, which is now likely lost.
- T2 does not load the microdata. It aggregates them per municipality into separate computed tables
  ("Systematic" set). These are kept apart from the shipped aggregates.

## 2. T1 status

| Step | Script / config | Output (T1/ASSETS) | Run on the warehouse? |
|---|---|---|---|
| Profile microdata | `profile_microdata.py` | `profile_report/` | yes |
| Microdata | `assemble_microdata.py` + `recode_1991.json` | MICRODADOS-DOMICILIOS (4,024,553), MICRODADOS-PESSOAS (17,045,712) | yes (renamed to MICRODADOS-*) |
| Catalogs | `build_variables.py` (+ `variables_1991.json`, `labels_1991.json`, `codes_1991.json`, `aggregates_spec`) | VARIABLES, CATEGORIES | yes |
| Shipped aggregates | `assemble_aggregates.py` + `aggregates_1991.json` | AGREGADOS-MUNICIPIOS (4,491 rows, V001..V008) | yes |
| Geography | `assemble_geography.py` | UNIDADES, MUNICIPIOS, SEDES | yes |
| LAYERS / FIELDS | hand-kept CSVs in this folder | 11-row LAYERS, 46-row FIELDS | yes |

### Pending user actions on the warehouse — all verified done on 2026-10-23
1. `python assemble_geography.py --config assemble_geography.local.json --overwrite`. This adds SEDES and 13 consistency rows.
2. Copy `IBGE_CENSO-1991_LAYERS_X_X.csv` (11 rows, includes `sedes`) to T1/ASSETS.
3. Clean the old ';' in the CONSISTENCY notes, if they are still there:
   `sed -i 's/"PESSOAN skips numbers; \(.*\)"$/PESSOAN skips numbers, \1/' IBGE_CENSO-1991_CONSISTENCY_X_X.csv`

### Notes on the T1 contents
- `PESO` = `V0927`, a household-level weight. Persons get it through `household_code`.
- **Gini (V008):** the IBGE GINI91 values cannot be reproduced from the sample.
  - Brasil: IBGE 0.6366 vs 0.634 (household per capita) vs 0.6395 (head income).
  - The best municipal correlation (0.79) is with head-of-household income.
  - 29 municipalities have no Gini in the source and are set to -9999.
- **Seats:**
  - Itapipoca and Amontada are swapped in the source; fixed via `seats_code_fixes`.
  - Duplicate seat points are resolved using the 2000 T1 SEDES layer.
  - 3 seats have null geometry and are dropped.
  - 44 seats lie more than 2 km from their 2000 position; they are kept and reported in `geography_report/seats_vs_reference.csv`.

## 3. T2 status (paused)

- `build_database_1991.py` + `build_database_1991.json` were tested in the scratchpad only (55 s, 40 MB). **Not run on the warehouse.**
- Contents:
  - geography: unidades, municipios, sedes;
  - `agregados-municipios` as shipped;
  - `estimativas-domicilios` (174 cols) and `estimativas-pessoas` (819 cols), weighted by V0927.
- Estimate columns:
  - categories become `V<n>_<code>`, plus `V<n>_ign` where a -2 value occurs;
  - quantities become `_n`, `_sum` and `_mean`;
  - every table also has `amostra` and `total`.
- Excluded variables: V0927, V1102, V1155 and V1154, whose classifications are too detailed.
- Check: the estimated population is 146,815,790 vs the universe's 146,825,475.
- **Hold reason:** the tract aggregates below may become the main universe data. Revisit the T2 design first (§5).

## 4. New source: tract aggregates DVD (`SRC/liv93703_dvd.zip`)

Publication "Agregado de setores / Descrição de setores censitários", 144 MB zipped, 866 MB extracted.

### Data tables
`Dados/<set>/<SET><UF>.xls`: one sheet per UF. Row 1 holds the mnemonic column names; the meanings are in `Documentação/*.doc`.

| Set | Cols | Content |
|---|---|---|
| Pessoas | 118 | population by sex × 5-year age group, total and literate |
| Domicílios | 108 | household type, water, sanitation, tenure, garbage, rooms, residents, households by size, collective |
| Condição de Ocupação | 64 | residents by relationship to the head × sex, total and literate |
| Chefes | 30 | heads by years of schooling and income bracket, head income (RENDNOMCH) |

- **Columns:**
  - key columns: UF, MUN (4 digits), DIST, SUBD, SETOR, SITUACAO (1–8), TIPO (0–7);
  - all four sets end with NOMEMUNIC, NOMEDISTRI and NOMESUBDIS.
- **Coverage:** 161,697 tracts, the identical set in all four tables. All 4,491 municipalities map to the DTB via
  UF+MUN = the 6-digit code.
- **Validation:**
  - POPTOTAL = 146,825,475, which equals FRACAMO in every municipality;
  - TOTDOMPART = TOT_CHEFE = 34,734,715, which equals the universe households;
  - TOTALDOMIC = 35,417,653.
- **Defect:** `CONDICDF` has 3,093 exact duplicate rows. Drop them and log the drop in CONSISTENCY.
- **Quirks:**
  - zip member names are cp437 and must be decoded as `name.encode("cp437").decode("cp850")`;
  - in Chefes, the code columns are text.

### Tract descriptions
`Descrição de setores/DSetor91<UF>.dbf`, cp1252, about 1.6M lines.
- **Fields V1..V10:** UF, municipality code, municipality name, district code/name, subdistrict, tract, line type,
  sequence, and 80-character text lines. Line types: 1 = start/end, 2 = description, 3 = internal tracts, 4 = rural agglomeration.
- **File quirks:** the RJ file is named `DSsetor91RJ.dbf`, and RS is split into `Descrição RS CD 91 A/B/C.dbf`.
- **Line counts:** in AC, the descriptions cover 392 tracts while the data has 391 rows, so the two need reconciling.

### Geography
None. The 1991 tract codes are **not** comparable to the 2000 codes, even when they have the same format.
The plan is to ask IBGE for a 1991→2000 tract relational table and attach geometry later.

## 5. Proposed next steps (when resuming)

1. **Confirm the pending the warehouse actions** in §2.
2. **Decide the role of the tracts.** Suggested:
   - tracts become the finest universe grain in T1, as in 2000;
   - municipal universe tables in T2 are summed exactly from the tracts;
   - the sample `estimativas-*` tables are kept but limited to sample-only topics: income detail, migration, fertility, labour.
3. **New T1 script `assemble_tracts.py`** (with `--config`) and a spec `tracts_1991.json`:
   - read the 4 sets and decode the zip names;
   - build `setor1991_code` = DTB municipio_code (7) + DIST (2) + SUBD (2) + SETOR (4). Use a distinct name so nobody joins it to 2000 codes;
   - keep situacao/tipo as categorical;
   - rename variables to the V-pattern, keeping the source mnemonic in `source_name`;
   - check the internal sums (sex = total, age groups = total, and so on);
   - check that the municipal totals equal `agregados-municipios` V001/V002;
   - drop the CONDICDF duplicates;
   - write consistency rows;
   - outputs: AGREGADOS-SETORES-{PESSOAS,DOMICILIOS,CONDICAO,CHEFES} (names to be confirmed).
4. **Tract descriptions:** concatenate the lines per tract into one auxiliary T1 table (`descricoes-setores`),
   with the encoding handled and the RS/RJ file quirks resolved. Reconcile the tract counts with the data tables.
5. **Update the catalogs:** LAYERS, FIELDS and build_variables (the tract spec).
6. **Revise the T2 builder:**
   - tract tables without geometry, linked by municipio_code;
   - municipal sums from the tracts;
   - optional, smaller estimate tables.
7. **Later:** load the IBGE 1991→2000 tract crosswalk as its own T1 table and derive tract geometry from the 2000 tracts.

## 6. Open questions (unanswered)
- Should the 2000 T1 names `PiŒí` and `Wesceslau` be fixed?
- Should persons in collective dwellings go in a separate table? Right now each one counts as its own household in the estimates.
- Which variables to keep in the T2 estimates once the tract data exist.
- Naming of the tract tables and of the tract code field.

## 7. Scratchpad artifacts (temporary, lost between sessions)
Session scratch folder `c1991/`:
- `t1n/`: the full test T1;
- `t2/`: the test GeoPackage;
- `dvd/` and `dvd_txt/`: the extracted DVD and its docs converted to text;
- `profile_dvd.py`: the DVD profiler;
- `gini_*.py` and `gini_compare.csv`: the Gini analysis.

Re-extract the DVD from SRC if these are gone.

## 8. Tract vs microdata coverage (checked 2026-10-23)
- The tract sets aggregate only the basic questionnaire (CD 1.01; scanned in the DVD `Questionário/`).
- Of 131 microdata variables: 17 are covered by tracts, often more coarsely (5-year age groups, 8 relationship classes, schooling and income for heads only, rooms/bedrooms/bathrooms as tract means only); 5 are operational; **109 exist only in the microdata** (24 household, 85 person).
- Later idea: an extended municipal table with two components, sums of the tracts plus direct aggregation of the microdata for the items the tracts lack.

## 9. Tract code and links (2026-10-23)
- Split approved: basico, domicilio, morador, responsavel, pessoa01, pessoa02, instrucao01, instrucao02.
- `setor_code` = municipio_code (7) + DIST (2) + SUBD (2) + SETOR (4), the 15-digit form IBGE itself uses in the comparability tables (same layout as 2000+).
- `SRC/tabelas_de_comparabilidade/Comparabilidade_2022_1991_RS.csv`: 2022→1991 RS, many-to-many. Of its 12,629 1991 codes, 12,563 are in the DVD.
  - 66 are only in the table, all in districts that exist in the DVD (32 in Porto Alegre: tract numbers missing from the DVD).
  - 406 DVD tracts are missing from the table (Pelotas seat 259, Passo Fundo seat 139, Novo Hamburgo 4, Gravataí 2).
  - The other UFs are on request from IBGE.
- The microdata have no tract link. Records are in tract order (CD107 restarts) but reconstruction is unreliable (exact in 30 of 333 RS municipalities); not used.

## 10. Inferred tract for the microdata (prototype, 2026-10-23)
- Idea (user): recover the tract of each sampled household from the record order, named `setor_code_inferred`. It is weaker than `setor_code` and is meant for a sample-based tract aggregation; municipio stays the strong link.
- Method: households in file order are cut into blocks where CD107 stops increasing or SITSET changes. A monotone DP then assigns consecutive blocks (0 to 8 per tract) to the DVD tracts in (DIST, SUBD, SETOR) order. Cost: weight sum vs TOTALDOMIC, plus penalties for a situacao mismatch and for merging blocks.
- Key facts: PESO is calibrated per tract to TOTALDOMIC (exact in about 53% of tracts), and SITSET uses the same codes as SITUACAO.
- Result, all 27 UFs in 55 s: 98.7% of tracts receive sampled households. Tract shares of piped water and garbage collection correlate 0.98 to 0.997 with the universe in every UF (0.88 if shifted by one tract).
- Prototype: session scratchpad `infer/` (common.py, align.py, validate.py).
- Plan: a T1 link table household_code -> setor_code_inferred plus quality fields, built from lineage_households (microdata files untouched). Later: a T2 tract-level sample aggregation, kept apart from the universe tables.

## 11. Tract T1 built (2026-10-23), user runs on the warehouse
Decisions:
- `setor_code` = municipio_code (7) + DIST (2) + SUBD (2) + SETOR (4); distrito_code and subdistrito_code are its first 9 and 11 digits.
- 8 layers mirror 2000: basico, domicilio, morador, responsavel, pessoa01, pessoa02, instrucao01, instrucao02.
  - V0001 to V0275 are counts numbered across the tables.
  - basico has V01 to V05, real means: mean head income, collective-unit income, rooms, bedrooms, bathrooms.
  - Spec: `tracts_1991.json`.
- Names: district and subdistrict names are kept as the DVD has them (upper case). tipo_code uses the 2000 labels, which the data confirm.
- Duplicates: 3,093 exact duplicate rows were dropped (CONDICDF 1,670, CONDICRO 1,423).
- Checks: all 173 spec checks hold, and tract sums equal agregados-municipios in every municipality.
- microdados-setores (infer_tracts.py):
  - target is private households;
  - up to 40 blocks per tract;
  - no band search;
  - 99.0% of tracts receive sample;
  - water/garbage validation corr ≥ 0.976 in every UF.
- comparabilidade-2022 (assemble_comparability.py): RS only so far. Re-run it when more UFs arrive.
- Catalogs: build_variables.py now also covers the tracts (VARIABLES 419 rows). LAYERS has 21 rows and FIELDS 55.

Run order (from censo_1991/, with ../.venv/bin/python):
1. assemble_tracts.py --config assemble_tracts.local.json
2. infer_tracts.py --config infer_tracts.local.json
3. assemble_comparability.py --config assemble_comparability.local.json
4. build_variables.py --config build_variables.local.json
5. cp IBGE_CENSO-1991_LAYERS_X_X.csv IBGE_CENSO-1991_FIELDS_X_X.csv to T1/ASSETS

Next:
- T2 with tract tables (check censos/build_database.py with no tract geometry).
- Tract-level sample aggregation (from microdados-setores), built in T2, never T1. Decision (user): every row carries
  `n_sample` (unweighted microdata records used: households, or persons in person tables), so small and
  non-representative tract samples are visible. Person tables also get `n_sample_households` (persons cluster
  by household). No suppression; the layer description states it is a sample estimate on an inferred tract.
  `amostra` renamed to `n_sample` in the municipal estimates too.
- descricoes-setores (DBF tract descriptions).
- Extended municipal aggregation.

## 12. T2 built (2026-10-23), user runs on the warehouse
- Builder: build_database_1991.py stays the dedicated 1991 builder (extended; censos/build_database.py untouched).
  New config keys: `tables` (T1 CSVs loaded as they are, key + unique), per estimate `group` (link table ->
  inferred tract), `after` (layer order) and `check` (estimated total vs a universe variable).
- Database: geography (unidades, municipios, sedes), agregados-municipios, the 8 tract tables, comparabilidade-2022
  (non-unique index), 4 estimate tables, catalogs (layers 22, fields, variables 2,262, categories 16 = basico
  situacao/tipo labels, consistency). Microdata and microdados-setores are not loaded.
- Estimates renamed: estimativas-municipios-{domicilios,pessoas} (were estimativas-{domicilios,pessoas}, values
  identical) and new estimativas-setores-{domicilios,pessoas} keyed by setor_code_inferred, one row per basico
  tract (1,627 without sample, n_sample 0). All estimate tables carry n_sample, n_sample_households (persons) and
  total. Municipal estimates keep all variables (overlap with the universe kept as a check).
- Test: 2 to 6 min, peak RAM 28 GB (the old builder 24 GB), 1.06 GB. Tract n_sample median 24 households / 96
  persons (p10 8 / 31). Estimated tract persons vs pessoa01.V0114: corr 0.990, median |diff| 2.4%.
  comparabilidade-2022: 109 pairs whose 1991 code is not in basico (the 66 known codes).
- Table names (user decision): "-" -> "_" in T2 (config db_hyphen), e.g. agregados_municipios,
  estimativas_setores_pessoas; the catalogs (layers.name, variables.table/variable_code, categories.table,
  consistency.layer) and description texts use the same names. T1 file names keep the hyphen. Made after the
  full test; only the catalog renaming was checked (on the T1 catalogs), no rebuild.
- Run: ../.venv/bin/python build_database_1991.py --config build_database_1991.local.json
- Comparability (user decisions): columns renamed setor_code_2022;setor_code_1991 (the year resolves the census;
  setor_code_1991 = setor_code of the tract tables); T2 index on setor_code_1991. The 1991 tract geometry is NOT a
  built-in feature (it would duplicate the large 2022 tract layer): mapping 1991 data to 2022 geometry is a later
  analysis process. The release only adds the other UF comparability tables as IBGE sends them (rerun
  assemble_comparability.py, then T2). RS facts for that analysis: 52.9% of the 1991 tracts are an exact union of
  2022 tracts; connected components give 7,962 comparable areas (6,680 single tract, max 93 tracts).
Next: Zenodo packaging of T1 (package_t1.py; ~8.1 GB -> ~0.7 GB gz; microdata needed to rebuild T2, and
build_database_1991.py cannot read .gz yet); descricoes-setores; extended municipal table.

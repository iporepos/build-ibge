# Censos (1991, 2000, 2010, 2022) — cross-census plan and status

Last updated: 2026-10-26. Per-census details live in `censo_1991/plan.md` and in `MEMORY.md`.
Rule: the user runs anything that writes to the warehouse. Claude tests in the scratchpad only.

## 0. Where we stopped / next steps (in order)
1. **1991 v1.1.0 on the warehouse** (§1.1): five commands, then the Zenodo v1.1.0 upload. Nothing has run yet.
2. ~~**2010 Pernambuco repair**~~ (§1.2): **solved** (confirmed by the user 2026-10-26), including the new Zenodo 2010
   version and the deletion of the `*_backup_20261026` folders. Nothing left to do.
2b. **2022 `unidades` + `uniao`** (2026-10-26): `geography_from_dtb.py --config geography_from_dtb_2022.local.json
   --overwrite` (replaces the hand-made `unidades`, same geometry, fixed attributes; adds `uniao`), copy
   `catalog_fixes/IBGE_CENSO-2022_{LAYERS,FIELDS}_X_X.csv` into T1/ASSETS, rebuild T2 with `--overwrite`, then
   package and a new Zenodo 2022 version (layer list in `zenodo_2022.html`).
3. After 1 and 2: rerun all four checks (§2.1) and compare with the results logged in §2.3. Expected changes:
   2010 coverage no longer flagged, 2010 pyramids/income with PE, 1991 population check switched to basico.V06.
4. Later, optional: more checks (§2.4); the 1991 open items in `censo_1991/plan.md` (descricoes-setores,
   extended municipal table, comparability tables of the other states).

## 1. Pending actions on the warehouse (user)

### 1.1 1991 — v1.1.0 (`basico` totals), all tested in the scratchpad
From `censo_1991/`, with `../.venv/bin/python`:
1. `assemble_tracts.py --config assemble_tracts.local.json --overwrite` — `basico` gains V06 (POPTOTAL), V07 (TOTDOMPART),
   V08 (TOTALDOMIC); the other 7 tract tables are byte-identical.
2. `build_variables.py --config build_variables.local.json` — VARIABLES 422 rows.
3. `cp IBGE_CENSO-1991_LAYERS_X_X.csv <IBGE root>/CENSO-1991/V100/T1/ASSETS/` (basico text).
4. `build_database_1991.py --config build_database_1991.local.json --overwrite` — T2 rebuild.
5. `../censos/package_t1.py --config package_t1_1991.local.json` — incremental; upload the files the log lists as changed
   (basico, variables, layers, consistency) as Zenodo v1.1.0, with the description `censo_1991/zenodo_1991.html`
   (already has the v1.1.0 entry).
After step 1: point `censos/checks/check_population.json` (and `check_coverage.json` population) 1991 at
`basico.V06`.

### 1.2 2010 — Pernambuco missing — SOLVED (confirmed by the user 2026-10-26; kept for the record)
Found by the coverage check (§2): PE (26) has **no row** in 26 of the 27 T1 tract tables; it is present only in
`sinopse` (12,392 tracts) and the `setores` geometry (12,498).

**Which T1 is the source of truth (verified 2026-10-26).** `T1/ASSETS` is the current version; `T1/_legacy` is the
raw input from before harmonization (the old ASSETS, built 2026-02-26). On 2026-10-24 the folders were renamed
(old ASSETS -> `_legacy`, HARMONIZED -> ASSETS). The proof: `harmonize_2010.py` run on `_legacy` (output to the
scratchpad) gives all 27 CSVs **byte-identical** to `T1/ASSETS`. The gpkgs and catalogs are identical in both folders, and
both lack PE. So `_legacy` holds no data that ASSETS doesn't, and it can be deleted:
`rm -r <IBGE root>/CENSO-2010/V200/T1/_legacy` (4.5 GB).

**New IBGE release (2026-06-15).** IBGE republished every UF (`*_20260615.zip`); the change log says that the
`Cod_setor` column of the CSVs was corrupted and has been fixed. That is the damage that harmonize repairs by
position. A comparison of the new source with T1 on 4 tables found more than PE:
- missing tracts are back: TO basico +12, RR pessoa04 +21, CE pessoa07 +28 (RO entorno05: 6 tracts still absent
  at the source, a real gap);
- T1 values rounded to about 7 significant digits (e.g. 134669180,98 -> 134669181.0, Excel/float32 damage);
- RR pessoa04 counts off by 1 (T1 predates an IBGE correction); CE pessoa07 cells that IBGE now hides with `X`
  had values in T1; 3 CE keys of T1 are not in the source (bad positional repair?).
PE source checked: all 26 tables, 12,379 tracts, `;`-separated, latin1, CRLF, quoted header, decimal comma,
leading id columns (`Cod_setor`, `Situacao_setor`, and names in Basico) before the V columns.

**Decided 2026-10-26: A.** The user renamed T1/ASSETS and T2/ASSETS to `ASSETS_backup_20261026` (the backup); no
staging folder: the rebuild writes the new T1/ASSETS directly. The gpkgs and catalogs are now only in the backup.
- A. Rebuild the 2010 T1 tract tables from the 2026-06-15 release, all UFs. This restores the T0/SRC provenance (empty
  today), uses the official key fix instead of the positional repair, and fixes the rounding and the TO/RR/CE gaps.
- B. Patch PE only (+ basico TO, pessoa04 RR, pessoa07 CE) into the current T1. Faster, but it keeps the rounding and
  the repaired keys, and it mixes two releases.

**Downloads** (into `CENSO-2010/V200/SRC/`, one folder per product):
- A: `https://ftp.ibge.gov.br/Censos/Censo_Demografico_2010/Resultados_do_Universo/Agregados_por_Setores_Censitarios/`
  28 zips `<UF>_20260615.zip` (AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE TO,
  `SP_Capital`, `SP_Exceto_Capital`), about 1.6 GB, plus `Documentacao_Agregado_dos_Setores_2010_20231030.zip` and
  `1_Atualizacoes_20260615.txt`. Sinopse (optional; T1 already has PE, so this is for provenance):
  `.../Censo_Demografico_2010/Sinopse/Agregados_por_Setores_Censitarios/Base_informacoes_setores2010_sinopse_<UF>.zip`
  (27 UF zips + the 2 SP ones).
- B: `PE_20260615.zip`, `TO_20260615.zip`, `RR_20260615.zip`, `CE_20260615.zip` (about 155 MB).

**Steps (nothing written in place; the user runs everything on the warehouse):**
0. Backup / snapshot: `sha256sum` of T1/ASSETS and of `T2/ASSETS/IBGE_CENSO-2010_BD_BRASIL_2010.gpkg` into a
   manifest. The published Zenodo 2010 deposit is the off-site copy of the current T1.
1. Download into SRC: `censos/download_2010.py --config download_2010.local.json` (written and tested 2026-10-26; checks size + zip
   integrity, never overwrites).
2. `censos/assemble_2010.py --config assemble_2010.local.json` (written + tested 2026-10-26): SRC -> T0/ASSETS (raw text tables,
   checked against the VARIABLES catalog; compare report vs the backup in censos/assemble_report).
3. `censos/harmonize_2010.py --config harmonize_2010.local.json`: T0/ASSETS -> T1/ASSETS, gpkgs + catalogs copied from
   T1/ASSETS_backup_20261026. Scratchpad test: 0 keys repaired, 0 rows left out.
4. Run the checks (§2) against T1/ASSETS; coverage must no longer flag PE.
5. (Swap done by the user: the old T1/T2 are in `ASSETS_backup_20261026`.) Delete the backups only after the new Zenodo version is
   published and checked.
6. Fix the stale configs: harmonize_2010.json, build_database_2010.json, package_t1_2010.json and the 4 check
   JSONs still point at `T1/HARMONIZED` (gone). Then `build_database.py` (2010 T2), `package_t1.py`, new Zenodo
   version (`censos/zenodo_2010.html`: version-log entry about PE + the 2026-06-15 source).

## 2. Sanity checks (`censos/checks/`)

### 2.1 Scripts
Every script takes `--config` (JSON next to it); outputs go to a report folder next to it. They only read the warehouse.
Run from `censos/checks/` with `../../.venv/bin/python <script> --config <script>.json`.

| Script | What | Report | Runtime |
|---|---|---|---|
| `check_population.py` | total resident population, national and by state, vs IBGE totals; alternative variables | `population_report/` (3 CSV, 2 PNG) | ~1 min |
| `check_coverage.py` | rows per state of every T1 table/layer; missing tracts vs the reference split by population (populated / empty / unknown); flags missing states or populated tracts | `coverage_report/` (2 CSV, heat map per census) | ~2.5 min |
| `check_pyramids.py` | age pyramids by sex, national and by state, per census and overlaid | `pyramid_report/` (2 CSV, 7 PNG) | ~15 s |
| `check_income.py` | exceedance curves of the tract mean income of household heads (income exceeded by a share of the households; flatter = more equal; horizontal at 1 = perfect equality), national in minimum wages and relative to the median, states relative to their own median, each in log and linear scale; p90/p10 and p75/p25 per state | `income_report/` (2 CSV, 5 PNG) | ~5 s |

Common rules: flagged cells (< 0) count as 0 or are left out (counted in the logs); national = sum of the states
that have data, missing states named in the figures; palette = dataviz (male #2a78d6, female #eb6834,
census years in sequential blues).

### 2.2 Variables used
| Census | Population | Age by sex | Income of heads (mean / weight / minimum wage) |
|---|---|---|---|
| 1991 | pessoa01.V0114 (POPTOTAL; basico.V06 after v1.1.0) | pessoa01, 5-year groups to 80+ | basico.V01 x (responsavel V0103-V0111 + V0112) / (V0103-V0111) / responsavel.V0096 / Cr$ 36,161.60 |
| 2000 | morador.V0237 | pessoa02 (men) / pessoa03 (women), single years to 100+ | basico.V06 / basico.V01 / R$ 151 |
| 2010 | sinopse.V014 | sinopse, single years to 79, 5-year groups to 100+ | basico.V007 / basico.V001 / R$ 510 |
| 2022 | basico.V0001 | demografia 0-14 + alfabetizacao 15+ (all persons, not only literates), 5-year to 69, 70-79, 80+ | responsavelrenda.V06004 / V06001 / R$ 1,212 |

Notes on income:
- Income is the mean nominal monthly income of the heads **with income** (the only basis 2022 has).
- 1991 basico.V01 is the mean over the heads with **and without** income (undeclared left out). This was verified
  against the band limits in every tract: no other denominator fits. It is converted with the band counts.
- The four minimum wages were verified on the band data.

### 2.3 Results (2026-10-23; 2010 rerun 2026-10-26 after the rebuild)
- **2010 after the rebuild (2026-10-26):** coverage 0 of 89 files flagged (PE in every table; TO/RR/CE gaps gone;
  entorno05 RO 6 tracts = source gap, set as expected_partial). Population 190,755,799 = IBGE; the alternative
  pessoa03.V001 now matches too (0 states differ). Pyramids unchanged (sinopse). Income with PE: national median
  2.02 minimum wages, p90/p10 4.96, p75/p25 2.06; PE p90/p10 3.83. check_coverage.py printout crash fixed
  (Int64 fillna).
- **Population:** all four national sums equal the IBGE totals exactly (146,825,475; 169,799,170; 190,755,799;
  203,080,756). The state series are smooth.
- **Coverage:**
  - 1991, 2000 and 2022 are complete for populated tracts.
  - 2022: the tables other than basico leave out 9,327 tracts, all with 0 persons; the entorno* tables cover about
    60-90% of populated tracts, by design.
  - 2000: 22 setores polygons have no data.
  - 2010 is flagged for PE and the small gaps of §1.2.
- **Pyramids:**
  - Common groups: 0-4 ... 65-69, 70-79 (drawn per 5 years of age), 80+.
  - Totals vs population: 1991 exact, 2000 -0.09%, 2010 -0.15%, 2022 -1.13%. The 2022 gap comes from hidden
    cells of the IBGE age tables, up to -3.9% in RR.
  - The shapes are consistent: ageing from census to census, and the male share falls from 49.4% to 48.4%.
  - The RR 1991 bulge of men aged 25-29 is real (gold rush migration).
- **Income:**
  - National medians: 2.46, 3.40, 2.05 and 1.82 minimum wages. The real value of the minimum wage changed a lot,
    so the level shifts are not real income changes; an IPCA deflator would be the next step if needed.
  - Shape: a modest, steady gain in equality between tracts.
    - National p90/p10 falls 8.0 -> 6.8 -> 4.9 -> 4.6, and p75/p25 2.9 -> 2.0.
    - The gain comes from the bottom: the 10% line rises from about 0.36x to 0.53x the median, while the rich
      tail stays where it was.
    - The biggest step is 2000-2010, in line with the published Gini fall.
    - Every state narrows, the Northeast most (CE, PI, BA from about 6 to about 3). DF stays by far the most
      unequal (about 7-8, flat); RJ and SP come next; SC, GO, RO and MT are the least unequal.
  - Left out: 2022 9,241 hidden tracts (4,800 heads); 2000 1,987 flagged tracts (4,928 heads).
  - Caveats: the measure is between tracts only, not inside them. The tract count is about 2.8x larger in 2022
    than in 1991; finer units usually widen the spread, so the narrowing is likely understated. Comparable areas
    (comparability tables) would settle this.

### 2.4 Ideas for further checks
- households vs residents (mean household size)
- urban/rural split
- literacy shares
- cross-census municipal growth outliers
- income in real terms (IPCA)
- income spread on comparable areas, once the comparability tables exist for all states

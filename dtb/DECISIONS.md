# DTB T1 — critical judgements (2026-10-24)

Source: `<work root>/DTB/V1/SRC` (copy of `<IBGE root>/DTB/V1/SRC`, untouched). Output: `<work root>/DTB/V1/T1/ASSETS`.
Every fix is also a row in `IBGE_DTB_CONSISTENCY_X_X.csv`.

## Run order (all deterministic, `--config` JSON, `--overwrite` to replace)
1. `assemble_tables.py` → quadro-municipios / -distritos / -subdistritos, genealogia
2. `assemble_geography.py` → municipios / sedes / unidades per snapshot + 2022 regional layers (reads the T1 quadro and genealogia for names)
3. `build_lineage.py` → linhagem (the splitting graph)
4. `plot_lineage.py` → trees (Graphviz SVG/PNG) + summary chart in `lineage_report/plots`
5. `check_visuals.py` → QA figures in `visual_checks/`

Run time: ~1 min, ~1.5 min, ~1.5 min, a few seconds.

## Judgements

### Sources
- **`DTB_1993` folder holds the 1933 files** (folder name typo). Used as snapshot 1933.
- **`DTB_2000/dtb_2000.zip` is "Divisão Territorial 1997"** (leia_me) dated 29/12/1999 (sheet A991229), 5,507 municipalities = the 2000 census set. Labelled edition 2000.
- **2003: the 17/11/2003 version** (`dtb17112003nome`) is used; the 07/05/2003 one is superseded.
- **2013: the per-level files** (municipio/distrito/subdistrito); `dtb_2013.zip` (combined) not used.
- `.ods` twins of the `.xls` files are ignored (same content).
- 2000/2010 meso/microregion meshes (per UF) are **not converted**: 2010 ones carry only names; they can be dissolved from municipios + quadro codes. 2022 regional layers are converted.

### Geometry
- **Spatial master 1872–1991 = IBGE "Evolução" meshes**; IBGE built them by joining 1991 polygons, so the 1872–1991 snapshots nest exactly (same cartographic base). 2000 (1:500k), 2010 (1:250k), 2022 are independent bases.
- **2000 mesh has no .prj**: labelled EPSG:4674 without transformation (same decision as the census 2000 T1; datum likely SAD69, ~60 m, below the 1:500k accuracy).
- **Code fixes**: 1900/1911 mesh uses the 1933 codes of Alegre and Alfredo Chaves (3200201/3200300) → set to the genealogy codes of their first incarnation (3299905/3299906). 1991: Itapipoca 2306045 → 2306405 and seats of Itapipoca/Amontada swapped (as in the 1991 census work).
- **Kept as published although the genealogy disagrees** (logged): 1920 mesh has Porangaba (2399911, annexed to Fortaleza per genealogy) and Alegre/Alfredo Chaves (annexed per genealogy) while lacking Poranga (2399940); 1960 mesh has Salmourão (3545100; DTB 1960 list agrees with the mesh); 1980 genealogy lists Diamante D'Oeste (4107157) with no 1980 polygon. **No geometry was invented.**
- Villa Franca (PA, 1872) and Jatahy (SP, 1900–1920) have no polygon (documented by IBGE); their seats are dropped.
- **Disputed areas (06 litígio) were merged into the 1940–1980 municipios layers** (area_type litigio, code NA) so every snapshot is a full partition; 1991 already had PI/CE inside. Lagoons 4300001/4300002 and the 1991 unassigned islands also kept with area_type.
- 2000: one 0.0002 km² polygon with code 0 dropped.
- 1970: 37 São Paulo seats with code 0 assigned to the polygon containing them (each municipality without a seat).
- **Unidades** of 1872–1991: source state limits; their code (absent in the source) is the UF prefix of the municipalities with the largest overlap. 2000/2010 dissolved from municipalities.

### Names
- Name priority per snapshot (config `names`): 1940–1980 **quadro (contemporary DTB list) > genealogy > mesh**; 1872–1933 and 1991 genealogy > mesh; **1920 mesh first** (it keeps the 1920 spelling, e.g. Barcellos, Coary; the genealogy modernized 1920 names); 2000/2010 genealogy (census-date names, as IBGE did, e.g. Campo de Santana not Tacima) > quadro; 2022 quadro > mesh. All disagreements: `geography_report/names_differences.csv` (481).

### Genealogy (`dtb_evolucao_pop_1872-2010.csv`)
- Kept as a long table; the note's municipality names are resolved to codes of the same census year (exact fold in UF → other UF → difflib ≥ 0.85), else **of the next census** (`reference_year`; the note describes a change inside the interval). Lists "A, B e C" split and kept aligned with `" | "`. Result: 5,522 of 5,549 names resolved (25 unresolved, 2 ambiguous, all logged).
- "desmembrado e X" read as "desmembrado de X" (typo); '*' kept as `divergence` = 1 (IBGE flags divergent origins).

### Splitting graph (`linhagem`)
- **Codes drive the event type** (IBGE never reuses codes): same code = continuation; new code = split / recode / redistribution / emergence; vanished code = absorption / recode / redistribution. Overlay areas quantify every edge.
- Thresholds (config): noise < 0.1 % of both areas; parent/destination edges kept at ≥ 5 % share (+ always the largest); recode when both shares ≥ 90 %; transfers between continuing municipalities: ≥ 1 % & ≥ 1 km² on the same base, **≥ 10 % & ≥ 10 km² across bases** (1991→2000→2010→2022 boundaries differ mostly cartographically). The 847 transfers, 372 of them in 1991→2000, are still partly cartographic: filter with `same_base`.
- **Centroid rule** (your suggestion) is a check column, not the rule: for primary edges it agrees in ~98 % of new codes; the largest-area rule is more robust for odd shapes.
- **Genealogy check**: the primary parent agrees with IBGE's genealogy for 97–100 % of the new municipalities in every interval (e.g. 1991→2000: 1,012 of 1,016). Disagreements: `lineage_report/lineage_disagreements.csv`.
- New codes born in disputed areas (MG/ES 1950–1970, PI/CE 2000) or outside the previous mesh (Acre 1911, Fernando de Noronha 1950) get an `emergence` edge with code_from NA.
- **Tocantins**: GO→TO 1988 codes are `recode` edges; `same_trajectory` uses the genealogy trajectory code (the GO-era and TO-era rows share it).

## Fixes found by the visual checks (`check_visuals.py`, figures in `dtb/visual_checks`)
- **Swapped labels in the 1872–1991 base (mesh and seats)**: Espírito Santo do Pinhal ↔ Santo Antônio do Jardim
  (1960–1991) and Amélia Rodrigues ↔ Conceição do Jacuípe (1970–1991). The 2000, 2010 and 2022 meshes and their seats agree with each other → codes swapped back.
- **1920**: Entre Rios CE (2399909) ↔ Entre Rios PR (4199906) swapped in the mesh only; the seats were right (they had fallen outside their polygons).
- **2000**: enclaves not cut from the surrounding polygon (Ladário in Corumbá, Portelândia in Mineiros, Águas de São Pedro in
  São Pedro) → generic rule: a polygon covered ≥ 50 % by another is subtracted from it (`enclave_share`).
- **2010 mesh gaps**: 309 km² of the Maciço de Baturité (CE, 248.6 + 60.0 km²) belong to no municipality in the source → kept as
  holes, logged (`mesh_gap`). 27,123 precision slivers (2.5 km²) filled only in the dissolved 2000/2010 state layers.
- Remaining strong transfers look like real boundary revisions or base offsets (Areial/Pocinhos 1991, Salvador/Madre de Deus,
  Eldorado do Sul/Charqueadas, Costa Marques/São Francisco do Guaporé, Guaribas/Caracol, Santa Luzia/Brejo de Areia): left as edges.

## Not done / open
- Áreas mínimas comparáveis (connected components of the graph for a start year) — straightforward next step (T2-like) for harmonized population series.
- 2010/2022 seats (no source in SRC).
- Visual QA of the 2000→2010 / 2010→2022 transfers.

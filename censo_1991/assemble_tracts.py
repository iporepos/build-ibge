"""
Assemble the IBGE Census 1991 tract (setor) universe tables into T1, one CSV per layer.

Source: the IBGE DVD "Agregado de setores / Descrição de setores censitários" (SRC/liv93703_dvd.zip),
read straight from the zip (member names are cp437-encoded cp850). Four data sets, one XLS per UF
(Dados/<folder>/<prefix><UF>.xls, header row of mnemonics): Pessoas, Domicílios, Condição de Ocupação,
Chefes. Every set starts with UF, MUN (4 digits), DIST, SUBD, SETOR, SITUACAO, TIPO and ends with the
municipality, district and subdistrict names.

Layers (spec tracts_1991.json, same split as the 2000 tract tables):
  basico       setor_code, geography (codes and names), situacao_type/code, tipo_code, V01..V05
               (means: head income, rooms, bedrooms, bathrooms; collective-unit income)
  domicilio, morador, responsavel, pessoa01, pessoa02, instrucao01, instrucao02
               setor_code + V0001..V0275 (counts)
Codes follow the 2000+ layout (the one IBGE uses in its comparability tables):
  setor_code = municipio_code (7, DTB 1991) + DIST (2) + SUBD (2) + SETOR (4) = 15 digits
  distrito_code = first 9 digits, subdistrito_code = first 11 digits
Names: municipality, UF, meso/microregion and metropolitan region from the T1 `municipios` layer
(DTB 1991 names); district and subdistrict names from the DVD (Pessoas set), blank -> NA.

Checks (the run stops, nothing is written, on failure):
  * the zip holds one XLS per UF and set; headers hold every spec column; keys and values numeric
  * after dropping exact duplicate rows (same key and numbers; CONDICDF has 3,093), keys are unique
    and the four sets hold the same tracts, with the same SITUACAO and TIPO
  * every tract municipality is in the DTB 1991, and every DTB municipality has tracts
  * count variables are whole numbers
Findings that do not stop the run go to CONSISTENCY (layer = tract table): dropped duplicates, name
conflicts between sets, spec checks that fail (sum / equal / le), municipal totals that differ from
agregados-municipios (population V001, permanent private households V002).

Usage:
    python assemble_tracts.py --config assemble_tracts.local.json [--overwrite]
"""

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xlrd

from assemble_geography import merge_consistency, read_dtb

REQUIRED_KEYS = ["spec", "dvd_zip", "dtb_xls", "municipios_gpkg", "output_folder", "report_folder"]
KEYS = ["UF", "MUN", "DIST", "SUBD", "SETOR", "SITUACAO", "TIPO"]
NAMES = ["NOMEMUNIC", "NOMEDISTRI", "NOMESUBDIS"]
GEO_COLUMNS = ["setor_code", "unidade_code", "unidade_name", "regiaomeso_code", "regiaomeso_name",
               "regiaomicro_code", "regiaomicro_name", "regiaometropolitana_code", "regiaometropolitana_name",
               "municipio_code", "municipio_name", "distrito_code", "distrito_name", "subdistrito_code",
               "subdistrito_name", "situacao_type", "situacao_code", "tipo_code"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("data_folder", "Dados")
    cfg.setdefault("table_pattern", "IBGE_CENSO-1991_{NAME}_BRASIL_1991.csv")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    cfg.setdefault("municipios_layer", "ibge_censo_1991_municipios")
    cfg.setdefault("aggregates_table", None)
    cfg.setdefault("text_no_data", "NA")
    cfg.setdefault("real_decimals", 2)
    cfg.setdefault("situacao_type", {"1": "Urbana", "2": "Urbana", "3": "Urbana", "4": "Rural", "5": "Rural",
                                     "6": "Rural", "7": "Rural", "8": "Rural"})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"tracts_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_tracts")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_path)
    return logger


def clean_text(v) -> str:
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def zip_members(zf: zipfile.ZipFile) -> dict:
    """{decoded name: ZipInfo}; names without the UTF-8 flag are cp437 bytes of cp850 text."""
    out = {}
    for i in zf.infolist():
        name = i.filename if i.flag_bits & 0x800 else i.filename.encode("cp437").decode("cp850")
        out[name] = i
    return out


def read_set(zf, members: dict, folder: str, prefix: str, columns: list, logger) -> pd.DataFrame:
    """All UF files of one set as one frame: KEYS (int), spec columns (float), NAMES (text), _file."""
    pat = re.compile(rf"^{re.escape(folder)}/{prefix}([A-Z]{{2}})\.xls$", re.IGNORECASE)
    files = sorted(n for n in members if pat.match(n))
    if len(files) != 27:
        sys.exit(f"{folder}: expected 27 UF files, found {len(files)}")
    frames, errors = [], []
    for name in files:
        book = xlrd.open_workbook(file_contents=zf.read(members[name]))
        sheets = [s for s in book.sheets() if s.nrows > 0]
        header = None
        for sh in sheets:
            hdr = [clean_text(h) for h in sh.row_values(0)]
            if header is None:
                header = hdr
            elif hdr != header:
                errors.append(f"{name}: sheet {sh.name} has another header")
                continue
            if len(hdr) < len(KEYS) + len(NAMES):
                errors.append(f"{name}: header too short")
                continue
            lost = [c for c in columns if c not in hdr]
            if lost:
                errors.append(f"{name}: spec columns missing in the header: {lost}")
                continue
            data = [sh.row_values(r) for r in range(1, sh.nrows)]
            df = pd.DataFrame(data, columns=hdr)
            out = {k: df.iloc[:, i] for i, k in enumerate(KEYS)}
            out.update({c: df[c] for c in columns})
            out.update({k: df.iloc[:, i].map(clean_text) for k, i in zip(NAMES, range(len(hdr) - 3, len(hdr)))})
            out = pd.DataFrame(out)
            out["_file"] = Path(name).name
            frames.append(out)
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(f"{folder}: {len(errors)} errors — nothing written")
    d = pd.concat(frames, ignore_index=True)
    blank = (d[KEYS].astype(str).apply(lambda s: s.str.strip()) == "").all(axis=1)
    if blank.any():
        logger.info("  %s: %d empty rows skipped", folder, int(blank.sum()))
        d = d[~blank].reset_index(drop=True)
    for c in KEYS + columns:
        v = pd.to_numeric(d[c], errors="coerce")
        bad = v.isna()
        if bad.any():
            ex = d.loc[bad, [c, "_file"]].head(5).to_dict("records")
            sys.exit(f"{folder}: column {c} has {int(bad.sum())} non-numeric cells, e.g. {ex}")
        d[c] = v
    for k in KEYS:
        if (d[k] % 1 != 0).any():
            sys.exit(f"{folder}: key column {k} has non-integer values")
        d[k] = d[k].astype(np.int64)
    logger.info("  %-22s %d files, %d rows, %d columns used", folder, len(files), len(d), len(columns))
    return d.copy()  # one block per dtype again after the column-wise conversions


def read_municipios(path: str, layer: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = ["municipio_code", "municipio_name", "unidade_code", "unidade_name", "regiaomeso_code",
                "regiaomeso_name", "regiaomicro_code", "regiaomicro_name", "regiaometropolitana_code",
                "regiaometropolitana_name"]
        return pd.read_sql(f'SELECT {", ".join(cols)} FROM "{layer}" WHERE area_type = \'municipio\'', con)
    finally:
        con.close()


def fmt_frame(df: pd.DataFrame, reals: set, decimals: int) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if not c.startswith("V"):
            continue
        if c in reals:
            out[c] = out[c].round(decimals).map(lambda x: repr(float(x)).removesuffix(".0"))
        else:
            out[c] = out[c].astype(np.int64)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    with open(cfg["spec"], encoding="utf-8") as f:
        spec = json.load(f)
    out_dir = Path(cfg["output_folder"])
    tables = [t["table"] for t in spec["tables"]]
    paths = {t: out_dir / cfg["table_pattern"].format(NAME=t.upper()) for t in tables}
    exist = [str(p) for p in paths.values() if p.exists()]
    if exist and not args.overwrite:
        sys.exit(f"Outputs exist (use --overwrite): {exist}")
    na = cfg["text_no_data"]

    # variable -> (table, set, source column, type)
    var_of, set_cols = {}, defaultdict(list)
    for t in spec["tables"]:
        for v in t["variables"]:
            var_of[v["name"]] = (t["table"], v["set"], v["source_name"], v["type"])
            if v["source_name"] not in set_cols[v["set"]]:  # basico repeats a few totals of other tables
                set_cols[v["set"]].append(v["source_name"])
    reals = {n for n, x in var_of.items() if x[3] == "real"}
    cons = []
    first_table = {}
    for t in spec["tables"]:
        for v in t["variables"]:
            first_table.setdefault(v["set"], t["table"])

    # --- read the four sets ------------------------------------------------------------
    logger.info("Reading %s", cfg["dvd_zip"])
    sets = {}
    with zipfile.ZipFile(cfg["dvd_zip"]) as zf:
        members = zip_members(zf)
        for s, info in spec["sets"].items():
            sets[s] = read_set(zf, members, f'{cfg["data_folder"]}/{info["folder"]}', info["prefix"],
                               set_cols[s], logger)

    # --- duplicates and key coverage ---------------------------------------------------------
    dtb = read_dtb(cfg["dtb_xls"])
    code6 = {c[:6]: c for c in dtb["municipio_code"]}
    keysets = {}
    for s, d in sets.items():
        d["municipio_code"] = (d.UF.astype(str).str.zfill(2) + d.MUN.astype(str).str.zfill(4)).map(code6)
        if d["municipio_code"].isna().any():
            bad = sorted(set(d.loc[d.municipio_code.isna(), "UF"].astype(str) + "-" + d.loc[d.municipio_code.isna(), "MUN"].astype(str)))
            sys.exit(f"{s}: municipalities not in the DTB 1991: {bad[:10]}")
        d["setor_code"] = (d.municipio_code + d.DIST.astype(str).str.zfill(2) + d.SUBD.astype(str).str.zfill(2)
                           + d.SETOR.astype(str).str.zfill(4))
        if (d.setor_code.str.len() != 15).any():
            sys.exit(f"{s}: key parts longer than their width")
        dup = d.duplicated(["setor_code", "SITUACAO", "TIPO"] + set_cols[s], keep="first")
        if dup.any():
            for code, fname in d.loc[dup, ["setor_code", "_file"]].itertuples(index=False):
                cons.append(dict(layer=first_table[s], code=code, issue="duplicate_row", value=fname, action="dropped",
                                 note=f"exact repeat of the tract row in the DVD set {s} (numbers equal, names may differ in spacing)"))
            logger.info("  %s: %d exact duplicate rows dropped", s, int(dup.sum()))
            d = d[~dup]
        if d.setor_code.duplicated().any():
            ex = d.loc[d.setor_code.duplicated(keep=False), "setor_code"].unique()[:10]
            sys.exit(f"{s}: tract codes repeated with different values: {list(ex)}")
        sets[s] = d.set_index("setor_code").sort_index()
        keysets[s] = set(sets[s].index)
    ref = next(iter(keysets))
    for s, k in keysets.items():
        if k != keysets[ref]:
            sys.exit(f"{s} and {ref} hold different tracts ({len(k ^ keysets[ref])} differ)")
    codes = sets[ref].index
    for s, d in sets.items():
        for c in ("SITUACAO", "TIPO"):
            diff = (d[c] != sets[ref][c]).sum()
            if diff:
                sys.exit(f"{s}: {c} differs from {ref} in {diff} tracts")
    no_tracts = sorted(set(dtb["municipio_code"]) - set(sets[ref].municipio_code))
    if no_tracts:
        sys.exit(f"DTB municipalities without tracts: {no_tracts[:10]}")
    logger.info("Tracts: %d in all %d sets, %d municipalities", len(codes), len(sets), sets[ref].municipio_code.nunique())

    # --- values ------------------------------------------------------------------------------
    cols = {}
    for name, (table, s, col, typ) in var_of.items():
        v = sets[s][col]
        if typ == "integer" and (v % 1 != 0).any():
            sys.exit(f"{name} ({s}.{col}) has non-integer values")
        cols[name] = v
    vals = pd.DataFrame(cols, index=codes)
    if vals.isna().any().any():
        sys.exit("missing values after assembling (unexpected)")

    # --- checks ------------------------------------------------------------------------------
    n_find = Counter()

    def finding(mask, table, issue, value_fn, note):
        for code in vals.index[mask]:
            cons.append(dict(layer=table, code=code, issue=issue, value=value_fn(code), action="kept", note=note))
        n_find[issue] += int(mask.sum())

    for chk in spec["checks"].get("sum", []):
        t, parts = chk["total"], chk["parts"]
        got = vals[parts].sum(axis=1)
        bad = got != vals[t]
        if bad.any():
            finding(bad, var_of[t][0], "sum_check_failed", lambda c: f"{' + '.join(parts)} = {got[c]:.0f}, {t} = {vals.at[c, t]:.0f}",
                    "parts do not add up to the total in the source")
    for a, b in spec["checks"].get("equal", []):
        bad = vals[a] != vals[b]
        if bad.any():
            finding(bad, var_of[a][0], "equal_check_failed", lambda c: f"{a} = {vals.at[c, a]:.0f}, {b} = {vals.at[c, b]:.0f}",
                    "variables expected to hold the same value")
    for a, b in spec["checks"].get("le", []):
        bad = vals[a] > vals[b]
        if bad.any():
            finding(bad, var_of[a][0], "literate_exceeds_total", lambda c: f"{a} = {vals.at[c, a]:.0f} > {b} = {vals.at[c, b]:.0f}",
                    "subgroup larger than its total in the source")
    logger.info("Spec checks: %d sum, %d equal, %d le; findings %s", len(spec["checks"].get("sum", [])),
                len(spec["checks"].get("equal", [])), len(spec["checks"].get("le", [])), dict(n_find) or "none")

    # --- names -------------------------------------------------------------------------------
    base = sets["pessoas"] if "pessoas" in sets else sets[ref]
    for level, col, width in (("distrito", "NOMEDISTRI", 9), ("subdistrito", "NOMESUBDIS", 11)):
        per = pd.concat([d[col].rename(s) for s, d in sets.items()], axis=1)
        conflict = per.nunique(axis=1) > 1
        for code in per.index[conflict]:
            cons.append(dict(layer="basico", code=code, issue=f"{level}_name_differs_between_sets",
                             value=" / ".join(f"{s}: {per.at[code, s]}" for s in per.columns), action="kept the Pessoas name",
                             note="name of the same tract differs between DVD sets"))
        unit = pd.DataFrame({"u": base.index.str[:width], "n": base[col]})
        many = unit.groupby("u").n.nunique()
        for u in many[many > 1].index:
            cons.append(dict(layer="basico", code=u, issue=f"{level}_name_varies",
                             value=" / ".join(sorted(unit.loc[unit.u == u, "n"].unique())), action="kept per tract",
                             note=f"tracts of the same {level} carry different names"))
        logger.info("  %s names: %d conflicts between sets, %d %ss with more than one name", level,
                    int(conflict.sum()), int((many > 1).sum()), level)

    # --- municipal totals vs agregados-municipios ------------------------------------------------
    if cfg["aggregates_table"] and Path(cfg["aggregates_table"]).exists():
        agg = pd.read_csv(cfg["aggregates_table"], sep=";", dtype={"municipio_code": str}).set_index("municipio_code")
        mun = base.municipio_code
        for v_agg, v_tr in cfg.get("aggregates_checks", {}).items():
            s = vals[v_tr].groupby(mun.values).sum()
            d = s - agg[v_agg].reindex(s.index)
            bad = d[d != 0]
            for code, x in bad.items():
                cons.append(dict(layer=var_of[v_tr][0], code=code, issue="municipal_total_differs",
                                 value=f"sum of tracts {v_tr} = {s[code]:.0f}, agregados-municipios {v_agg} = {agg.at[code, v_agg]:.0f}",
                                 action="kept", note="tract values summed per municipality"))
            logger.info("Municipal totals: sum of %s equals agregados-municipios %s in %d of %d municipalities",
                        v_tr, v_agg, int((d == 0).sum()), len(d))
    else:
        logger.warning("agregados-municipios not found; municipal totals not checked")

    # --- basico geography ------------------------------------------------------------------------
    muni = read_municipios(cfg["municipios_gpkg"], cfg["municipios_layer"]).set_index("municipio_code")
    miss = sorted(set(base.municipio_code) - set(muni.index))
    if miss:
        sys.exit(f"municipios layer lacks {len(miss)} municipalities: {miss[:10]}")
    geo = pd.DataFrame(index=codes)
    geo["setor_code"] = codes
    m = muni.reindex(base.municipio_code.values)
    for c in ["unidade_code", "unidade_name", "regiaomeso_code", "regiaomeso_name", "regiaomicro_code", "regiaomicro_name",
              "regiaometropolitana_code", "regiaometropolitana_name"]:
        geo[c] = m[c].values
    geo["municipio_code"] = base.municipio_code.values
    geo["municipio_name"] = m["municipio_name"].values
    geo["distrito_code"] = codes.str[:9]
    geo["distrito_name"] = base.NOMEDISTRI.replace("", na).values
    geo["subdistrito_code"] = codes.str[:11]
    geo["subdistrito_name"] = base.NOMESUBDIS.replace("", na).values
    geo["situacao_code"] = base.SITUACAO.astype(str).values
    geo["situacao_type"] = geo.situacao_code.map(cfg["situacao_type"]).fillna(na)
    geo["tipo_code"] = base.TIPO.astype(str).values
    geo = geo[GEO_COLUMNS]

    # --- write -------------------------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    for t in spec["tables"]:
        names = [v["name"] for v in t["variables"]]
        frame = vals[names]
        frame = pd.concat([geo, frame], axis=1) if t["table"] == "basico" else pd.concat([geo[["setor_code"]], frame], axis=1)
        frame = fmt_frame(frame, reals, cfg["real_decimals"])
        tmp = paths[t["table"]].with_name(paths[t["table"]].name + ".part")
        frame.to_csv(tmp, sep=";", index=False, lineterminator="\n", na_rep=na, quoting=csv.QUOTE_MINIMAL)
        tmp.replace(paths[t["table"]])
        logger.info("Wrote %s (%d rows, %d variables)", paths[t["table"]], len(frame), len(names))

    cons_path = out_dir / cfg["consistency_name"]
    kept = merge_consistency(cons_path, cons, tuple(tables))
    issues = Counter(r["issue"] for r in cons)
    logger.info("Consistency: %d rows (+%d kept from other layers) -> %s %s", len(cons), kept, cons_path, dict(issues))

    # --- report: tracts per municipality ------------------------------------------------------------
    rep = pd.DataFrame({"municipio_code": base.municipio_code, "situacao_type": geo.situacao_type,
                        "population": vals[cfg.get("population_variable", "V0114")]})
    rep = rep.groupby("municipio_code").agg(tracts=("population", "size"), population=("population", "sum"),
                                            urban_tracts=("situacao_type", lambda s: int((s == "Urbana").sum())))
    rep.to_csv(report / "tracts_by_municipio.csv", sep=";")
    logger.info("Report: %s", report / "tracts_by_municipio.csv")


if __name__ == "__main__":
    main()

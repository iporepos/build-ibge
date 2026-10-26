"""
Build the IBGE Census 1991 T2 database (one GeoPackage), dedicated to 1991: the sample microdata
are NOT loaded, they are aggregated to the municipality and to the inferred tract.

Layers of the database:
  * geography      the T1 GeoPackages of the layers catalog with theme "geography" (unidades,
                   municipios, sedes), copied with ogr2ogr, layer names without census/year
  * aggregates     the T1 table aggregates_table (agregados-municipios): values published by IBGE,
                   loaded as they are
  * tables         every entry of "tables" (the universe tract tables basico ... instrucao02, the link
                   comparabilidade-2022): T1 CSVs loaded as they are, V columns numeric (INTEGER, or
                   REAL when the data has a fraction), all other columns text; an index on "key",
                   unique unless "unique": false
  * estimates      one table per entry of "estimates", computed here from the T1 microdata table
                   "source", weighted by the household weight ("weight": V0927 of
                   microdados-domicilios, joined to persons by household_code). Kept apart from the
                   published values. The rows ("group") are either
                     municipalities  column municipio_code of the source, one row per municipality of
                                     the aggregates table
                     inferred tracts column setor_code_inferred of the link table (microdados-setores,
                                     by household_code), one row per tract of the units table (basico),
                                     tracts without sample included (n_sample 0)
                   Systematic rules, by the class of the T1 variable (VARIABLES catalog), for every V
                   column not in "exclude":
                     categorical / boolean  V<n>_<code>: estimated units in each category of the
                                            CATEGORIES catalog, V<n>_ign: units with -2 (only when
                                            the data has -2), -9999 (not applicable) is left out
                     quantity               V<n>_n: units with a valid value (>= 0), V<n>_sum and
                                            V<n>_mean: weighted sum and mean (-9999 without valid
                                            value; top-coded quantities stay top-coded)
                     identifier / code      skipped
                   plus extra_categorical columns (e.g. situacao_code) and the columns n_sample (sample
                   records used: households or persons), n_sample_households (person tables: distinct
                   households of those persons) and total (sum of the weights). A code in the data
                   without a category stops the run.
  * catalogs       derived from the T1 catalogs for this database: layers (rows of the tables
                   present, each estimate table after its "after" layer), fields (rows of the fields
                   present, fields_add / fields_update applied), variables (rows of the tables loaded as
                   they are plus one row per estimate column, with the T1 description and category
                   label), categories (rows of the tables loaded as they are), consistency (as in T1).

Checks at the end (log): municipio_code of the attribute tables in municipios, setor_code /
setor_code_inferred in basico; per estimate with a "check", the estimated total against a universe
variable (estimates_vs_universe_<table>.csv in the report folder); SQLite quick_check.

Table names in the database: the T1 layer names with "-" replaced by db_hyphen ("_" by default, e.g.
agregados-municipios -> agregados_municipios), so they need no quoting in SQL; the same names are used in
the catalogs (layers.name, variables.table and variable_code, categories.table, consistency.layer) and in
the descriptions. T1 file names keep the hyphen.

The database is always built from zero: a .part.gpkg next to the target replaces it only when the
build finishes (--overwrite needed when the target exists).

Usage:
    python build_database_1991.py --config build_database_1991.local.json [--overwrite]
"""

import argparse
import csv
import json
import logging
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder", "output_name", "table_pattern",
                 "geography_pattern", "geography_layer_pattern", "catalog_pattern", "aggregates_table",
                 "weight", "estimates"]
NO_DATA = -9999
IGNORED = -2
NA = "NA"
KEY = "municipio_code"
TRACT_KEY = "setor_code"
V_RE = re.compile(r"^V\d+$")
COUNT_COLUMNS = ("n_sample", "n_sample_households")
ROW_BATCH = 5000


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("label", cfg["output_name"])
    cfg.setdefault("tables", [])
    cfg.setdefault("exclude", {})
    cfg.setdefault("extra_categorical", {})
    cfg.setdefault("decimals", 4)
    cfg.setdefault("column_batch", 24)
    cfg.setdefault("fields_add", [])
    cfg.setdefault("fields_update", {})
    cfg.setdefault("db_hyphen", "_")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"database_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_database")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_path)
    return logger


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def read_catalog(cfg: dict, name: str) -> list:
    path = Path(cfg["input_folder"]) / cfg["catalog_pattern"].format(NAME=name.upper())
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def db_name(cfg: dict, name: str) -> str:
    """Table name in the database for a T1 layer name."""
    return name.replace("-", cfg["db_hyphen"])


def table_path(cfg: dict, table: str) -> Path:
    return Path(cfg["input_folder"]) / cfg["table_pattern"].format(NAME=table.upper())


def read_table(cfg: dict, table: str) -> pd.DataFrame:
    """A T1 table as it is: V columns numeric, all other columns text ("NA" kept as text)."""
    df = pd.read_csv(table_path(cfg, table), sep=";", dtype=str, keep_default_na=False)
    for c in df.columns:
        if V_RE.match(c):
            try:
                df[c] = pd.to_numeric(df[c])
            except ValueError as e:
                raise SystemExit(f"{table}: column {c} is not numeric ({e})")
    return df


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------

def read_columns(path: Path, columns: list, text: list = ()) -> pa.Table:
    return pacsv.read_csv(path, parse_options=pacsv.ParseOptions(delimiter=";"),
                          convert_options=pacsv.ConvertOptions(
                              include_columns=columns, column_types={c: pa.string() for c in text}))


def plan_columns(table: str, variables: list, categories: dict, cfg: dict) -> list:
    """[(source column, class, [codes])] in the T1 variables order, extra categoricals first."""
    plan = [(c, "categorical", categories[(table, c)]) for c in cfg["extra_categorical"].get(table, {})]
    skip = set(cfg["exclude"].get(table, []))
    for v in variables:
        if v["table"] != table or v["variable_name"] in skip:
            continue
        cls = v["class"]
        if cls in ("categorical", "boolean"):
            codes = categories.get((table, v["variable_name"]))
            if codes:
                plan.append((v["variable_name"], "categorical", codes))
        elif cls == "quantity":
            plan.append((v["variable_name"], "quantity", None))
    return plan


def aggregate(x: np.ndarray, mi: np.ndarray, w: np.ndarray, m: int, name: str, cls: str, codes,
              errors: list) -> dict:
    """Weighted aggregation of one column to the groups (mi = row -> group index)."""
    out = {}
    ign = x == IGNORED
    if cls == "categorical":
        valid = x >= 0
        order = np.array(sorted(codes))
        pos = np.searchsorted(order, x[valid])
        pos = np.clip(pos, 0, len(order) - 1)
        known = order[pos] == x[valid]
        if not known.all():
            bad = np.unique(x[valid][~known])
            errors.append(f"{name}: codes in the data without a category: {bad[:10].tolist()}")
            return out
        k = len(order)
        cnt = np.bincount(mi[valid] * k + pos, weights=w[valid], minlength=m * k).reshape(m, k)
        for j, code in enumerate(order):
            out[f"{name}_{code}"] = ("estimate_count", code, cnt[:, j])
    else:
        valid = x >= 0
        n = np.bincount(mi[valid], weights=w[valid], minlength=m)
        s = np.bincount(mi[valid], weights=(w * np.where(valid, x, 0))[valid], minlength=m)
        mean = np.divide(s, n, out=np.full(m, float(NO_DATA)), where=n > 0)
        out[f"{name}_n"] = ("estimate_n", None, n)
        out[f"{name}_sum"] = ("estimate_sum", None, np.where(n > 0, s, NO_DATA))
        out[f"{name}_mean"] = ("estimate_mean", None, mean)
    if ign.any():
        out[f"{name}_ign"] = ("estimate_ign", None, np.bincount(mi[ign], weights=w[ign], minlength=m))
    return out


def build_estimates(est: dict, units: list, weights: pd.Series, links: dict, variables: list,
                    categories: dict, cfg: dict, logger) -> tuple:
    """(DataFrame, {column: (class, source column, code)}) for one estimate table.
    units: the group codes (rows of the table); links: {link table: household -> group array
    aligned to weights}."""
    src = table_path(cfg, est["source"])
    wkey = cfg["weight"]["key"]
    group = est.get("group", {"column": KEY})
    gcol = group["column"]
    if "link" in group:
        base = read_columns(src, [wkey], text=[wkey])
    else:
        base = read_columns(src, [gcol, wkey], text=[gcol, wkey])
    pos = weights.index.get_indexer(base.column(wkey).to_numpy(zero_copy_only=False))
    if (pos < 0).any():
        raise SystemExit(f"{est['source']}: {(pos < 0).sum()} rows without a household weight")
    # group index of each row; a link is coded once per household, then picked by the weight position
    gvals = links[group["link"]] if "link" in group else base.column(gcol).to_numpy(zero_copy_only=False)
    del base
    codes = pd.Categorical(gvals, categories=units).codes.astype(np.int64)
    if (codes < 0).any():
        bad = pd.unique(np.asarray(gvals, dtype=object)[codes < 0])[:5].tolist()
        raise SystemExit(f"{est['table']}: {(codes < 0).sum()} rows whose {gcol} is not a row of the "
                         f"table, e.g. {bad}")
    mi = codes[pos] if "link" in group else codes
    del gvals, codes
    w = weights.to_numpy()[pos]
    m = len(units)
    cols = {"n_sample": ("count", None, None, np.bincount(mi, minlength=m))}
    if est["source"] != cfg["weight"]["table"]:
        _, first = np.unique(pos, return_index=True)
        cols["n_sample_households"] = ("count", None, None, np.bincount(mi[first], minlength=m))
    cols["total"] = ("sum_weights", None, None, np.bincount(mi, weights=w, minlength=m))
    plan = plan_columns(est["source"], variables, categories, cfg)
    errors = []
    step = cfg["column_batch"]
    for i in range(0, len(plan), step):
        part = plan[i:i + step]
        t = read_columns(src, [p[0] for p in part])
        for name, cls, codes in part:
            x = t.column(name).to_numpy(zero_copy_only=False)
            if x.dtype.kind not in "iu":
                if cls == "categorical" or x.dtype.kind != "f":
                    errors.append(f"{name}: not numeric in {src.name} ({x.dtype})")
                    continue
            x = x.astype(np.int64) if cls == "categorical" else x.astype(np.float64)
            for col, (ccls, code, values) in aggregate(x, mi, w, m, name, cls, codes, errors).items():
                cols[col] = (ccls, name, code, values)
        logger.debug("  %s: %d / %d source columns", est["table"], min(i + step, len(plan)), len(plan))
    if errors:
        for e in errors:
            logger.error(e)
        raise SystemExit(f"{est['table']}: {len(errors)} errors")
    dec = cfg["decimals"]
    df = pd.DataFrame({gcol: units, **{c: v if c in COUNT_COLUMNS else np.round(v, dec)
                                       for c, (_, _, _, v) in cols.items()}})
    meta = {c: (cls, name, code) for c, (cls, name, code, _) in cols.items()}
    ns = df["n_sample"]
    logger.info("%-34s %6d rows  %4d columns from %d source variables (%d sample records; rows without sample "
                "%d, n_sample median %.0f, p10 %.0f)", est["table"], len(df), len(cols), len(plan), int(ns.sum()),
                int((ns == 0).sum()), ns.median(), ns.quantile(0.1))
    return df, meta


# ---------------------------------------------------------------------------
# Catalogs for the database
# ---------------------------------------------------------------------------

def estimate_variables(est: dict, meta: dict, tvars: dict, labels: dict, extra: dict) -> list:
    rows = []
    unit_pt, unit_en = est["unit_pt"], est["unit_en"]
    for col, (cls, name, code) in meta.items():
        if cls in ("count", "sum_weights"):
            continue
        if (est["source"], name) in tvars:
            src, num, dpt, den = tvars[(est["source"], name)]
        else:
            src, num, (dpt, den) = name, NA, extra[name]
        if cls == "estimate_count":
            lpt, len_ = labels[(est["source"], name, str(code))]
            pt, en = f"{dpt}: {lpt} ({unit_pt}, estimativa)", f"{den}: {len_} ({unit_en}, estimate)"
        elif cls == "estimate_ign":
            pt, en = f"{dpt}: ignorado ou não sabe ({unit_pt}, estimativa)", \
                     f"{den}: ignored or does not know ({unit_en}, estimate)"
        elif cls == "estimate_n":
            pt, en = f"{dpt}: {unit_pt} com valor válido (estimativa)", f"{den}: {unit_en} with a valid value (estimate)"
        elif cls == "estimate_sum":
            pt, en = f"{dpt}: soma ponderada", f"{den}: weighted sum"
        else:
            pt, en = f"{dpt}: média ponderada", f"{den}: weighted mean"
        rows.append(dict(table=est["table"], variable_name=col, source_name=src, dictionary_number=num,
                         **{"class": "estimate_count" if cls == "estimate_ign" else cls},
                         description_pt=pt, description_en=en))
    return rows


def build_catalogs(cfg: dict, t1: dict, metas: dict, present: list, loaded: list, all_columns: set) -> dict:
    # layers: tables present, each estimate table after its "after" layer
    after = {}
    for e in cfg["estimates"]:
        after.setdefault(e.get("after", cfg["aggregates_table"]), []).append(
            dict(name=e["table"], theme=e["theme"], description_pt=e["description_pt"],
                 description_en=e["description_en"]))
    layers = []

    def place(name):
        for row in after.pop(name, []):
            layers.append(row)
            place(row["name"])

    for r in t1["layers"]:
        if r["name"] in present:
            layers.append({k: v for k, v in r.items() if k != "id"})
        place(r["name"])
    if after:
        raise SystemExit(f"estimates placed after unknown layers: {sorted(after)}")
    layers = [dict(id=i, **r) for i, r in enumerate(layers, 1)]

    # variables: rows of the tables loaded as they are + estimate rows
    tvars = {(v["table"], v["variable_name"]): (v["source_name"], v["dictionary_number"], v["description_pt"],
                                                v["description_en"]) for v in t1["variables"]}
    labels = {(c["table"], c["variable_name"], c["code"]): (c["label_pt"], c["label_en"]) for c in t1["categories"]}
    vrows = [{k: v for k, v in r.items() if k not in ("variable_id", "variable_code")}
             for r in t1["variables"] if r["table"] in loaded]
    for e in cfg["estimates"]:
        vrows += estimate_variables(e, metas[e["table"]], tvars, labels,
                                    cfg["extra_categorical"].get(e["source"], {}))
    variables = [dict(table=r["table"], variable_id=i, variable_code=f"{r['table']}_{r['variable_name']}",
                      variable_name=r["variable_name"], source_name=r["source_name"],
                      dictionary_number=r["dictionary_number"], **{"class": r["class"]},
                      description_pt=r["description_pt"], description_en=r["description_en"])
                 for i, r in enumerate(vrows, 1)]

    # categories: rows of the tables loaded as they are (e.g. situacao_code / tipo_code of basico)
    categories = [r for r in t1["categories"] if r["table"] in loaded]

    # fields: the fields present (V prefix kept), additions and updates
    fcols = list(t1["fields"][0].keys())
    frows = [dict(r) for r in t1["fields"] if r["field"] in all_columns or r["field"] == "V"]
    for f in cfg["fields_add"]:
        frows.append(dict(zip(fcols[1:], f)))
    for name, (pt, en) in cfg["fields_update"].items():
        for r in frows:
            if r["field"] == name:
                r["description_pt"], r["description_en"] = pt, en
    fields = [dict(r, id=i) for i, r in enumerate(frows, 1)]
    out = {"layers": layers, "fields": fields, "variables": variables, "consistency": t1["consistency"]}
    if categories:
        out["categories"] = categories
    names = [r["name"] for r in t1["layers"]] + [e["table"] for e in cfg["estimates"]]
    return rename_catalogs(out, {n: db_name(cfg, n) for n in names if db_name(cfg, n) != n})


def rename_catalogs(cats: dict, mapping: dict) -> dict:
    """Layer names of the catalogs (name / table / layer columns and mentions in the texts) as in the database."""
    if not mapping:
        return cats
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in sorted(mapping, key=len, reverse=True))
                     + r")(?![A-Za-z0-9-])")
    out = {}
    for cat, rows in cats.items():
        new = []
        for r in rows:
            r = dict(r)
            for c in ("name", "table", "layer"):
                if c in r and r[c] in mapping:
                    r[c] = mapping[r[c]]
            for c in ("description_pt", "description_en", "note"):
                if c in r and r[c]:
                    r[c] = pat.sub(lambda m: mapping[m.group(1)], r[c])
            if cat == "variables":
                r["variable_code"] = f"{r['table']}_{r['variable_name']}"
            new.append(r)
        out[cat] = new
    return out


# ---------------------------------------------------------------------------
# GeoPackage
# ---------------------------------------------------------------------------

def register(con, table: str):
    con.execute("INSERT OR REPLACE INTO gpkg_contents (table_name, data_type, identifier, description, "
                "last_change) VALUES (?,?,?,?,?)",
                (table, "attributes", table, "", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")))


def add_geography(dst: Path, name: str, cfg: dict, logger):
    src = Path(cfg["input_folder"]) / cfg["geography_pattern"].format(NAME=name.upper())
    if not src.exists():
        raise SystemExit(f"{name}: {src} not found")
    cmd = ["ogr2ogr", "-f", "GPKG", str(dst), str(src), cfg["geography_layer_pattern"].format(name=name),
           "-nln", db_name(cfg, name), "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
    if dst.exists():
        cmd.insert(3, "-update")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"{name}: ogr2ogr failed: {res.stderr.strip()[:600]}")
    con = sqlite3.connect(dst)
    n = con.execute(f"SELECT COUNT(*) FROM {q(db_name(cfg, name))}").fetchone()[0]
    con.close()
    logger.info("%-34s %6d features (geography)", db_name(cfg, name), n)


def sql_type(s: pd.Series) -> str:
    if s.dtype.kind in "iu":
        return "INTEGER"
    return "REAL" if s.dtype.kind == "f" else "TEXT"


def write_table(dst: Path, name: str, rows, columns: list, types: list, key: str = None, unique: bool = True):
    con = sqlite3.connect(dst)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        cols = ", ".join(f"{q(c)} {t}" for c, t in zip(columns, types))
        con.execute(f"CREATE TABLE {q(name)} (fid INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        sql = f"INSERT INTO {q(name)} ({', '.join(q(c) for c in columns)}) VALUES ({', '.join('?' * len(columns))})"
        for batch in rows:
            con.executemany(sql, batch)
        if key:
            kind = "UNIQUE INDEX" if unique else "INDEX"
            con.execute(f"CREATE {kind} {q(('ux_' if unique else 'ix_') + name + '_' + key)} ON {q(name)} ({q(key)})")
        register(con, name)
        con.commit()
    finally:
        con.close()


def frame_batches(df: pd.DataFrame):
    """Row batches of a DataFrame as python values (None for missing), to keep memory low on wide tables."""
    for i in range(0, len(df), ROW_BATCH):
        part = df.iloc[i:i + ROW_BATCH]
        yield list(part.astype(object).where(part.notna(), None).itertuples(index=False, name=None))


def write_frame(dst: Path, name: str, df: pd.DataFrame, logger, key: str = KEY, unique: bool = True):
    types = [sql_type(df[c]) for c in df.columns]
    write_table(dst, name, frame_batches(df), list(df.columns), types, key, unique)
    nreal = sum(1 for c, t in zip(df.columns, types) if V_RE.match(c) and t == "REAL")
    logger.info("%-34s %6d rows  %4d columns%s", name, len(df), len(df.columns),
                f"  ({nreal} REAL variables)" if nreal else "")


def write_catalog(dst: Path, name: str, rows: list, logger):
    columns = list(rows[0].keys())
    types = ["INTEGER" if c in ("id", "variable_id", "scale") and all(str(r[c]).lstrip("-").isdigit() for r in rows)
             else "TEXT" for c in columns]
    conv = [int if t == "INTEGER" else str for t in types]
    write_table(dst, name, [[[f(r[c]) for f, c in zip(conv, columns)] for r in rows]], columns, types)
    logger.info("%-34s %6d rows  (catalog)", name, len(rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="rebuild from zero over an existing database")
    args = ap.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    out_dir = Path(cfg["output_folder"])
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / cfg["output_name"]
    part = out_dir / (final.stem + ".part.gpkg")
    if final.exists() and not args.overwrite:
        sys.exit(f"{final} exists — use --overwrite to rebuild from zero")
    if part.exists():
        part.unlink()

    t1 = {n: read_catalog(cfg, n) for n in ("layers", "fields", "variables", "categories", "consistency")}
    categories = {}
    for c in t1["categories"]:
        categories.setdefault((c["table"], c["variable_name"]), []).append(int(c["code"]))
    geo = [r["name"] for r in t1["layers"] if r["theme"] == "geography"]
    agg_name = cfg["aggregates_table"]
    logger.info("=" * 60)
    logger.info("%s — T2 database: %d geography layers, %s, %d tables, %d estimate tables",
                cfg["label"], len(geo), agg_name, len(cfg["tables"]), len(cfg["estimates"]))
    logger.info("Target: %s", final)
    logger.info("=" * 60)

    # --- read and compute first (nothing is written if anything fails) -----------------------
    agg = pd.read_csv(table_path(cfg, agg_name), sep=";", dtype={KEY: str})
    tables = {t["name"]: read_table(cfg, t["name"]) for t in cfg["tables"]}
    units = {KEY: agg[KEY].tolist()}
    for t in cfg["tables"]:
        if t.get("units"):
            units[t["units"]] = tables[t["name"]][t["key"]].tolist()
    wt = cfg["weight"]
    wdf = read_columns(table_path(cfg, wt["table"]), [wt["key"], wt["column"]], text=[wt["key"]]).to_pandas()
    weights = wdf.set_index(wt["key"])[wt["column"]].astype(float)
    if weights.index.duplicated().any() or (weights <= 0).any():
        raise SystemExit("weights: duplicated keys or non-positive weights")
    del wdf
    links = {}
    for e in cfg["estimates"]:
        g = e.get("group", {})
        if "link" in g and g["link"] not in links:
            lk = read_columns(table_path(cfg, g["link"]), [wt["key"], g["column"]],
                              text=[wt["key"], g["column"]]).to_pandas().set_index(wt["key"])[g["column"]]
            if lk.index.duplicated().any():
                raise SystemExit(f"{g['link']}: duplicated {wt['key']}")
            arr = lk.reindex(weights.index).to_numpy(dtype=object)
            missing = pd.isna(arr) | (arr == NA)
            if missing.any():
                raise SystemExit(f"{g['link']}: {int(missing.sum())} households without {g['column']}")
            links[g["link"]] = arr
            logger.info("%-34s %6d households linked to %s", g["link"], len(lk), g["column"])
    frames, metas = {}, {}
    for e in cfg["estimates"]:
        gcol = e.get("group", {}).get("column", KEY)
        unit_name = e.get("group", {}).get("units", KEY)
        frames[e["table"]], metas[e["table"]] = build_estimates(e, units[unit_name], weights, links,
                                                                t1["variables"], categories, cfg, logger)
        e["_key"] = gcol

    # --- database ----------------------------------------------------------------------------
    for name in geo:
        add_geography(part, name, cfg, logger)
    write_frame(part, db_name(cfg, agg_name), agg, logger)
    for t in cfg["tables"]:
        write_frame(part, db_name(cfg, t["name"]), tables[t["name"]], logger, key=t["key"],
                    unique=t.get("unique", True))
    for e in cfg["estimates"]:
        write_frame(part, db_name(cfg, e["table"]), frames[e["table"]], logger, key=e["_key"])
    loaded = [agg_name] + list(tables)
    present = geo + loaded + list(frames) + ["layers", "fields", "variables", "categories", "consistency"]
    con = sqlite3.connect(part)
    all_columns = {r[1] for t in geo for r in con.execute(f"PRAGMA table_info({q(db_name(cfg, t))})")}
    con.close()
    all_columns |= set(agg.columns) | {c for df in tables.values() for c in df.columns}
    all_columns |= {c for df in frames.values() for c in df.columns}
    all_columns |= {c for n in ("layers", "fields", "variables", "categories", "consistency") for c in t1[n][0].keys()}
    cats = build_catalogs(cfg, t1, metas, present, loaded, all_columns)
    for name, rows in cats.items():
        write_catalog(part, name, rows, logger)

    # --- checks ------------------------------------------------------------------------------
    logger.info("-" * 60)
    con = sqlite3.connect(part)
    try:
        names = [db_name(cfg, n) for n in [n for n in geo if n != "municipios"] + loaded + list(frames)]
        for t in names:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({q(t)})")]
            for key, ref in ((KEY, "municipios"), (TRACT_KEY, "basico"), ("setor_code_inferred", "basico"),
                             ("setor_code_1991", "basico")):
                if key not in cols or (t == ref) or ref not in present:
                    continue
                refkey = KEY if ref == "municipios" else TRACT_KEY
                miss = con.execute(f"SELECT COUNT(*) FROM {q(t)} WHERE {q(key)} <> 'NA' AND {q(key)} NOT IN "
                                   f"(SELECT {q(refkey)} FROM {q(ref)})").fetchone()[0]
                (logger.warning if miss else logger.info)("  %-34s %s", t, f"{miss} {key} not in {ref}"
                                                          if miss else f"all {key} in {ref}")
        logger.info("quick_check: %s", con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()
    for e in cfg["estimates"]:
        chk = e.get("check")
        if not chk:
            continue
        src = agg if chk["table"] == agg_name else tables[chk["table"]]
        ref_key = KEY if chk["table"] == agg_name else TRACT_KEY
        uni = src.set_index(ref_key)[chk["variable"]].astype(float)
        df = frames[e["table"]]
        cmp = pd.DataFrame({e["_key"]: df[e["_key"]], "n_sample": df["n_sample"],
                            "universe": uni.reindex(df[e["_key"]]).to_numpy(), "estimated": df["total"]})
        cmp["diff_pct"] = (100 * (cmp["estimated"] / cmp["universe"].where(cmp["universe"] > 0) - 1)).round(3)
        cmp.to_csv(report / f"estimates_vs_universe_{db_name(cfg, e['table'])}.csv", sep=";", index=False)
        d = cmp["diff_pct"].abs()
        logger.info("%-34s total %.0f vs universe %s.%s %.0f; |diff| median %.2f%%, %d of %d rows over 10%%, "
                    "corr %.4f", e["table"], cmp["estimated"].sum(), chk["table"], chk["variable"],
                    cmp["universe"].sum(), d.median(), int((d > 10).sum()), len(cmp),
                    cmp["estimated"].corr(cmp["universe"]))

    if final.exists():
        final.unlink()
    part.rename(final)
    logger.info("=" * 60)
    logger.info("Wrote %s (%.2f GB)", final, final.stat().st_size / 1e9)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

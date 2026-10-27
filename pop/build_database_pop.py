"""
Build the IBGE population (POP) T2 database: one GeoPackage of attribute tables (no geometry; join to the DTB
municipios layer of the same year by municipio_code), merging the per-year T1 files into few tables.

Tables:
  municipios  the T1 censos-municipios and estimativas-municipios files in one long table, plus the
              interpolated years. Columns: year, municipio_code, municipio_name, unidade_code, source,
              series, population, population_urban, population_rural, population_free,
              population_enslaved, note_mark, interpolation. Unique on (year, municipio_code, source).
                source  the flag of the value: censo, estimativa (T1 values as published) or one of the
                        interpolated kinds below
                series  1 for the one row per (year, municipio_code) of the annual series: the census
                        when the year has one, else the estimate, else the interpolated value; 0 otherwise
                        (e.g. the 2000 estimate, next to the 2000 census)
              unidade_code of census rows = first two digits of the code; columns a source does not have
              are -9999 (numbers) / NA (text).
  unidades    the T1 estimativas-unidades files (published totals of Brasil, regions and UFs; no
              interpolation)
  notas       the T1 estimativas-notas files
  layers, fields, consistency   catalogs: layers from the config (the tables of this database), fields =
              T1 FIELDS plus fields_add, consistency = T1 CONSISTENCY

Interpolation (config "interpolation"): for each listed year y, the anchors are the nearest series years
a < y < b. Values keep the territory of the earlier anchor a (the municipalities of year a), with
method "geometric" (P_a * (P_b / P_a) ** ((y - a) / (b - a)); linear when an anchor is 0) or "linear",
rounded to integers:
  interpolado            same code in a and b, no municipality created from it between a and b
  interpolado_territorio the code lost territory to municipalities created between a and b whose only
                         parent it is: its b value is summed with theirs (same territory as in a)
  interpolado_rateio     a municipality created between a and b has several parents: the whole group
                         (parents and children, connected) is interpolated and shared among the parents
                         by their population in a
Parents of a created municipality come from the DTB genealogia ("desmembrado", reference codes resolved to
the trajectory code when the code of the reference year is not in a), else from the DTB linhagem
("split" edges). The interpolation column documents each value: method, anchors and, for the territory
kinds, the codes added. A created municipality without a known parent, or a code of a missing from b,
stops the run (the value would be wrong).

Checks (the run stops, exit 1): unique (year, municipio_code, source); exactly one series row per (year,
municipio_code); every interpolation year has both anchors; per interpolated year the national total lies
between the anchor totals (otherwise a warning). SQLite quick_check at the end. Report:
interpolation_<year>.csv (every interpolated row with its anchor values) in report_folder.

CONFIG (JSON, --config) — required: input_folder, output_folder, output_name, report_folder, inputs,
catalogs, layers
  input_folder      the T1 folder (per-year CSVs and catalogs)
  output_folder     the T2 folder; created if missing
  output_name       database file name, e.g. IBGE_POP_BD_BRASIL_1872-2026.gpkg
  report_folder     log and interpolation reports
  inputs            {"censos": pattern, "estimativas": pattern, "unidades": pattern, "notas": pattern}: glob
                    patterns of the per-year T1 files, relative to input_folder
  catalogs          {"fields": file, "consistency": file} in input_folder
  layers            [{"name", "theme", "description_pt", "description_en"}]: the layers catalog of the database
Optional (defaults in brackets):
  interpolation     {"years": [...], "method": "geometric" | "linear"} [no interpolation]
  genealogy_csv     DTB T1 genealogia (needed when interpolation spans a created municipality)
  lineage_csv       DTB T1 linhagem (fallback for parents after the genealogy, e.g. 2013 municipalities)
  fields_add        rows appended to the fields catalog (same columns as FIELDS) [[]]

The database is always built from zero: a .part.gpkg next to the target replaces it only when the build
finishes (--overwrite needed when the target exists).

Usage:
    python build_database_pop.py --config build_database_pop.local.json [--overwrite]
"""

import argparse
import csv
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyogrio

REQUIRED_KEYS = ["input_folder", "output_folder", "output_name", "report_folder", "inputs", "catalogs", "layers"]
NA = "NA"
NODATA = -9999
MUNI_COLUMNS = ["year", "municipio_code", "municipio_name", "unidade_code", "source", "series", "population",
                "population_urban", "population_rural", "population_free", "population_enslaved", "note_mark",
                "interpolation"]
TEXT = {"municipio_code", "municipio_name", "unidade_code", "source", "note_mark", "interpolation", "level", "code",
        "name", "mark", "text"}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("interpolation", {"years": []})
    cfg["interpolation"].setdefault("method", "geometric")
    if cfg["interpolation"]["method"] not in ("geometric", "linear"):
        sys.exit("interpolation.method must be geometric or linear")
    cfg.setdefault("fields_add", [])
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"database_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("pop_database")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def read_years(folder: Path, pattern: str, logger) -> pd.DataFrame:
    paths = sorted(folder.glob(pattern))
    if not paths:
        sys.exit(f"No T1 file matches {folder / pattern}")
    d = pd.concat([pd.read_csv(p, sep=";", dtype=str, keep_default_na=False) for p in paths], ignore_index=True)
    logger.info("  %-40s %3d files, %7d rows", pattern, len(paths), len(d))
    return d


def typed(d: pd.DataFrame) -> pd.DataFrame:
    for c in d.columns:
        if c not in TEXT:
            d[c] = d[c].astype("int64")
    return d


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------

def interp(pa: int, pb: int, t: float, method: str) -> int:
    if method == "geometric" and pa > 0 and pb > 0:
        return int(round(pa * (pb / pa) ** t))
    return int(round(pa + (pb - pa) * t))


def parent_map(cfg: dict, created: set, codes_a: set) -> dict:
    """{created code: set of parent codes of year a}."""
    parents = defaultdict(set)
    if cfg.get("genealogy_csv"):
        g = pd.read_csv(cfg["genealogy_csv"], sep=";", dtype=str, keep_default_na=False)
        trajectory = dict(zip(g["municipio_code"], g["trajectory_code"]))
        rel =g[g["trajectory_code"].isin(created) & (g["relation"] == "desmembrado")]
        for c, ref in zip(rel["trajectory_code"], rel["reference_code"]):
            for p in (x.strip() for x in ref.split("|")):
                p = p if p in codes_a else trajectory.get(p, p)
                if p != c:
                    parents[c].add(p)
    if cfg.get("lineage_csv"):
        lin = pd.read_csv(cfg["lineage_csv"], sep=";", dtype=str, keep_default_na=False)
        lin = lin[lin["code_to"].isin(created - set(parents)) & (lin["relation"] == "split") & (lin["code_from"] != lin["code_to"])]
        for c, p in zip(lin["code_to"], lin["code_from"]):
            parents[c].add(p)
    return parents


def interpolate_year(y: int, series: pd.DataFrame, cfg: dict, logger) -> pd.DataFrame:
    method = cfg["interpolation"]["method"]
    years = sorted(series["year"].unique())
    before, after = [x for x in years if x < y], [x for x in years if x > y]
    if not before or not after:
        sys.exit(f"interpolation year {y}: no series year on both sides")
    a, b = before[-1], after[0]
    t = (y - a) / (b - a)
    A = series[series["year"] == a].set_index("municipio_code")
    B = series[series["year"] == b].set_index("municipio_code")
    gone = sorted(set(A.index) - set(B.index))
    if gone:
        sys.exit(f"interpolation {y}: codes of {a} missing from {b}: {gone}")
    created = set(B.index) - set(A.index)
    parents = parent_map(cfg, created, set(A.index)) if created else {}
    unknown = sorted(c for c in created if not parents.get(c) or not parents[c] <= set(A.index))
    if unknown:
        sys.exit(f"interpolation {y}: municipalities created between {a} and {b} without a known parent in {a}: {unknown}")
    # groups: parents connected through shared children
    group = {c: c for c in A.index}

    def find(c):
        while group[c] != c:
            group[c] = group[group[c]]
            c = group[c]
        return c
    for c, ps in parents.items():
        ps = sorted(ps)
        for p in ps[1:]:
            group[find(p)] = find(ps[0])
    children = defaultdict(list)
    for c, ps in parents.items():
        children[find(next(iter(ps)))].append(c)
    members = defaultdict(list)
    for c in A.index:
        members[find(c)].append(c)

    rows = []
    for root, ms in members.items():
        kids = sorted(children.get(root, []))
        pa = {m: int(A.at[m, "population"]) for m in ms}
        pb = {m: int(B.at[m, "population"]) for m in ms}
        kb = sum(int(B.at[k, "population"]) for k in kids)
        base = f"{method} {a}-{b}"
        if not kids:
            vals = {m: (interp(pa[m], pb[m], t, method), "interpolado", base) for m in ms}
        elif len(ms) == 1:
            m = ms[0]
            vals = {m: (interp(pa[m], pb[m] + kb, t, method), "interpolado_territorio",
                        f"{base}; {b} + {', '.join(kids)}")}
        else:
            total = interp(sum(pa.values()), sum(pb.values()) + kb, t, method)
            sa = sum(pa.values())
            note = f"{base}; group {', '.join(sorted(ms))} + {', '.join(kids)}, shared by {a} population"
            vals = {m: (int(round(total * pa[m] / sa)) if sa else 0, "interpolado_rateio", note) for m in ms}
        for m, (v, kind, note) in vals.items():
            rows.append(dict(year=y, municipio_code=m, municipio_name=A.at[m, "municipio_name"],
                             unidade_code=A.at[m, "unidade_code"], source=kind, series=1, population=v,
                             population_urban=NODATA, population_rural=NODATA, population_free=NODATA,
                             population_enslaved=NODATA, note_mark=NA, interpolation=note,
                             anchor_a=pa[m], anchor_b=pb[m]))
    d = pd.DataFrame(rows).sort_values("municipio_code")
    ta, tb, ty = int(A["population"].sum()), int(B["population"].sum()), int(d["population"].sum())
    kinds = d["source"].value_counts().to_dict()
    logger.info("  %d: anchors %d/%d, %d municipalities (%d created between them), total %s (anchors %s / %s), %s",
                y, a, b, len(d), len(created), f"{ty:,}", f"{ta:,}", f"{tb:,}", kinds)
    if not min(ta, tb) <= ty <= max(ta, tb):
        logger.warning("  %d: interpolated total outside the anchor totals", y)
    return d


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_municipios(cfg: dict, t1: Path, logger) -> pd.DataFrame:
    c = typed(read_years(t1, cfg["inputs"]["censos"], logger))
    e = typed(read_years(t1, cfg["inputs"]["estimativas"], logger))
    c = c.assign(unidade_code=c["municipio_code"].str[:2], source="censo", note_mark=NA)
    e = e.assign(source="estimativa", population_urban=NODATA, population_rural=NODATA, population_free=NODATA,
                 population_enslaved=NODATA)
    m = pd.concat([c, e], ignore_index=True).assign(interpolation=NA)
    # a year with a census takes all its series values from the census
    census_years = set(c["year"])
    m["series"] = ((m["source"] == "censo") | ~m["year"].isin(census_years)).astype("int64")
    left = m[(m["source"] == "estimativa") & m["year"].isin(census_years)]
    if len(left):
        logger.info("  estimates of census years kept with series 0: %s", left.groupby("year").size().to_dict())
    series = m[m["series"] == 1]
    report = Path(cfg["report_folder"])
    for y in sorted(cfg["interpolation"]["years"]):
        if y in set(series["year"]):
            sys.exit(f"interpolation year {y} already has values")
        d = interpolate_year(y, series, cfg, logger)
        d.to_csv(report / f"interpolation_{y}.csv", sep=";", index=False)
        m = pd.concat([m, d.drop(columns=["anchor_a", "anchor_b"])], ignore_index=True)
    m = m[MUNI_COLUMNS].sort_values(["year", "municipio_code", "series"], ascending=[True, True, False]).reset_index(drop=True)
    dup = m.duplicated(["year", "municipio_code", "source"]).sum()
    one = m[m["series"] == 1].groupby(["year", "municipio_code"]).size()
    every = m.groupby(["year", "municipio_code"]).size()
    if dup or (one != 1).any() or len(one) != len(every):
        sys.exit(f"municipios: {dup} duplicated keys, {(one != 1).sum()} keys with several series rows, "
                 f"{len(every) - len(one)} keys without a series row")
    logger.info("  municipios: %d rows, %d series rows, sources %s", len(m), int(m["series"].sum()),
                m["source"].value_counts().to_dict())
    return m


def catalog(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
    for c in ("id", "scale"):
        if c in d and d[c].str.fullmatch(r"-?\d+").all():
            d[c] = d[c].astype("int64")
    return d


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Rebuild over an existing database")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    t1, out = Path(cfg["input_folder"]), Path(cfg["output_folder"])
    target = out / cfg["output_name"]
    if target.exists() and not args.overwrite:
        sys.exit(f"{target} exists (use --overwrite)")
    out.mkdir(parents=True, exist_ok=True)

    logger.info("Reading %s", t1)
    tables = {"municipios": build_municipios(cfg, t1, logger),
              "unidades": typed(read_years(t1, cfg["inputs"]["unidades"], logger)),
              "notas": typed(read_years(t1, cfg["inputs"]["notas"], logger))}
    layers = pd.DataFrame(cfg["layers"])
    layers.insert(0, "id", range(1, len(layers) + 1))
    fields = catalog(t1 / cfg["catalogs"]["fields"])
    if cfg["fields_add"]:
        add = pd.DataFrame(cfg["fields_add"]).astype(str)
        add.insert(0, "id", range(int(fields["id"].max()) + 1, int(fields["id"].max()) + 1 + len(add)))
        fields = pd.concat([fields, add[fields.columns]], ignore_index=True)
    tables.update(layers=layers, fields=fields, consistency=catalog(t1 / cfg["catalogs"]["consistency"]))
    missing = sorted(set(tables) - set(layers["name"]))
    if missing:
        sys.exit(f"tables without a row in the layers config: {missing}")

    part = target.with_name(target.stem + ".part.gpkg")
    if part.exists():
        part.unlink()
    for name, d in tables.items():
        pyogrio.write_dataframe(d, part, layer=name, driver="GPKG", append=part.exists())
        logger.info("  %-12s %7d rows %2d columns", name, len(d), len(d.columns))
    con = sqlite3.connect(part)
    con.execute("CREATE UNIQUE INDEX municipios_key ON municipios (year, municipio_code, source)")
    con.execute("CREATE INDEX municipios_code ON municipios (municipio_code, year)")
    con.execute("CREATE INDEX municipios_series ON municipios (year, series)")
    con.execute("CREATE UNIQUE INDEX unidades_key ON unidades (year, level, code)")
    con.commit()
    ok = con.execute("PRAGMA quick_check").fetchone()[0]
    con.close()
    logger.info("quick_check: %s", ok)
    if ok != "ok":
        sys.exit(1)
    part.replace(target)
    logger.info("Wrote %s (%.1f MB)", target, target.stat().st_size / 1e6)


if __name__ == "__main__":
    main()

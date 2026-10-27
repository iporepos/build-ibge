"""
Build the T2 database of a dataset whose T1 is a set of attribute tables (PNSB-<year>, ATLASSAN): one
GeoPackage with one table per T1 data file, the plotting geography of the dataset (T1 gpkgs from
geography_from_dtb_editions.py, copied with their geometry), plus the T1 catalogs. Values are kept as in T1; columns are typed from the FIELDS catalog.

Tables:
  <layer>      one per T1 data file matching data_pattern; the name comes from layer_template filled with
               the named groups of the pattern (lowercase), e.g. IBGE_PNSB-2017_T7460_BRASIL_2017.csv -> t7460,
               IBGE_ATLASSAN_CAP05-MUNICIPIOS_BRASIL_2008.csv -> cap05_municipios_2008
  <geo layer>  one per T1 gpkg matching geo_pattern, named by geo_template (e.g. geo_municipios), with its
               geometry; its T1 LAYERS row is found by geo_lookup
  <catalog>    every T1 catalog (*_X_X.csv) as a table named after it (variables, tables, categories,
               consistency, fields); layers = the T2 layers catalog (below), not the T1 one
  layers       one row per table of the database: the theme and descriptions of the T1 LAYERS row of the
               table (looked up by layers_lookup, else layers_default), then the catalogs

Types: a column named in FIELDS with dtype_stored text stays text (codes, names); other columns are integer
when every value is an integer, real when every value is a number, else text. FIELDS rows that list several
columns ("a, b, c") describe each of them; field_patterns ({regex: FIELDS field}) cover the generated value
columns (v<variable>_<category>, as<code>).

Checks (exit 1): every T1 CSV is a data file or a catalog; every data table has a T1 LAYERS row (or the
default); every column of every table is described in FIELDS (by name or pattern); the key column (first of
keys found in the table) is unique and has no NA; rows in the database = rows of the CSV; SQLite quick_check.
The database is always built from zero: <name>.part.gpkg replaces the target only when complete.

CONFIG (JSON, --config)
  required:
    input_folder    the T1 folder (data files and catalogs)
    output_folder   the T2 folder (created if missing)
    output_name     database file name, e.g. IBGE_PNSB-2017_BD_BRASIL_2017.gpkg
    report_folder   log folder
    data_pattern    regex (full match) of the T1 data file names, with named groups for layer_template
    layer_template  table name from the groups, e.g. "t{code}" or "{name}_{year}"
    layers_catalog  T1 LAYERS file in input_folder
    fields_catalog  T1 FIELDS file in input_folder
  optional:
    layers_lookup   T1 LAYERS name of a table from the groups (default: layer_template)
    layers_default  T1 LAYERS row used when the lookup finds none, filled from the groups like the template
                    (default: none, missing row fails)
    catalog_suffix  suffix of the catalog files (default "_X_X.csv"); catalog table = the word before it
    geo_pattern     regex (full match) of the T1 gpkg names, named groups for geo_template (default: no gpkg;
                    a gpkg in input_folder then fails the check)
    geo_template    table name of a gpkg, e.g. "geo_{name}" (default "geo_{name}")
    geo_lookup      T1 LAYERS name of a gpkg (default "geo-{name}")
    keys            candidate key columns (default ["municipio_code", "unidade_code",
                    "regiaointermediaria_code", "pais_code"])
    field_patterns  {regex: FIELDS field} for generated column names (default {})

Usage:
    python build_database_tables.py --config build_database_pnsb_2017.local.json [--overwrite]
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyogrio

REQUIRED_KEYS = ["input_folder", "output_folder", "output_name", "report_folder", "data_pattern", "layer_template",
                 "layers_catalog", "fields_catalog"]
INTEGER = re.compile(r"-?\d+")
REAL = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("layers_lookup", cfg["layer_template"])
    cfg.setdefault("layers_default", None)
    cfg.setdefault("catalog_suffix", "_X_X.csv")
    cfg.setdefault("keys", ["municipio_code", "unidade_code", "regiaointermediaria_code", "pais_code"])
    cfg.setdefault("field_patterns", {})
    cfg.setdefault("geo_pattern", None)
    cfg.setdefault("geo_template", "geo_{name}")
    cfg.setdefault("geo_lookup", "geo-{name}")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"database_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("tables_database")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def typed(d: pd.DataFrame, text: set) -> pd.DataFrame:
    """Columns of text fields stay text; others integer / real when every value is one, else text."""
    out = {}
    for c in d.columns:
        s = d[c]
        if c in text:
            out[c] = s
        elif s.str.fullmatch(INTEGER).all() and s.str.len().max() <= 18:
            out[c] = s.astype("int64")
        elif s.str.fullmatch(REAL).all():
            out[c] = s.astype("float64")
        else:
            out[c] = s
    return pd.DataFrame(out)


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

    read = lambda p: pd.read_csv(p, sep=";", dtype=str, keep_default_na=False)
    t1_layers = read(t1 / cfg["layers_catalog"]).set_index("name")
    fields = read(t1 / cfg["fields_catalog"])
    described = {n.strip(): dt for f, dt in zip(fields["field"], fields["dtype_stored"]) for n in f.split(",")}
    text = {n for n, dt in described.items() if dt == "text"}
    patterns = [(re.compile(rx), f) for rx, f in cfg["field_patterns"].items()]
    pattern = re.compile(cfg["data_pattern"])

    errors, data, catalogs = [], {}, {}
    for p in sorted(t1.glob("*.csv")):
        m = pattern.fullmatch(p.name)
        if m:
            groups = {k: v.lower() for k, v in m.groupdict().items()}
            data[cfg["layer_template"].format(**groups)] = (p, cfg["layers_lookup"].format(**groups), groups)
        elif p.name.endswith(cfg["catalog_suffix"]):
            catalogs[p.name[: -len(cfg["catalog_suffix"])].rsplit("_", 1)[-1].lower()] = p
        else:
            errors.append(f"{p.name}: neither a data file ({cfg['data_pattern']}) nor a catalog")
    geo = re.compile(cfg["geo_pattern"]) if cfg["geo_pattern"] else None
    for p in sorted(t1.glob("*.gpkg")):
        m = geo.fullmatch(p.name) if geo else None
        if not m:
            errors.append(f"{p.name}: gpkg not matching geo_pattern")
            continue
        groups = {k: v.lower() for k, v in m.groupdict().items()}
        data[cfg["geo_template"].format(**groups)] = (p, cfg["geo_lookup"].format(**groups), groups)
    logger.info("%s: %d data files (%d gpkg), catalogs %s", t1, len(data),
                sum(p.suffix == ".gpkg" for p, _, _ in data.values()), sorted(catalogs))

    layer_rows, tables = [], {}
    for name, (p, look, groups) in sorted(data.items()):
        row = look if look in t1_layers.index else (cfg["layers_default"] or "").format(**groups)
        if row not in t1_layers.index:
            errors.append(f"{p.name}: no LAYERS row {look!r}")
            continue
        spatial = p.suffix == ".gpkg"
        d = pyogrio.read_dataframe(p) if spatial else read(p)
        undescribed = [c for c in d.columns if c != "geometry" and c not in described and not any(rx.fullmatch(c) for rx, _ in patterns)]
        if undescribed:
            errors.append(f"{p.name}: columns not in FIELDS: {undescribed[:8]}")
        key = next((k for k in cfg["keys"] if k in d.columns), None)
        if key and (d[key].duplicated().any() or (d[key] == "NA").any() or (d[key] == "").any()):
            errors.append(f"{p.name}: key {key} not unique or empty")
        tables[name] = (d if spatial else typed(d, text), key)
        r = t1_layers.loc[row]
        desc_pt, desc_en = r["description_pt"], r["description_en"]
        if row != look:  # default row: say which file the table is
            desc_pt, desc_en = f"{p.name}. {desc_pt}", f"{p.name}. {desc_en}"
        layer_rows.append(dict(name=name, theme=r["theme"], description_pt=desc_pt, description_en=desc_en))
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    for name in sorted(catalogs):
        if name == "layers":
            continue
        r = t1_layers.loc[name] if name in t1_layers.index else None
        layer_rows.append(dict(name=name, theme="metadata", description_pt=r["description_pt"] if r is not None else "NA",
                               description_en=r["description_en"] if r is not None else "NA"))
        tables[name] = (read(catalogs[name]), None)
    layer_rows.append(dict(name="layers", theme="metadata", description_pt="Catálogo das camadas (tabelas) deste banco.",
                           description_en="Catalog of the layers (tables) of this database."))
    layers = pd.DataFrame(layer_rows)
    layers.insert(0, "id", range(1, len(layers) + 1))
    tables["layers"] = (layers, None)

    part = target.with_name(target.stem + ".part.gpkg")
    if part.exists():
        part.unlink()
    for name, (d, _) in tables.items():
        pyogrio.write_dataframe(d, part, layer=name, driver="GPKG", append=part.exists())
        logger.debug("  %-28s %7d rows %4d columns", name, len(d), len(d.columns))
    con = sqlite3.connect(part)
    for name, (d, key) in tables.items():
        n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        if n != len(d):
            errors.append(f"{name}: {n} rows in the database, {len(d)} in T1")
        if key:
            con.execute(f'CREATE UNIQUE INDEX "{name}_key" ON "{name}" ("{key}")')
    con.commit()
    ok = con.execute("PRAGMA quick_check").fetchone()[0]
    con.close()
    logger.info("%d tables (%d data, %d catalogs), quick_check: %s", len(tables), len(data), len(tables) - len(data), ok)
    if errors or ok != "ok":
        for e in errors:
            logger.error(e)
        sys.exit(1)
    part.replace(target)
    logger.info("Wrote %s (%.1f MB)", target, target.stat().st_size / 1e6)


if __name__ == "__main__":
    main()

"""
Build the DTB T2 database: one GeoPackage from the T1 files, one layer per T1 file (snapshots split by year).

Tables (config "tables", one entry per T2 table; names use '_' where T1 uses '-'):
  snapshot layers   several T1 files of the same layer, one per year (municipios, sedes, unidades 1872-2022):
                    one layer per year, <name>_<year> (e.g. municipios_1950), the year taken from the file name
                    (IBGE_DTB_<LAYER>_BRASIL_<year>.gpkg); the columns are those of the T1 file
  single layers     one T1 GeoPackage (pais, regioes, mesorregioes, ... 2022) copied as it is
  attribute tables  one T1 CSV (quadro-municipios, quadro-distritos, quadro-subdistritos, genealogia, linhagem)
  layers, fields, consistency   catalogs: layers = one row per T2 layer from the T1 LAYERS row of its table
                    (description replaced where the config gives one, the snapshot year appended), fields = T1
                    FIELDS, consistency = T1 CONSISTENCY

Every column is typed by the T1 FIELDS catalog (dtype_stored integer / real / text); 'NA' in a numeric column
becomes the field's no_data value (-9999). Codes stay text. Geometries keep their CRS (EPSG:4674); polygons
are written as MultiPolygon, with the GeoPackage spatial index.

Checks (the run stops, exit 1): every column of every table is in FIELDS; every value of a numeric column is a
number or NA; the files of a snapshot layer have the same columns, geometry type and CRS; every key is unique
(rows whose key holds NA are left out of the key: the special areas of the meshes, e.g. disputed areas, have
municipio_code NA); every T2 table has a T1 LAYERS row. Logged: rows per layer, seats without a municipality
polygon of the same year. SQLite quick_check at the end. Report: database_tables.csv (table,
source files, rows, columns, geometry) in report_folder.

CONFIG (JSON, --config) - required: input_folder, output_folder, output_name, report_folder, catalogs, tables
  input_folder      the T1 folder
  output_folder     the T2 folder; created if missing
  output_name       database file name, e.g. IBGE_DTB_BD_BRASIL_1872-2022.gpkg
  report_folder     log and table report
  catalogs          {"layers": file, "fields": file, "consistency": file} in input_folder
  tables            [{"name": T2 name, "t1": T1 LAYERS name, "files": glob pattern in input_folder,
                      optional "key": [columns] (unique index, on every layer of the table), "indexes":
                      [[columns], ...], "description_pt" / "description_en" (replace the T1 text)}]
                    A pattern matching several files makes one layer per file (the 4 digits before the
                    extension give the year); a .csv file makes an attribute table.

The database is always built from zero: a .part.gpkg next to the target replaces it only when the build
finishes (--overwrite needed when the target exists).

Usage:
    python build_database_dtb.py --config build_database_dtb.local.json [--overwrite] [--tables sedes,genealogia]

  --tables takes the table names of the config (sedes = every sedes_<year> layer)
"""

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyogrio

REQUIRED_KEYS = ["input_folder", "output_folder", "output_name", "report_folder", "catalogs", "tables"]
NA = "NA"
CATALOGS = ("layers", "fields", "consistency")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"database_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("dtb_database")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)


def typed(d: pd.DataFrame, fields: dict, where: str) -> pd.DataFrame:
    """Types the columns by FIELDS; NA in numeric columns -> no_data."""
    unknown = [c for c in d.columns if c != "geometry" and c not in fields]
    if unknown:
        sys.exit(f"{where}: columns not in FIELDS: {unknown}")
    for c in d.columns:
        if c == "geometry":
            continue
        kind, nodata = fields[c]
        if kind == "text":
            d[c] = d[c].astype(str).where(d[c].notna(), NA)
            continue
        if d[c].dtype.kind in "if":
            d[c] = d[c].fillna(float(nodata))
            if kind == "integer":
                d[c] = d[c].astype("int64")
            continue
        s = d[c].astype(str).replace({NA: nodata, "": nodata, "nan": nodata, "None": nodata})
        num = pd.to_numeric(s, errors="coerce")
        if num.isna().any():
            sys.exit(f"{where}: column {c} ({kind}) has non-numeric values: {sorted(set(s[num.isna()]))[:5]}")
        if kind == "integer":
            if (num != num.round()).any():
                sys.exit(f"{where}: column {c} (integer) has decimals")
            num = num.astype("int64")
        d[c] = num
    return d


def read_layers(t1: Path, spec: dict, fields: dict) -> list:
    """Returns [(layer name, frame, file, year)]: one per T1 file; a pattern matching several files gives one
    layer per year, named <name>_<year>."""
    paths = sorted(t1.glob(spec["files"]))
    if not paths:
        sys.exit(f"{spec['name']}: no T1 file matches {t1 / spec['files']}")
    if paths[0].suffix == ".csv":
        if len(paths) > 1:
            sys.exit(f"{spec['name']}: a CSV table must come from one file, found {len(paths)}")
        return [(spec["name"], typed(read_csv(paths[0]), fields, spec["name"]), paths[0], None)]
    out, sig = [], None
    for p in paths:
        info = pyogrio.read_info(p)
        s = (tuple(info["fields"]), info["geometry_type"].replace("Multi", ""), info["crs"])
        if sig and s != sig:
            sys.exit(f"{spec['name']}: {p.name} differs from {paths[0].name} (columns, geometry or CRS): {s} vs {sig}")
        sig = s
        year, name = None, spec["name"]
        if len(paths) > 1:
            y = re.search(r"(\d{4})\.gpkg$", p.name)
            if not y:
                sys.exit(f"{spec['name']}: no year in {p.name}")
            year = int(y.group(1))
            name = f"{spec['name']}_{year}"
        out.append((name, typed(pyogrio.read_dataframe(p), fields, name), p, year))
    return out


def check_key(d: pd.DataFrame, key: list, name: str) -> int:
    """Stops on a duplicated key; returns the number of rows left out of the key (NA in a key column)."""
    if not key:
        return 0
    k = d[key]
    has_na = (k.astype(str) == NA).any(axis=1)
    dup = k[~has_na].duplicated(keep=False)
    if dup.any():
        sys.exit(f"{name}: {int(dup.sum())} rows with a duplicated key {key}, e.g. {k[~has_na][dup].head(3).values.tolist()}")
    return int(has_na.sum())


def layers_catalog(t1: Path, cfg: dict, written: list) -> pd.DataFrame:
    """One row per T2 layer: the T1 LAYERS row of its table (or the config text), with the year of a snapshot."""
    src = read_csv(t1 / cfg["catalogs"]["layers"]).set_index("name")
    rows = []
    for name, spec, year in written + [(c, {"name": c, "t1": c}, None) for c in CATALOGS]:
        if spec["t1"] not in src.index:
            sys.exit(f"{spec['name']}: T1 LAYERS has no row {spec['t1']}")
        r = src.loc[spec["t1"]]
        pt, en = spec.get("description_pt", r["description_pt"]), spec.get("description_en", r["description_en"])
        if year:
            pt, en = f"{pt} Levantamento {year}.", f"{en} Snapshot {year}."
        rows.append(dict(name=name, theme=r["theme"], description_pt=pt, description_en=en))
    d = pd.DataFrame(rows)
    d.insert(0, "id", range(1, len(d) + 1))
    return d


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Rebuild over an existing database")
    parser.add_argument("--tables", help="comma-separated subset of tables (test runs; the catalogs are always written)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    t1, out = Path(cfg["input_folder"]), Path(cfg["output_folder"])
    target = out / cfg["output_name"]
    if target.exists() and not args.overwrite:
        sys.exit(f"{target} exists (use --overwrite)")
    specs = cfg["tables"]
    if args.tables:
        want = args.tables.split(",")
        bad = sorted(set(want) - {s["name"] for s in specs})
        if bad:
            sys.exit(f"--tables: unknown {bad}")
        specs = [s for s in specs if s["name"] in want]
    out.mkdir(parents=True, exist_ok=True)

    fcat = read_csv(t1 / cfg["catalogs"]["fields"])
    fields = {r["field"]: (r["dtype_stored"], r["no_data"]) for _, r in fcat.iterrows()}
    bad = sorted({k for k, _ in fields.values()} - {"integer", "real", "text"})
    if bad:
        sys.exit(f"FIELDS: unknown dtype_stored {bad}")

    part = target.with_name(target.stem + ".part.gpkg")
    if part.exists():
        part.unlink()
    report, written, codes = [], [], {}
    logger.info("Reading %s, writing %s", t1, part)
    for spec in specs:
        for name, d, path, year in read_layers(t1, spec, fields):
            key_na = check_key(d, spec.get("key"), name)
            spatial = "geometry" in d
            gtype = d.geom_type.dropna().iloc[0] if spatial else ""
            if gtype.endswith("Polygon"):
                gtype = "MultiPolygon"
            kw = dict(promote_to_multi=True) if gtype == "MultiPolygon" else {}
            pyogrio.write_dataframe(d, part, layer=name, driver="GPKG", append=part.exists(), **kw)
            logger.info("  %-27s %7d rows %2d columns %-12s%s", name, len(d), len(d.columns) - spatial, gtype or "table",
                        f"  {key_na} rows with NA key" if key_na else "")
            report.append(dict(table=name, files=path.name, rows=len(d), columns=len(d.columns) - spatial, geometry=gtype or "NA"))
            written.append((name, spec, year))
            if year and spec["name"] in ("municipios", "sedes"):
                codes[(spec["name"], year)] = set(d["municipio_code"])
    lost = {y: sorted(c - codes.get(("municipios", y), set())) for (n, y), c in codes.items() if n == "sedes"}
    if any(("municipios", y) in codes for y in lost):
        logger.info("  seats without a municipality polygon of the same year: %d", sum(len(v) for y, v in lost.items() if ("municipios", y) in codes))
        for y, v in lost.items():
            if v and ("municipios", y) in codes:
                logger.warning("    sedes_%s: %s have no polygon", y, v)
    layers = layers_catalog(t1, cfg, written)
    for name in CATALOGS:
        d = layers if name == "layers" else typed(read_csv(t1 / cfg["catalogs"][name]), fields, name)
        pyogrio.write_dataframe(d, part, layer=name, driver="GPKG", append=part.exists())
        logger.info("  %-27s %7d rows %2d columns", name, len(d), len(d.columns))
        report.append(dict(table=name, files=cfg["catalogs"][name] if name != "layers" else "config + " + cfg["catalogs"]["layers"],
                           rows=len(d), columns=len(d.columns), geometry="NA"))

    con = sqlite3.connect(part)
    for name, spec, _ in written:
        if spec.get("key"):
            cols = ", ".join(f'"{c}"' for c in spec["key"])
            where = " AND ".join(f"\"{c}\" <> '{NA}'" for c in spec["key"] if fields[c][0] == "text")
            con.execute(f'CREATE UNIQUE INDEX "{name}_key" ON "{name}" ({cols})' + (f" WHERE {where}" if where else ""))
        for i, idx in enumerate(spec.get("indexes", []), 1):
            con.execute(f'CREATE INDEX "{name}_idx{i}" ON "{name}" ({", ".join(chr(34) + c + chr(34) for c in idx)})')
    con.commit()
    ok = con.execute("PRAGMA quick_check").fetchone()[0]
    con.close()
    logger.info("quick_check: %s; %d layers", ok, len(written) + len(CATALOGS))
    with open(Path(cfg["report_folder"]) / "database_tables.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["table", "files", "rows", "columns", "geometry"], delimiter=";")
        w.writeheader()
        w.writerows(report)
    if ok != "ok":
        sys.exit(1)
    part.replace(target)
    logger.info("Wrote %s (%.1f MB)", target, target.stat().st_size / 1e6)


if __name__ == "__main__":
    main()

"""
Build an IBGE Census T2 database (any year: 2000, 2010, 2022...) as a single GeoPackage.

  * geography layers  (theme "geography" in the layers catalog) are copied from the T1 GeoPackages
                      with ogr2ogr; layer names carry no census/year (setores, municipios, ...)
  * sector tables     (all other themes) are loaded from the T1 CSVs as attribute tables
  * catalog tables    layers, fields, variables (and any other catalog listed in the config,
                      e.g. consistency for 2000), loaded from the catalog CSVs

The layer list comes from the layers catalog (LAYERS csv): every row decides what is built,
by its `theme`. Nothing census-specific is hardcoded here: the census is whatever the config
points to. Adding/removing a layer means editing the catalog (and providing the matching T1
file). A layer without its T1 file is skipped with a warning.

----------------------------------------------------------------------------------------------
CONFIG (JSON, passed with --config)
----------------------------------------------------------------------------------------------
Required:
  input_folder             the T1 folder holding the CSVs and GeoPackages described below
  output_folder            the T2 folder; created if missing
  output_name              database file name, e.g. IBGE_CENSO-2022_BD_BRASIL_2022.gpkg
  report_folder            where the log (database_log_<timestamp>.txt) is written
  table_pattern            T1 sector table file name, {NAME} = layer name in upper case,
                           e.g. IBGE_CENSO-2022_{NAME}_BRASIL_2022.csv
  geography_pattern        T1 geography file name, {NAME} likewise,
                           e.g. IBGE_CENSO-2022_{NAME}_BRASIL_2022.gpkg
  geography_layer_pattern  layer name inside each T1 GeoPackage, {name} = lower case,
                           e.g. ibge_censo_2022_{name}
  catalogs                 {"layers": path, "fields": path, "variables": path, ...}: the catalog
                           CSVs, each loaded as a table of the same name (';' delimited, UTF-8).
                           "layers" is required and also drives what is built; any further
                           entry (e.g. "consistency": path) is loaded the same way.
Optional (defaults in brackets):
  label                    name shown in the log [output_name]
  key_name                 the sector key column ["setor_code"]
  population_table         sector table loaded first ["basico"]
  skip_themes              themes that are not sector tables (neither geography nor catalogs
                           of the layers file) [["geography", "auxiliary"]]
  cross_check              reference tables for the end-of-run key check [["setores", "basico"]]
  real_variables           {table: [variable names]} to ENFORCE the REAL variables of a table
                           (see COLUMN TYPES). Tables not listed are detected from the data. [{}]

----------------------------------------------------------------------------------------------
WHAT THE SCRIPT EXPECTS IN T1
----------------------------------------------------------------------------------------------
PACKAGED (GZIP) T1: every file below may also be given as <name>.gz (as written by package_t1.py,
e.g. IBGE_CENSO-2022_BASICO_BRASIL_2022.csv.gz, ..._SETORES_BRASIL_2022.gpkg.gz); the plain file wins
when both exist, and the config keeps the plain names. CSVs and catalogs are read straight from the
gzip. A GeoPackage needs random access, so a .gpkg.gz is unpacked to a temporary file next to the
output database (needs the free space of the largest layer) and removed right after the copy.

Geography layers (theme "geography"): one GeoPackage per layer, named by geography_pattern, with
exactly one layer named by geography_layer_pattern (e.g. IBGE_CENSO-2022_SETORES_BRASIL_2022.gpkg
holding ibge_censo_2022_setores). The layer is copied as is (geometry, SRS, columns), renamed to the
plain layer name. Every column named code or ending in _code must already be text (TEXT or TEXT(n));
otherwise an error is logged. Attributes that live in a sector table (e.g. 2022 `setores` keeps only
fid, setor_code and geom; see purge_geometry_attributes.py) are not duplicated here. Points stay points.

Sector tables (every theme not in skip_themes): one CSV per layer, named by table_pattern,
  * UTF-8, ';' delimiter, one header row (standard csv)
  * contains the sector key column (key_name), unique per row (a unique index is created on it,
    so duplicates make the table fail)
  * columns named V<digits> are the variables; all values must be numeric (flags already applied:
    -9999 no data, -1 hidden, -2 uncertain); no empty cells; whole numbers may be written "12" or
    "12.0" (2010 files do the latter)
  * other columns are text (names, aliases, situacao_type...); `area_km2` is numeric.
    Text no-data is "NA".
These files are produced by each census T1 harmonization (assemble_tables.py for 2000 and 2022,
harmonize_2010.py for 2010).

Catalogs: the CSVs listed in `catalogs` (';' delimited, UTF-8, one header row). The columns
`id`, `variable_id` and `scale` become INTEGER when every value is an integer; all else is TEXT.

----------------------------------------------------------------------------------------------
COLUMN TYPES
----------------------------------------------------------------------------------------------
  * key, names, codes, aliases: TEXT (codes stay text, safe for leading zeros)
  * area_km2: REAL
  * V columns: INTEGER or REAL, per table, decided ON THE FLY from the data: a first pass over
    the CSV looks for any value with a non-zero fraction (or an exponent). Columns that have one
    are REAL, all others INTEGER. "12.0" counts as whole. The log lists the REAL variables of
    every table. Caveat: a decimal variable whose values are all whole in the data is detected
    as INTEGER; enforce it in the config if that matters.
  * ENFORCE with real_variables: {"basico": ["V0005", "V0006"], ...}. For a listed table the
    given variables are REAL and all others INTEGER, whatever the detection says. The data is
    still scanned as a safety net: a decimal found in a column that is not listed aborts that
    table, and a listed column without decimals is only reported.
  * flags stay as numbers (-9999 no data, -1 hidden, -2 uncertain) and "NA" in text columns

----------------------------------------------------------------------------------------------
OUTPUT AND BEHAVIOR
----------------------------------------------------------------------------------------------
  * layers: geography first (layers catalog order), then sector tables (population_table first),
    then catalogs. Sector and catalog tables get an integer `fid` primary key, are registered in
    gpkg_contents as attributes, and the sector tables get a unique index on the key.
  * the database is always built from zero: without --overwrite an existing database stops the
    run; with it, a new file is built next to the target (*.part.gpkg) and replaces it only when
    the build finishes, so a failed run leaves the previous database untouched.
  * at the end: key cross-check of each sector table against the cross_check tables present
    (warnings with counts for sectors not found) and a SQLite quick_check, all written to the log.
  * a table that fails (non-numeric value, enforced list conflicting with the data...) is logged,
    left out, and listed in the final warning; the others are still built.
  * every sector table is read twice (type detection, then load), so large censuses take a while.

Usage:
    python build_database.py --config build_database_2022.local.json [--overwrite] [--tables a,b]

  --overwrite  rebuild from zero over an existing database
  --tables     comma-separated subset of layers/catalogs, for test runs
               (the first geography layer is needed to start the GeoPackage)
"""

import argparse
import csv
import gzip
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from operator import itemgetter
from pathlib import Path

V_RE = re.compile(r"^V\d+$")
INT_RE = re.compile(r"^-?\d+$")
# a value with a non-zero fraction ("1.67", "0.5", "3.10") or an exponent ("1e5"); "12.0" is whole
DECIMAL_RE = re.compile(r"\.\d*[1-9]|\d[eE][-+]?\d")
BATCH = 5000
CATALOG_INT_COLUMNS = {"id", "variable_id", "scale"}

REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder", "output_name",
                 "table_pattern", "geography_pattern", "geography_layer_pattern", "catalogs"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    if "layers" not in cfg["catalogs"]:
        sys.exit(f"Config {path}: catalogs must include 'layers'")
    cfg.setdefault("label", cfg["output_name"])
    cfg.setdefault("key_name", "setor_code")
    cfg.setdefault("population_table", "basico")
    cfg.setdefault("skip_themes", ["geography", "auxiliary"])
    cfg.setdefault("cross_check", ["setores", "basico"])
    cfg.setdefault("real_variables", {})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"database_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("census_database")
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


def resolve(path: Path):
    """The file itself, else the same name + .gz (packaged T1), else None."""
    path = Path(path)
    if path.exists():
        return path
    gz = path.with_name(path.name + ".gz")
    return gz if gz.exists() else None


def open_text(path: Path):
    """Text reader for a plain or gzipped (.gz) UTF-8 file."""
    if Path(path).suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def register(con, table: str, data_type: str = "attributes"):
    con.execute("INSERT OR REPLACE INTO gpkg_contents (table_name, data_type, identifier, "
                "description, last_change) VALUES (?,?,?,?,?)",
                (table, data_type, table, "", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")))


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

def make_getter(idx: list):
    """Fast tuple extractor for the given column positions (always returns a tuple)."""
    if len(idx) == 1:
        i = idx[0]
        return lambda row: (row[i],)
    return itemgetter(*idx)


def detect_real(path: Path, header: list) -> tuple:
    """First pass: names of V columns holding any non-zero fraction, and whether any V value
    is written with a decimal point (e.g. "12.0")."""
    pending = [i for i, c in enumerate(header) if V_RE.match(c)]
    real, dotted = set(), False
    if not pending:
        return real, dotted
    getter = make_getter(pending)
    with open_text(path) as f:
        rd = csv.reader(f, delimiter=";")
        next(rd)
        for n, row in enumerate(rd, 2):
            try:
                vals = getter(row)
            except IndexError:
                raise ValueError(f"line {n} has {len(row)} columns, header has {len(header)}")
            s = ";".join(vals)
            if "." in s:
                dotted = True
                if DECIMAL_RE.search(s):
                    hit = [i for i, v in zip(pending, vals) if DECIMAL_RE.search(v)]
                    real.update(header[i] for i in hit)
                    pending = [i for i in pending if i not in hit]
                    if not pending:
                        break
                    getter = make_getter(pending)
    return real, dotted


def column_types(table: str, header: list, real: set) -> list:
    types = []
    for c in header:
        if V_RE.match(c):
            types.append("REAL" if c in real else "INTEGER")
        elif c == "area_km2":
            types.append("REAL")
        else:
            types.append("TEXT")
    return types


def whole_int(v: str) -> int:
    return int(float(v))


def converter(ctype: str, dotted: bool = False):
    if ctype == "INTEGER":
        return whole_int if dotted else int
    return float if ctype == "REAL" else str


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def is_code(col: str) -> bool:
    return col == "code" or col.endswith("_code")


def add_geography(dst: Path, name: str, cfg: dict, logger) -> bool:
    expected = Path(cfg["input_folder"]) / cfg["geography_pattern"].format(NAME=name.upper())
    src = resolve(expected)
    if src is None:
        logger.warning("%-18s T1 file not found (%s[.gz]) — skipped", name, expected.name)
        return False
    layer = cfg["geography_layer_pattern"].format(name=name)
    tmp = None
    try:
        if src.suffix == ".gz":
            # a GeoPackage is a SQLite file and needs random access: unpack next to the target
            tmp = dst.with_name(f"{name}.tmp.gpkg")
            with gzip.open(src, "rb") as fi, open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo, 8 << 20)
            src = tmp
        cmd = ["ogr2ogr", "-f", "GPKG", str(dst), str(src), layer, "-nln", name,
               "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
        if dst.exists():
            cmd.insert(3, "-update")
        res = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    if res.returncode != 0:
        logger.error("%-18s ogr2ogr failed: %s", name, res.stderr.strip()[:600])
        return False
    con = sqlite3.connect(dst)
    try:
        cols = [(r[1], (r[2] or "").upper()) for r in con.execute(f"PRAGMA table_info({q(name)})")]
        n = con.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]
        srs = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?",
                          (name,)).fetchone()
    finally:
        con.close()
    bad = [c for c, t in cols if is_code(c) and not t.startswith("TEXT")]
    logger.info("%-18s %9d features  %2d columns  srs_id=%s", name, n, len(cols), srs and srs[0])
    if bad:
        logger.error("  code columns not TEXT: %s", bad)
    return True


# ---------------------------------------------------------------------------
# Attribute tables
# ---------------------------------------------------------------------------

def create_and_fill(con, table: str, header: list, types: list, rows, key: str,
                    dotted: bool = False) -> int:
    cols = ", ".join(f"{q(c)} {t}" for c, t in zip(header, types))
    con.execute(f"CREATE TABLE {q(table)} (fid INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    sql = f"INSERT INTO {q(table)} ({', '.join(q(c) for c in header)}) " \
          f"VALUES ({', '.join('?' * len(header))})"
    conv = [converter(t, dotted) for t in types]
    n, batch = 0, []
    for row in rows:
        try:
            batch.append([f(v) for f, v in zip(conv, row)])
        except ValueError:
            for c, f, v in zip(header, conv, row):
                try:
                    f(v)
                except ValueError:
                    shown = v if len(v) <= 30 else v[:30] + "..."
                    raise ValueError(f"line {n + len(batch) + 2}, column {c}: "
                                     f"cannot read {shown!r} as {types[header.index(c)]}") from None
            raise
        if len(batch) >= BATCH:
            con.executemany(sql, batch)
            n += len(batch)
            batch = []
    if batch:
        con.executemany(sql, batch)
        n += len(batch)
    if key:
        con.execute(f"CREATE UNIQUE INDEX {q('ux_' + table + '_' + key)} ON {q(table)} ({q(key)})")
    register(con, table)
    con.commit()
    return n


def resolve_real(table: str, header: list, detected: set, cfg: dict, logger):
    """REAL variables of a table: the enforced list if configured, else the detected set.
    Returns None when the enforced list conflicts with the data or the header."""
    enforced = cfg["real_variables"].get(table)
    if enforced is None:
        return detected
    enforced = set(enforced)
    vcols = {c for c in header if V_RE.match(c)}
    unknown = sorted(enforced - vcols)
    if unknown:
        logger.error("%-18s enforced REAL variables not in table: %s", table, unknown)
        return None
    undeclared = sorted(detected - enforced)
    if undeclared:
        logger.error("%-18s data has decimals in variables not enforced as REAL: %s",
                     table, undeclared[:10])
        return None
    whole = sorted(enforced - detected)
    if whole:
        logger.info("%-18s enforced REAL variables with only whole values in the data: %s",
                    table, whole[:10])
    return enforced


def add_sector_table(dst: Path, table: str, cfg: dict, logger) -> bool:
    expected = Path(cfg["input_folder"]) / cfg["table_pattern"].format(NAME=table.upper())
    src = resolve(expected)
    if src is None:
        logger.warning("%-18s T1 file not found (%s[.gz]) — skipped", table, expected.name)
        return False
    with open_text(src) as f:
        header = next(csv.reader(f, delimiter=";"))
    try:
        detected, dotted = detect_real(src, header)
    except (ValueError, UnicodeDecodeError, csv.Error) as e:
        logger.error("%-18s type detection failed: %s", table, e)
        return False
    real = resolve_real(table, header, detected, cfg, logger)
    if real is None:
        return False
    types = column_types(table, header, real)
    con = sqlite3.connect(dst)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        with open_text(src) as f:
            rd = csv.reader(f, delimiter=";")
            next(rd)
            try:
                n = create_and_fill(con, table, header, types, rd, cfg["key_name"], dotted)
            except (ValueError, sqlite3.Error) as e:
                logger.error("%-18s load failed: %s", table, e)
                con.rollback()
                return False
    finally:
        con.close()
    nv = sum(1 for c in header if V_RE.match(c))
    shown = sorted(real)
    logger.info("%-18s %9d rows  %4d INTEGER  %d REAL variables  (%s)", table, n, nv - len(real),
                len(real), "enforced" if table in cfg["real_variables"] else "detected")
    if real:
        logger.info("  REAL: %s%s", ", ".join(shown[:12]), " ..." if len(shown) > 12 else "")
    return True


def add_catalog(dst: Path, name: str, path: str, logger) -> bool:
    found = resolve(Path(path))
    if found is None:
        logger.warning("%-18s catalog file not found (%s[.gz]) — skipped", name, path)
        return False
    with open_text(found) as f:
        rd = csv.reader(f, delimiter=";")
        header = next(rd)
        data = list(rd)
    types = ["INTEGER" if c in CATALOG_INT_COLUMNS and all(INT_RE.match(r[i]) for r in data)
             else "TEXT" for i, c in enumerate(header)]
    con = sqlite3.connect(dst)
    try:
        n = create_and_fill(con, name, header, types, data, None)
    finally:
        con.close()
    logger.info("%-18s %9d rows  (catalog)", name, n)
    return True


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def cross_check(dst: Path, tables: list, key: str, refs: list, logger):
    con = sqlite3.connect(dst)
    try:
        names = {r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")}
        logger.info("-" * 60)
        logger.info("Cross-check of %s against %s", key, " / ".join(refs))
        for ref in refs:
            if ref not in names:
                continue
            if key not in [r[1] for r in con.execute(f"PRAGMA table_info({q(ref)})")]:
                logger.warning("  reference %s has no %s column — skipped", ref, key)
                continue
            for t in tables:
                if t == ref or t not in names:
                    continue
                if key not in [r[1] for r in con.execute(f"PRAGMA table_info({q(t)})")]:
                    continue
                miss = con.execute(f"SELECT COUNT(*) FROM {q(t)} WHERE {q(key)} NOT IN "
                                   f"(SELECT {q(key)} FROM {q(ref)})").fetchone()[0]
                if miss:
                    logger.warning("  %-18s %d sectors not in %s", t, miss, ref)
                else:
                    logger.info("  %-18s all sectors in %s", t, ref)
        bad = con.execute("PRAGMA quick_check").fetchone()[0]
        logger.info("quick_check: %s", bad)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="rebuild from zero over an existing database")
    ap.add_argument("--tables", help="comma-separated subset of layers (test runs)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))

    out_dir = Path(cfg["output_folder"])
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / cfg["output_name"]
    part = out_dir / (final.stem + ".part.gpkg")
    if final.exists() and not args.overwrite:
        logger.error("%s exists — use --overwrite to rebuild from zero", final)
        sys.exit(1)
    for p in (part, Path(str(part) + "-wal"), Path(str(part) + "-shm")):
        if p.exists():
            p.unlink()

    layers_file = resolve(Path(cfg["catalogs"]["layers"]))
    if layers_file is None:
        sys.exit(f"layers catalog not found: {cfg['catalogs']['layers']}[.gz]")
    with open_text(layers_file) as f:
        layers = list(csv.DictReader(f, delimiter=";"))
    only = set(args.tables.split(",")) if args.tables else None
    pick = lambda r: only is None or r["name"] in only
    geo = [r["name"] for r in layers if r["theme"] == "geography" and pick(r)]
    sect = [r["name"] for r in layers if r["theme"] not in cfg["skip_themes"] and pick(r)]
    sect.sort(key=lambda n: n != cfg["population_table"])
    cats = [n for n in cfg["catalogs"] if only is None or n in only]
    unknown = sorted(set(cfg["real_variables"]) - set(sect)) if only is None else []
    if unknown:
        logger.warning("real_variables lists tables that are not sector tables: %s", unknown)

    logger.info("=" * 60)
    logger.info("%s — T2 database  (%d geography, %d sector tables, %d catalogs)",
                cfg["label"], len(geo), len(sect), len(cats))
    logger.info("Target: %s", final)
    logger.info("=" * 60)

    failed = []
    for name in geo:
        if not add_geography(part, name, cfg, logger):
            failed.append(name)
    if not part.exists():
        logger.error("No geography layer built — a GeoPackage needs one to start from; aborting")
        sys.exit(1)
    for name in sect:
        if not add_sector_table(part, name, cfg, logger):
            failed.append(name)
    for name in cats:
        if not add_catalog(part, name, cfg["catalogs"][name], logger):
            failed.append(name)

    cross_check(part, sect, cfg["key_name"], cfg["cross_check"], logger)

    if final.exists():
        final.unlink()
    part.rename(final)
    logger.info("=" * 60)
    logger.info("Wrote %s (%.1f GB)", final, final.stat().st_size / 1e9)
    if failed:
        logger.warning("Layers not built: %s", failed)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

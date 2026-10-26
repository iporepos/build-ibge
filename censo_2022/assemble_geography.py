"""
Assemble the IBGE Census 2022 geography tables (Tier 0 -> Tier 1).

Each geography table lands in its own GeoPackage:

    IBGE_CENSO-2022_<NAME>_BRASIL_2022.gpkg      (layer: ibge_censo_2022_<name>)

Harmonization (same conventions as assemble_tables.py):
  * attribute columns renamed through the shared column_map (cd_regiao -> regiao_code, ...)
  * the sector key CD_SETOR becomes setor_code and comes first
  * V columns (v0001..) are dropped: they live in the BASICO table
  * text columns: NULL, empty or "." -> "NA"; numeric (area_km2): NULL -> -9999
  * polygons/lines promoted to multi-part (points stay points), column "geom", FID column
    "fid", source CRS kept; a table may add its own "column_map" entries in the config

A table configured with "derive_from" is built by dissolving another source layer
(municipios = districts dissolved by municipality code).

Uses the GDAL command line (ogr2ogr, SQLite/Spatialite dialect); sqlite3 only reads the
source schema.

Usage:
    python assemble_geography.py --config assemble_geography.local.json
    python assemble_geography.py --config assemble_geography.local.json --tables municipios --overwrite
    python assemble_geography.py --config assemble_geography.local.json --tables setores --limit 1000
"""

import argparse
import csv
import json
import logging
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

V_RE = re.compile(r"^[Vv]\d+$")
REQUIRED_KEYS = ["assemble_config", "input_folder", "output_folder", "report_folder", "tables"]


# ---------------------------------------------------------------------------
# Configuration / logging
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2022_{NAME}_BRASIL_2022.gpkg")
    cfg.setdefault("layer_pattern", "ibge_censo_2022_{name}")
    cfg.setdefault("text_no_data", "NA")
    cfg.setdefault("numeric_no_data", -9999)
    cfg.setdefault("key_name", "setor_code")
    with open(cfg["assemble_config"], encoding="utf-8") as f:
        cfg["column_map"] = json.load(f).get("column_map", {})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"geography_log_{timestamp}.txt"
    logger = logging.getLogger("censo2022_geography")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_path)
    return logger


# ---------------------------------------------------------------------------
# Source schema (read-only sqlite access)
# ---------------------------------------------------------------------------

def read_schema(gpkg: Path, layer: str) -> dict:
    con = sqlite3.connect(f"file:{gpkg}?immutable=1", uri=True)
    try:
        info = list(con.execute(f'PRAGMA table_info("{layer}")'))
        cols = [(r[1], (r[2] or "").upper()) for r in info]
        pk = [r[1] for r in info if r[5]]
        geom = con.execute("SELECT column_name, srs_id, geometry_type_name FROM gpkg_geometry_columns "
                           "WHERE table_name=?", (layer,)).fetchone()
        count = con.execute(f'SELECT COUNT(*) FROM "{layer}"').fetchone()[0]
    finally:
        con.close()
    if geom is None:
        raise ValueError(f"layer {layer} not found in {gpkg}")
    return dict(columns=cols, pk=pk, geom=geom[0], srs_id=geom[1], geom_type=geom[2], count=count)


def text_expr(col: str, nd: str) -> str:
    return f"CASE WHEN \"{col}\" IS NULL OR TRIM(\"{col}\") IN ('', '.') THEN '{nd}' ELSE \"{col}\" END"


def attribute_expr(col: str, ctype: str, cfg: dict, agg: str = None) -> str:
    """SQL expression for one attribute column (optionally wrapped in an aggregate)."""
    if ctype in ("TEXT", "") or ctype.startswith("TEXT"):
        inner = f"{agg}(\"{col}\")" if agg else f"\"{col}\""
        return (f"CASE WHEN {inner} IS NULL OR TRIM({inner}) IN ('', '.') "
                f"THEN '{cfg['text_no_data']}' ELSE {inner} END")
    inner = f"{agg}(\"{col}\")" if agg else f"\"{col}\""
    return f"COALESCE({inner}, {cfg['numeric_no_data']})"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def build_select(spec: dict, schema: dict, cfg: dict) -> tuple:
    """Return (sql, output column names) for one configured table."""
    cmap = {**cfg["column_map"], **{k.lower(): v for k, v in spec.get("column_map", {}).items()}}
    key = spec.get("key")
    layer = spec["layer"]
    cols = [(c, t) for c, t in schema["columns"] if c not in schema["pk"] and c != schema["geom"]
            and not V_RE.match(c)]
    parts, names = [], []

    def new_name(c):
        if key and c.lower() == key.lower():
            return cfg["key_name"]
        return cmap.get(c.lower(), c.lower())

    if "dissolve_by" in spec:
        by = spec["dissolve_by"]
        keep = spec["keep"]
        sums = spec.get("sum", [])
        lookup = {c.lower(): (c, t) for c, t in cols}
        for c in [*by, *keep, *sums]:
            if c.lower() not in lookup:
                raise ValueError(f"column {c} not in {layer}")
        for c in by:
            col, t = lookup[c.lower()]
            parts.append(f"{attribute_expr(col, t, cfg)} AS \"{new_name(c)}\"")
            names.append(new_name(c))
        for c in keep:
            col, t = lookup[c.lower()]
            parts.append(f"{attribute_expr(col, t, cfg, 'MIN')} AS \"{new_name(c)}\"")
            names.append(new_name(c))
        for c in sums:
            col, t = lookup[c.lower()]
            parts.append(f"SUM(\"{col}\") AS \"{new_name(c)}\"")
            names.append(new_name(c))
        parts.append(f"ST_Union(\"{schema['geom']}\") AS geom")
        group = ", ".join(f'"{lookup[c.lower()][0]}"' for c in by)
        where = " AND ".join(f'"{lookup[c.lower()][0]}" IS NOT NULL' for c in by)
        sql = f'SELECT {", ".join(parts)} FROM "{layer}" WHERE {where} GROUP BY {group}'
        return sql, names

    # key / 'first' columns first, the rest in source order
    first = [c.lower() for c in ([key] if key else []) + spec.get("first", [])]
    ordered = sorted(cols, key=lambda ct: first.index(ct[0].lower())
                     if ct[0].lower() in first else len(first))
    for c, t in ordered:
        parts.append(f"{attribute_expr(c, t, cfg)} AS \"{new_name(c)}\"")
        names.append(new_name(c))
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate column names after mapping in {layer}")
    parts.append(f"\"{schema['geom']}\" AS geom")
    return f'SELECT {", ".join(parts)} FROM "{layer}"', names


def run_ogr2ogr(src: Path, dst: Path, layer_out: str, sql: str, geom_type: str, limit,
                logger) -> bool:
    cmd = ["ogr2ogr", "-f", "GPKG", str(dst), str(src),
           "-dialect", "SQLite", "-sql", sql, "-nln", layer_out,
           "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
    if "POINT" not in geom_type.upper():
        cmd += ["-nlt", "PROMOTE_TO_MULTI"]  # points stay points
    if limit:
        cmd += ["-limit", str(limit)]
    logger.debug("  %s", " ".join(cmd[:6]) + " ...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        logger.error("  ogr2ogr failed: %s", res.stderr.strip()[:600])
        return False
    if res.stderr.strip():
        logger.warning("  ogr2ogr: %s", res.stderr.strip()[:300])
    return True


def check_output(dst: Path, layer_out: str, expected, src_srs: int, logger) -> dict:
    schema = read_schema(dst, layer_out)
    logger.info("  output: %d features, %d columns, srs_id=%s", schema["count"],
                len(schema["columns"]) - 2, schema["srs_id"])
    if expected is not None and schema["count"] != expected:
        logger.warning("  feature count %d differs from expected %d", schema["count"], expected)
    if schema["srs_id"] != src_srs:
        logger.warning("  srs_id %s differs from source %s", schema["srs_id"], src_srs)
    return schema


def basico_keys(path: Path, key_name: str) -> set:
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.reader(f, delimiter=";")
        header = next(r)
        i = header.index(key_name)
        return {row[i] for row in r}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--tables", help="Comma-separated table names (default: all configured)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    parser.add_argument("--limit", type=int, help="Only write the first N features (testing)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    input_folder = Path(cfg["input_folder"])
    output_folder = Path(cfg["output_folder"])

    names = list(cfg["tables"])
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",")]
        unknown = [t for t in wanted if t not in cfg["tables"]]
        if unknown:
            sys.exit(f"Tables not configured: {unknown}")
        names = [t for t in names if t in wanted]
    if not input_folder.is_dir():
        sys.exit(f"Input folder does not exist: {input_folder}")
    output_folder.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("IBGE Censo 2022 — geography tables: %s", ", ".join(names))
    logger.info("=" * 60)

    failed = []
    for n, name in enumerate(names, 1):
        spec = cfg["tables"][name]
        src_spec = cfg["tables"][spec["derive_from"]] if "derive_from" in spec else spec
        full = {**src_spec, **spec}  # a derived table keeps its own dissolve settings
        full["layer"] = src_spec["layer"]
        src = input_folder / src_spec["source"]
        dst = output_folder / cfg["name_pattern"].format(NAME=name.upper())
        layer_out = cfg["layer_pattern"].format(name=name)
        logger.info("[%d/%d] %s  <-  %s (%s)", n, len(names), name, src.name, src_spec["layer"])
        if dst.exists() and not args.overwrite:
            logger.info("  SKIP (output exists, use --overwrite)")
            continue
        try:
            schema = read_schema(src, src_spec["layer"])
            sql, cols = build_select(full, schema, cfg)
            logger.info("  source: %d features; output columns: %s", schema["count"], cols)
            if "dissolve_by" in spec:
                db = sqlite3.connect(f"file:{src}?immutable=1", uri=True)
                try:
                    by = full["dissolve_by"][0]
                    nnull = db.execute(f'SELECT COUNT(*) FROM "{src_spec["layer"]}" '
                                       f'WHERE "{by}" IS NULL').fetchone()[0]
                finally:
                    db.close()
                if nnull:
                    logger.info("  %d source features without %s are left out of the dissolve",
                                nnull, by)
            tmp = dst.with_name(dst.stem + ".part.gpkg")
            if tmp.exists():
                tmp.unlink()
            ok = run_ogr2ogr(src, tmp, layer_out, sql, schema["geom_type"], args.limit, logger)
            if not ok:
                if tmp.exists():
                    tmp.unlink()
                failed.append(name)
                continue
            expected = None if (args.limit or "dissolve_by" in spec) else schema["count"]
            out_schema = check_output(tmp, layer_out, expected, schema["srs_id"], logger)
            tmp.replace(dst)
            if name == "setores" and not args.limit and cfg.get("basico_csv"):
                con = sqlite3.connect(f"file:{dst}?immutable=1", uri=True)
                try:
                    sk = [r[0] for r in con.execute(f'SELECT "{cfg["key_name"]}" FROM "{layer_out}"')]
                finally:
                    con.close()
                bk = basico_keys(Path(cfg["basico_csv"]), cfg["key_name"])
                logger.info("  setor_code: %d unique of %d features; in polygons not in BASICO: %d; "
                            "in BASICO not in polygons: %d", len(set(sk)), len(sk),
                            len(set(sk) - bk), len(bk - set(sk)))
        except Exception as exc:
            logger.error("  FAILED: %s", exc)
            failed.append(name)

    logger.info("=" * 60)
    logger.info("DONE — %d processed, %d failed", len(names) - len(failed), len(failed))
    if failed:
        logger.info("Failed: %s", ", ".join(failed))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

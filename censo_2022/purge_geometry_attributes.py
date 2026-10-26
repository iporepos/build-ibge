"""
Purge duplicated attribute columns from a T1 geography GeoPackage, keeping only the key and geometry.

The attributes of a sector live in the `basico` table; the `setores` layer is meant to hold the
geometry alone. For each layer in the config the script rewrites the GeoPackage with only
fid, the `keep` columns and the geometry, then checks that nothing but columns changed:

  * feature count, SRS and geometry type are unchanged
  * the output columns are exactly fid, geom + keep
  * the key values (first keep column) are identical, row by row, to the source
  * optionally, the key set is compared with the `basico` CSV

The new file is built next to the original (*.part.gpkg); only after the checks pass the original
is renamed to *.orig.gpkg (keep_backup true) or deleted, and the new file takes its place.
A layer that already has just fid/geom/keep is left untouched.

Usage:
    python purge_geometry_attributes.py --config purge_geometry_attributes.local.json [--layers setores]
"""

import argparse
import csv
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["folder", "report_folder", "layers"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    for name, spec in cfg["layers"].items():
        bad = [k for k in ("gpkg", "layer", "keep") if k not in spec]
        if bad or not spec["keep"]:
            sys.exit(f"Config layer {name}: missing/empty {bad or ['keep']}")
    cfg.setdefault("keep_backup", True)
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"purge_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo2022_purge")
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


def read_info(gpkg: Path, layer: str) -> dict:
    con = sqlite3.connect(f"file:{gpkg}?immutable=1", uri=True)
    try:
        info = list(con.execute(f"PRAGMA table_info({q(layer)})"))
        geom = con.execute("SELECT column_name, srs_id, geometry_type_name FROM gpkg_geometry_columns "
                           "WHERE table_name=?", (layer,)).fetchone()
        if geom is None:
            raise ValueError(f"layer {layer} not found in {gpkg}")
        count = con.execute(f"SELECT COUNT(*) FROM {q(layer)}").fetchone()[0]
    finally:
        con.close()
    return dict(columns=[r[1] for r in info], pk=[r[1] for r in info if r[5]],
                geom=geom[0], srs_id=geom[1], geom_type=geom[2], count=count)


def key_signature(gpkg: Path, layer: str, key: str, geom: str) -> tuple:
    """(row count, distinct keys, order-sensitive hash of the key column ordered by fid)."""
    import hashlib
    con = sqlite3.connect(f"file:{gpkg}?immutable=1", uri=True)
    try:
        h = hashlib.sha1()
        n = 0
        for (v,) in con.execute(f"SELECT CAST({q(key)} AS TEXT) FROM {q(layer)} ORDER BY rowid"):
            h.update((v or "").encode("utf-8") + b"\n")
            n += 1
        distinct = con.execute(f"SELECT COUNT(DISTINCT {q(key)}) FROM {q(layer)}").fetchone()[0]
    finally:
        con.close()
    return n, distinct, h.hexdigest()


def basico_keys(path: Path) -> set:
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.reader(f, delimiter=";")
        next(rd)
        return {r[0] for r in rd}


def purge(name: str, spec: dict, cfg: dict, basico: set, logger) -> bool:
    folder = Path(cfg["folder"])
    src = folder / spec["gpkg"]
    layer, keep = spec["layer"], spec["keep"]
    if not src.exists():
        logger.error("%s: %s not found", name, src)
        return False
    before = read_info(src, layer)
    attrs = [c for c in before["columns"] if c not in before["pk"] + [before["geom"]]]
    missing = [c for c in keep if c not in attrs]
    if missing:
        logger.error("%s: keep columns not in layer: %s", name, missing)
        return False
    extra = [c for c in attrs if c not in keep]
    logger.info("%s: %d features, %d attribute columns (%d to keep, %d to purge)",
                name, before["count"], len(attrs), len(keep), len(extra))
    if not extra:
        logger.info("  already purged — nothing to do")
        return True

    part = src.with_name(src.stem + ".part.gpkg")
    for p in (part, Path(str(part) + "-wal"), Path(str(part) + "-shm")):
        if p.exists():
            p.unlink()
    sql = f'SELECT {", ".join(q(c) for c in keep)}, {q(before["geom"])} FROM {q(layer)} ORDER BY rowid'
    cmd = ["ogr2ogr", "-f", "GPKG", str(part), str(src), "-dialect", "SQLite", "-sql", sql,
           "-nln", layer, "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
    if "POINT" not in (before["geom_type"] or "").upper():
        cmd += ["-nlt", "PROMOTE_TO_MULTI"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        logger.error("  ogr2ogr failed: %s", res.stderr.strip()[:600])
        return False
    if res.stderr.strip():
        logger.warning("  ogr2ogr: %s", res.stderr.strip()[:300])

    # ---- checks -------------------------------------------------------
    after = read_info(part, layer)
    ok = True
    if after["count"] != before["count"]:
        logger.error("  feature count changed: %d -> %d", before["count"], after["count"])
        ok = False
    if after["srs_id"] != before["srs_id"]:
        logger.error("  srs_id changed: %s -> %s", before["srs_id"], after["srs_id"])
        ok = False
    if after["geom_type"] != before["geom_type"]:
        logger.error("  geometry type changed: %s -> %s", before["geom_type"], after["geom_type"])
        ok = False
    want = sorted(after["pk"] + [after["geom"]] + keep)
    if sorted(after["columns"]) != want:
        logger.error("  unexpected columns: %s", after["columns"])
        ok = False
    k = keep[0]
    sb, sa = key_signature(src, layer, k, before["geom"]), key_signature(part, layer, k, after["geom"])
    if sb != sa:
        logger.error("  key column %s differs from source (rows/distinct/hash %s vs %s)", k, sb, sa)
        ok = False
    else:
        logger.info("  %s identical to source: %d rows, %d distinct values", k, sa[0], sa[1])
    if basico:
        con = sqlite3.connect(f"file:{part}?immutable=1", uri=True)
        codes = {r[0] for r in con.execute(f"SELECT DISTINCT {q(k)} FROM {q(layer)}")}
        con.close()
        logger.info("  vs basico: %d only in layer, %d only in basico",
                    len(codes - basico), len(basico - codes))
    if not ok:
        logger.error("  checks failed — original kept, new file left as %s", part.name)
        return False

    size_b, size_a = src.stat().st_size, part.stat().st_size
    if cfg["keep_backup"]:
        backup = src.with_name(src.stem + ".orig.gpkg")
        if backup.exists():
            backup.unlink()
        src.rename(backup)
        logger.info("  original kept as %s", backup.name)
    else:
        src.unlink()
    part.rename(src)
    logger.info("  columns now: %s", after["columns"])
    logger.info("  size %.2f GB -> %.2f GB", size_b / 1e9, size_a / 1e9)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--layers", help="comma-separated subset of config layers")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    names = args.layers.split(",") if args.layers else list(cfg["layers"])
    unknown = [n for n in names if n not in cfg["layers"]]
    if unknown:
        sys.exit(f"Layers not in config: {unknown}")
    basico = set()
    if cfg.get("basico_csv") and Path(cfg["basico_csv"]).exists():
        basico = basico_keys(Path(cfg["basico_csv"]))
        logger.info("basico keys: %d", len(basico))
    failed = [n for n in names if not purge(n, cfg["layers"][n], cfg, basico, logger)]
    if failed:
        logger.error("Failed: %s", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()

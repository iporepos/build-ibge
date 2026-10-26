"""
Merge IBGE Census 2010 shapefiles (distritos & subdistritos) from per-UF zips
into nationwide GeoPackage layers.

Reads directly from zip files — no manual extraction needed.

Usage:
    python merge_malhas.py --config merge_malhas.local.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd

# UF lookup: alias → (official name, IBGE numeric code)
UF_INFO = {
    "ac": ("Acre", "12"),
    "al": ("Alagoas", "27"),
    "am": ("Amazonas", "13"),
    "ap": ("Amapá", "16"),
    "ba": ("Bahia", "29"),
    "ce": ("Ceará", "23"),
    "df": ("Distrito Federal", "53"),
    "es": ("Espírito Santo", "32"),
    "go": ("Goiás", "52"),
    "ma": ("Maranhão", "21"),
    "mg": ("Minas Gerais", "31"),
    "ms": ("Mato Grosso do Sul", "50"),
    "mt": ("Mato Grosso", "51"),
    "pa": ("Pará", "15"),
    "pb": ("Paraíba", "25"),
    "pe": ("Pernambuco", "26"),
    "pi": ("Piauí", "22"),
    "pr": ("Paraná", "41"),
    "rj": ("Rio de Janeiro", "33"),
    "rn": ("Rio Grande do Norte", "24"),
    "ro": ("Rondônia", "11"),
    "rr": ("Roraima", "14"),
    "rs": ("Rio Grande do Sul", "43"),
    "sc": ("Santa Catarina", "42"),
    "se": ("Sergipe", "28"),
    "sp": ("São Paulo", "35"),
    "to": ("Tocantins", "17"),
}

UFS = list(UF_INFO.keys())  # default; overridable via config "ufs"


# ---------------------------------------------------------------------------
# Configuration (populated from --config by load_config)
# ---------------------------------------------------------------------------
INPUT_FOLDER: Path
OUTPUT_FOLDER: Path
ADMIN_LEVELS: dict  # layer name -> zip filename pattern
OUTPUT_CRS: str

REQUIRED_KEYS = ["input_folder", "output_folder", "admin_levels"]


def load_config(path: str) -> None:
    """Load the JSON config and set module-level settings."""
    global INPUT_FOLDER, OUTPUT_FOLDER, ADMIN_LEVELS, OUTPUT_CRS, UFS
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    INPUT_FOLDER = Path(cfg["input_folder"])
    OUTPUT_FOLDER = Path(cfg["output_folder"])
    ADMIN_LEVELS = cfg["admin_levels"]
    OUTPUT_CRS = cfg.get("output_crs", "EPSG:4674")  # SIRGAS 2000 (IBGE standard)
    if "ufs" in cfg:
        unknown = [u for u in cfg["ufs"] if u.lower() not in UF_INFO]
        if unknown:
            sys.exit(f"Unknown UFs in config: {unknown}")
        UFS = [u.lower() for u in cfg["ufs"]]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_FOLDER / f"merge_log_{timestamp}.txt"

    logger = logging.getLogger("ibge_merge")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def read_shp_from_zip(zip_path: Path, logger: logging.Logger):
    """
    Read the shapefile inside a zip archive.
    Returns a GeoDataFrame or None on failure.
    """
    try:
        gdf = gpd.read_file(f"zip://{zip_path}")
        logger.debug("  Read %d features from %s", len(gdf), zip_path.name)
        return gdf
    except Exception as exc:
        logger.warning("  Failed to read %s: %s", zip_path.name, exc)
        return None


def merge_level(level_name: str, pattern: str, logger: logging.Logger) -> bool:
    """
    Merge all UF shapefiles for one admin level into a single GeoPackage.
    Returns True on success.
    """
    logger.info("-" * 50)
    logger.info("Processing level: %s", level_name)

    parts: list[gpd.GeoDataFrame] = []
    missing = []
    failed = []

    for uf in UFS:
        filename = pattern.format(uf=uf)
        zip_path = INPUT_FOLDER / uf / filename

        if not zip_path.exists():
            logger.warning("  MISSING  %s", zip_path)
            missing.append(uf.upper())
            continue

        gdf = read_shp_from_zip(zip_path, logger)
        if gdf is not None and len(gdf) > 0:
            uf_name, uf_code = UF_INFO[uf]
            gdf["unidade_alias"] = uf.upper()
            gdf["unidade_name"] = uf_name
            gdf["unidade_code"] = uf_code
            parts.append(gdf)
        else:
            failed.append(uf.upper())

    if not parts:
        logger.error("  No data collected for %s — skipping.", level_name)
        return False

    logger.info("  Concatenating %d UFs (%d skipped, %d failed) ...",
                len(parts), len(missing), len(failed))
    merged = gpd.pd.concat(parts, ignore_index=True)
    merged = gpd.GeoDataFrame(merged, geometry="geometry")

    # ---- Column cleanup ----
    level_singular = level_name.rstrip("s")  # "distrito" | "subdistrito"
    code_col = f"{level_singular}_code"
    name_col = f"{level_singular}_name"

    # CD_GEOCOD* → distrito_code / subdistrito_code (as text)
    geocod_cols = [c for c in merged.columns if c.upper().startswith("CD_GEOCOD")]
    if geocod_cols:
        merged[code_col] = merged[geocod_cols[0]].astype(str)
        logger.info("  Renamed %s → %s (as text)", geocod_cols, code_col)
    else:
        logger.warning("  No CD_GEOCOD* column found")

    # NM_* → distrito_name / subdistrito_name
    nm_cols = [c for c in merged.columns if c.upper().startswith("NM_")]
    if nm_cols:
        merged[name_col] = merged[nm_cols[0]].astype(str)
        logger.info("  Renamed %s → %s", nm_cols, name_col)
    else:
        logger.warning("  No NM_* column found")

    # Keep only the curated columns + fid (if present) + geometry
    keep = ["unidade_alias", "unidade_name", "unidade_code",
            code_col, name_col]
    if "fid" in merged.columns:
        keep = ["fid"] + keep
    keep.append("geometry")
    # guard against missing columns
    keep = [c for c in keep if c in merged.columns]
    merged = merged[keep]

    logger.info("  Final columns: %s", list(merged.columns))

    # Ensure consistent CRS
    if merged.crs is None:
        logger.warning("  No CRS detected — assigning %s", OUTPUT_CRS)
        merged = merged.set_crs(OUTPUT_CRS)
    elif str(merged.crs) != OUTPUT_CRS:
        logger.info("  Reprojecting from %s to %s", merged.crs, OUTPUT_CRS)
        merged = merged.to_crs(OUTPUT_CRS)

    out_path = OUTPUT_FOLDER / f"{level_name}.gpkg"
    logger.info("  Writing %d features to %s ...", len(merged), out_path.name)

    try:
        merged.to_file(out_path, driver="GPKG", layer=level_name)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        logger.info("  OK  %s  (%.1f MB)", out_path.name, size_mb)
    except Exception as exc:
        logger.error("  Write failed for %s: %s", out_path, exc)
        return False

    if missing:
        logger.info("  Missing UFs: %s", ", ".join(missing))
    if failed:
        logger.info("  Failed UFs:  %s", ", ".join(failed))

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    load_config(parser.parse_args().config)

    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("IBGE Setores 2010 — ZIP → GeoPackage Merger")
    logger.info("Input : %s", INPUT_FOLDER)
    logger.info("Output: %s", OUTPUT_FOLDER)
    logger.info("=" * 60)

    if not INPUT_FOLDER.exists():
        logger.error("Input folder does not exist: %s", INPUT_FOLDER)
        sys.exit(1)

    results = {}
    for level_name, pattern in ADMIN_LEVELS.items():
        ok = merge_level(level_name, pattern, logger)
        results[level_name] = ok

    logger.info("=" * 60)
    logger.info("SUMMARY")
    for level_name, ok in results.items():
        status = "OK" if ok else "FAILED"
        logger.info("  %-20s %s", level_name, status)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()


"""
Census geography layers taken from the DTB T1 snapshot of the census year (dtb/assemble_geography.py),
written as census T1 gpkgs (IBGE_CENSO-<year>_<NAME>_BRASIL_<year>.gpkg, layer ibge_censo_<year>_<name>).

  * unidades  unidade_code, unidade_alias, unidade_name, regiao_name, area_km2 — the DTB state polygons of
              the snapshot as they are; the alias comes from alias_source (a census layer that already
              carries unidade_alias and a code starting with the state code, e.g. distritos or
              municipios) and the region from the first digit of
              the code (regioes). Every DTB state must get an alias and a region, and every alias_source
              state must be in the DTB layer, else nothing is written.
  * uniao     pais_name, area_km2 — the country: union of the states; holes below fill_holes_km2
              (precision slivers between state polygons) are filled, larger ones kept and logged.

Areas are computed in area_crs (equal-area); geometries stay in the DTB CRS. Files are written to
<name>.part.gpkg and renamed when complete; an existing output stops the layer unless --overwrite.

CONFIG (JSON, --config) — required: dtb_unidades, output_folder, report_folder, year
  dtb_unidades     DTB T1 IBGE_DTB_UNIDADES_BRASIL_<year>.gpkg
  alias_source     census gpkg with unidade_alias and a code column
  alias_code_column  column of alias_source whose first two digits are the state code ["unidade_code"]
  regioes          {"1": "Norte", ...} first code digit -> region name
  layers           which layers to write [["unidades", "uniao"]]
  pais_name        ["Brasil"]
  area_crs         ["ESRI:102033"]
  fill_holes_km2   [1.0]
  name_pattern     ["IBGE_CENSO-{YEAR}_{NAME}_BRASIL_{YEAR}.gpkg"]
  layer_pattern    ["ibge_censo_{year}_{name}"]

Usage:
    .venv/bin/python geography_from_dtb.py --config geography_from_dtb_2010.local.json [--overwrite]
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

REQUIRED_KEYS = ["dtb_unidades", "output_folder", "report_folder", "year"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("alias_source", None)
    cfg.setdefault("alias_code_column", "unidade_code")
    cfg.setdefault("regioes", {})
    cfg.setdefault("layers", ["unidades", "uniao"])
    cfg.setdefault("pais_name", "Brasil")
    cfg.setdefault("area_crs", "ESRI:102033")
    cfg.setdefault("fill_holes_km2", 1.0)
    cfg.setdefault("name_pattern", "IBGE_CENSO-{YEAR}_{NAME}_BRASIL_{YEAR}.gpkg")
    cfg.setdefault("layer_pattern", "ibge_censo_{year}_{name}")
    return cfg


def setup_logging(report_folder: Path, stamp: str) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"geography_from_dtb_log_{stamp}.txt"
    logger = logging.getLogger("census_geography_from_dtb")
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


def polygonal(geom):
    """Valid MultiPolygon; collections returned by make_valid lose their lines and points."""
    geom = make_valid(geom)
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    polys = []
    for p in getattr(geom, "geoms", []):
        if p.geom_type == "Polygon":
            polys.append(p)
        elif p.geom_type == "MultiPolygon":
            polys.extend(p.geoms)
    return MultiPolygon(polys)


def build_unidades(cfg: dict, logger) -> gpd.GeoDataFrame:
    u = gpd.read_file(cfg["dtb_unidades"], engine="pyogrio")
    u["unidade_code"] = u["unidade_code"].astype(str)
    problems = []
    if cfg["alias_source"]:
        col = cfg["alias_code_column"]
        src = gpd.read_file(cfg["alias_source"], engine="pyogrio", ignore_geometry=True,
                            columns=[col, "unidade_alias"]).astype(str)
        src["unidade_code"] = src[col].str[:2]
        src = src[["unidade_code", "unidade_alias"]].drop_duplicates()
        dup = src["unidade_code"][src["unidade_code"].duplicated()].tolist()
        if dup:
            problems.append(f"alias_source has several aliases for {dup}")
        aliases = dict(zip(src["unidade_code"], src["unidade_alias"]))
        extra = sorted(set(aliases) - set(u["unidade_code"]))
        if extra:
            problems.append(f"alias_source states not in the DTB layer: {extra}")
        u["unidade_alias"] = u["unidade_code"].map(aliases)
    else:
        u["unidade_alias"] = pd.NA
    u["regiao_name"] = u["unidade_code"].str[0].map(cfg["regioes"])
    for col in ("unidade_alias", "regiao_name"):
        miss = u.loc[u[col].isna(), "unidade_code"].tolist()
        if miss:
            problems.append(f"no {col} for {miss}")
    if problems:
        for p in problems:
            logger.error("unidades: %s", p)
        sys.exit(1)
    dtb_area = pd.to_numeric(u["area_km2"]).sum() if "area_km2" in u else float("nan")
    u["area_km2"] = u.to_crs(cfg["area_crs"]).area / 1e6
    logger.info("unidades: %d states, %.0f km2 (DTB area_km2 %.0f)", len(u), u["area_km2"].sum(), dtb_area)
    cols = ["unidade_code", "unidade_alias", "unidade_name", "regiao_name", "area_km2", "geometry"]
    return u[cols].sort_values("unidade_code").reset_index(drop=True)


def build_uniao(u: gpd.GeoDataFrame, cfg: dict, logger) -> gpd.GeoDataFrame:
    geom = polygonal(unary_union([make_valid(g) for g in u.geometry]))
    limit, acrs = cfg["fill_holes_km2"], cfg["area_crs"]
    polys, n_fill, a_fill = [], 0, 0.0
    for p in geom.geoms:
        keep = []
        for r in p.interiors:
            a = gpd.GeoSeries([Polygon(r)], crs=u.crs).to_crs(acrs).iloc[0].area / 1e6
            if a < limit:
                n_fill += 1
                a_fill += a
                continue
            pt = Polygon(r).representative_point()
            logger.warning("uniao: hole of %.1f km2 kept at (%.3f, %.3f)", a, pt.y, pt.x)
            keep.append(r)
        polys.append(Polygon(p.exterior, keep))
    geom = polygonal(unary_union(polys))
    out = gpd.GeoDataFrame({"pais_name": [cfg["pais_name"]]}, geometry=[geom], crs=u.crs)
    out["area_km2"] = out.to_crs(acrs).area / 1e6
    logger.info("uniao: %d parts, %d sliver holes filled (%.2f km2), %.0f km2 (sum of states %.0f)",
                len(geom.geoms), n_fill, a_fill, out["area_km2"].iloc[0], u["area_km2"].sum())
    return out[["pais_name", "area_km2", "geometry"]]


def write_layer(gdf: gpd.GeoDataFrame, cfg: dict, name: str, overwrite: bool, logger) -> bool:
    year = cfg["year"]
    out = Path(cfg["output_folder"]) / cfg["name_pattern"].format(NAME=name.upper(), YEAR=year)
    if out.exists() and not overwrite:
        logger.error("%s exists — use --overwrite", out)
        return False
    tmp = out.with_name(out.stem + ".part.gpkg")
    if tmp.exists():
        tmp.unlink()
    gdf.to_file(tmp, layer=cfg["layer_pattern"].format(name=name, year=year), driver="GPKG", engine="pyogrio")
    tmp.replace(out)
    logger.info("Wrote %s (%d features)", out, len(gdf))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]), datetime.now().strftime("%Y%m%d_%H%M%S"))
    Path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)

    unknown = sorted(set(cfg["layers"]) - {"unidades", "uniao"})
    if unknown:
        sys.exit(f"unknown layers: {unknown}")
    u = build_unidades(cfg, logger)
    ok = True
    if "unidades" in cfg["layers"]:
        ok &= write_layer(u, cfg, "unidades", args.overwrite, logger)
    if "uniao" in cfg["layers"]:
        ok &= write_layer(build_uniao(u, cfg, logger), cfg, "uniao", args.overwrite, logger)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

"""
Plotting geography of a dataset of attribute tables (PNSB-<year>, ATLASSAN), taken from the DTB T1: for each
DTB edition the dataset uses (rows of its tables), the municipalities, seats, states, intermediate regions and
the country outline of that edition, simplified for maps. Written as T1 gpkgs of the dataset
(e.g. IBGE_PNSB-2008_GEO-MUNICIPIOS_BRASIL_2008.gpkg); the T2 builder copies them into the database.

The DTB has meshes for some years only (e.g. 2000, 2010, 2022). An edition is built from the mesh of a later
(or the same) year given in the config:
  * municipios  the mesh polygons; codes not in the edition are dropped when their area_type is in
                drop_area_types (the lagoons), else dissolved into their parent in the edition: the code_from
                of the DTB linhagem "split" row whose code_to is the code (year_from <= edition). Names and
                state codes from the DTB quadro of the edition; merged_codes lists the dissolved codes.
                Simplified with shapely coverage_simplify (simplify_tolerance, CRS units; 0 = none), which keeps
                the shared borders shared.
  * sedes       the DTB seats of the mesh year, only the codes of the edition (seats of dissolved
                municipalities dropped); names of the edition
  * unidades, regioesintermediarias, pais   dissolved from the (simplified) municipios by the codes of the
                edition quadro (unidade_code, regiaointermediaria_code), the country = all municipalities; holes
                smaller than fill_holes (CRS units², the gaps between mesh polygons) are filled. "codes_from"
                takes the codes of a level from a later quadro edition with the same municipalities (the
                intermediate regions of 2017 are NA in the quadro until 2019)
Geometries stay in the DTB CRS.

Checks (nothing is written for an edition that fails): the municipios codes equal the edition codes of the
quadro; every code to dissolve has exactly one parent in the edition; every geometry valid and non-empty;
every requested level has codes in the quadro of the edition (e.g. no intermediate regions before 2017).

CONFIG (JSON, --config)
  required:
    dtb_folder      DTB T1 folder (IBGE_DTB_MUNICIPIOS_BRASIL_<mesh>.gpkg, IBGE_DTB_SEDES_BRASIL_<mesh>.gpkg)
    dtb_quadro      DTB T1 IBGE_DTB_QUADRO-MUNICIPIOS_BRASIL_1940-2022.csv
    dtb_lineage     DTB T1 IBGE_DTB_LINHAGEM_BRASIL_1872-2022.csv
    output_folder   T1 ASSETS folder of the dataset
    report_folder   log folder
    editions        [{"edition": 2008, "mesh": 2010, "year": 2008 (names; default edition),
                      "layers": [...] (default: layers), "codes_from": {level: quadro edition}}]
    name_pattern    e.g. "IBGE_PNSB-{YEAR}_GEO-{NAME}_BRASIL_{YEAR}.gpkg"
    layer_pattern   e.g. "ibge_pnsb_{year}_geo_{name}"
  optional:
    layers              default ["municipios", "sedes", "unidades", "pais"]
    simplify_tolerance  default 0.001 (degrees of EPSG:4674, about 110 m)
    fill_holes          default 0.0001 (degrees², about 1.2 km²)
    drop_area_types     default ["lagoa"]
    pais_name           default "Brasil"
    mesh_pattern        default "IBGE_DTB_{NAME}_BRASIL_{MESH}.gpkg"

Usage:
    python geography_from_dtb_editions.py --config geography_pnsb_2008.local.json [--overwrite]
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

REQUIRED_KEYS = ["dtb_folder", "dtb_quadro", "dtb_lineage", "output_folder", "report_folder", "editions",
                 "name_pattern", "layer_pattern"]
LEVELS = {"unidades": ("unidade_code", "unidade_name"),
          "regioesintermediarias": ("regiaointermediaria_code", "regiaointermediaria_name")}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("layers", ["municipios", "sedes", "unidades", "pais"])
    cfg.setdefault("simplify_tolerance", 0.001)
    cfg.setdefault("fill_holes", 0.0001)
    cfg.setdefault("drop_area_types", ["lagoa"])
    cfg.setdefault("pais_name", "Brasil")
    cfg.setdefault("mesh_pattern", "IBGE_DTB_{NAME}_BRASIL_{MESH}.gpkg")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"geography_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("geography_editions")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def fill_holes(geom, min_area: float):
    """The geometry without its interior rings smaller than min_area."""
    polys = []
    for p in shapely.get_parts(geom):
        if not isinstance(p, shapely.Polygon):
            continue
        keep = [r for r in p.interiors if shapely.Polygon(r).area >= min_area]
        polys.append(shapely.Polygon(p.exterior, keep))
    return shapely.make_valid(shapely.MultiPolygon(polys))


def dissolve(gdf: gpd.GeoDataFrame, by: pd.Series, min_hole: float = 0) -> gpd.GeoSeries:
    """Union of the geometries per value of by (index = the values), small holes filled."""
    parts = {k: shapely.make_valid(shapely.union_all(g.values)) for k, g in gdf.geometry.groupby(by.values)}
    if min_hole:
        parts = {k: fill_holes(g, min_hole) for k, g in parts.items()}
    return gpd.GeoSeries(parts, crs=gdf.crs)


def build_edition(ed: dict, cfg: dict, quadro: pd.DataFrame, lineage: pd.DataFrame, logger) -> tuple:
    """({layer: GeoDataFrame}, [errors]) for one edition."""
    edition, mesh = int(ed["edition"]), int(ed["mesh"])
    layers = ed.get("layers", cfg["layers"])
    q = quadro[quadro["edition"] == str(edition)].set_index("municipio_code")
    folder = Path(cfg["dtb_folder"])
    m = pyogrio.read_dataframe(folder / cfg["mesh_pattern"].format(NAME="MUNICIPIOS", MESH=mesh))
    errors = []
    extra = m[~m["municipio_code"].isin(q.index)]
    drop = extra[extra["area_type"].isin(cfg["drop_area_types"])]
    to_merge = extra[~extra["area_type"].isin(cfg["drop_area_types"])]
    if len(drop):
        logger.info("  %d: dropped %s (%s)", edition, " ".join(drop["municipio_code"]), " ".join(drop["area_type"]))
    parent = {}
    for c in to_merge["municipio_code"]:
        rows = lineage[(lineage["code_to"] == c) & (lineage["relation"] == "split") & lineage["code_from"].isin(q.index)
                       & (lineage["year_from"].astype(int) <= edition)]
        cands = sorted(set(rows["code_from"]))
        if len(cands) != 1:
            errors.append(f"{edition}: {c} not in the edition and {len(cands)} parents in the lineage {cands}")
        else:
            parent[c] = cands[0]
            logger.info("  %d: %s (created after %d) dissolved into %s", edition, c, edition, cands[0])
    missing = sorted(set(q.index) - set(m["municipio_code"]))
    if missing:
        errors.append(f"{edition}: edition codes not in the {mesh} mesh: {missing[:10]}")
    if errors:
        return {}, errors

    m = m[~m["municipio_code"].isin(drop["municipio_code"])].copy()
    if cfg["simplify_tolerance"]:
        m["geometry"] = shapely.coverage_simplify(m.geometry.values, cfg["simplify_tolerance"])
    m["target"] = m["municipio_code"].map(lambda c: parent.get(c, c))
    geom = dissolve(m, m["target"]) if parent else m.set_index("target").geometry
    merged = m[m["target"] != m["municipio_code"]].groupby("target")["municipio_code"].agg(" ".join)
    codes = sorted(q.index)
    mun = gpd.GeoDataFrame({"municipio_code": codes, "municipio_name": q.loc[codes, "municipio_name"].values,
                            "unidade_code": q.loc[codes, "unidade_code"].values,
                            "merged_codes": [merged.get(c, "NA") for c in codes], "mesh_year": mesh},
                           geometry=geom.reindex(codes).values, crs=m.crs)
    out = {}
    for name in layers:
        if name == "municipios":
            out[name] = mun
        elif name == "sedes":
            s = pyogrio.read_dataframe(folder / cfg["mesh_pattern"].format(NAME="SEDES", MESH=mesh))
            s = s[s["municipio_code"].isin(q.index)].sort_values("municipio_code").copy()
            s["municipio_name"] = q.loc[s["municipio_code"], "municipio_name"].values
            if set(s["municipio_code"]) != set(q.index):
                logger.warning("  %d: %d municipalities without a seat in SEDES %d", edition, len(q) - len(s), mesh)
            out[name] = s.reset_index(drop=True)
        elif name in LEVELS:
            code, label = LEVELS[name]
            qq = q
            if name in ed.get("codes_from", {}):
                src = int(ed["codes_from"][name])
                qq = quadro[quadro["edition"] == str(src)].set_index("municipio_code")
                if set(qq.index) != set(codes):
                    errors.append(f"{edition}: {name} codes_from {src} has other municipalities")
                    continue
                logger.info("  %d: %s codes from quadro edition %d", edition, name, src)
            by = qq.loc[codes, code]
            if (by == "").any() or (by == "NA").any():
                errors.append(f"{edition}: {name} requested, {int(((by == '') | (by == 'NA')).sum())} municipalities without {code}")
                continue
            g = dissolve(mun, by, cfg["fill_holes"])
            names = qq.drop_duplicates(code).set_index(code)[label]
            out[name] = gpd.GeoDataFrame({code: g.index, label: names.reindex(g.index).values}, geometry=g.values, crs=mun.crs)
        elif name == "pais":
            out[name] = gpd.GeoDataFrame({"pais_name": [cfg["pais_name"]]},
                                         geometry=[fill_holes(shapely.union_all(mun.geometry.values), cfg["fill_holes"])], crs=mun.crs)
        else:
            errors.append(f"unknown layer {name}")
    for name, g in out.items():
        bad = (~g.geometry.is_valid) | g.geometry.is_empty | g.geometry.isna()
        if bad.any():
            errors.append(f"{edition} {name}: {int(bad.sum())} invalid or empty geometries")
    return out, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    out = Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    quadro = pd.read_csv(cfg["dtb_quadro"], sep=";", dtype=str, keep_default_na=False)
    lineage = pd.read_csv(cfg["dtb_lineage"], sep=";", dtype=str, keep_default_na=False,
                          usecols=["year_from", "year_to", "code_from", "code_to", "relation"])
    failed = False
    for ed in cfg["editions"]:
        year = int(ed.get("year", ed["edition"]))
        logger.info("Edition %s from the %s mesh", ed["edition"], ed["mesh"])
        layers, errors = build_edition(ed, cfg, quadro, lineage, logger)
        if errors:
            for e in errors:
                logger.error(e)
            failed = True
            continue
        for name, g in layers.items():
            dest = out / cfg["name_pattern"].format(YEAR=year, NAME=name.upper())
            if dest.exists() and not args.overwrite:
                logger.error("%s exists (use --overwrite)", dest.name)
                failed = True
                continue
            part = dest.with_name(dest.stem + ".part.gpkg")
            if part.exists():
                part.unlink()
            pyogrio.write_dataframe(g, part, layer=cfg["layer_pattern"].format(year=year, name=name), driver="GPKG")
            part.replace(dest)
            logger.info("  %s: %d features, %.1f MB", dest.name, len(g), dest.stat().st_size / 1e6)
    if failed:
        sys.exit(1)
    logger.info("Done")


if __name__ == "__main__":
    main()

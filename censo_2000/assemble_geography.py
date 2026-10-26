"""
Assemble IBGE Census 2000 geography from T0/legacy to Tier 1.

Sources
  * legacy_gpkg (T1/ASSETS/legacy/censo2000_malhas.gpkg)
      rural layer: the national 1:500,000 sector mesh (SRC 55se500g). Codes are either a sector
                   (15 digits) or a RANGE of urban sectors 'UFMMMMMDDSDSSSS-NNNN' (20 chars) that
                   covers an urban area as one filled polygon.
      urban layer: detailed urban sectors (one shapefile per municipality, unified by the user).
  * urban_t0_folder: the per-municipality urban shapefiles, used only for the municipalities in
    urban_reproject (their PRJ named the wrong UTM zone; the zone is chosen by overlap with the
    municipality polygon, or given explicitly).
  * admin_folder: the 1:500,000 administrative meshes (55uf/55mu/55ds/55sd500g) for unidades,
    municipios, distritos, subdistritos, and the municipal seats (sedes) of 55mu500g.
  * basico_csv: T1 basico table (assemble_tables.py output); it leads codes and names.

Geometry repair
  The 500k meshes store a polygon with holes as the filled outer polygon PLUS one extra feature
  per hole (same code, same MSLINK). Per code, parts are combined with an even-odd rule: largest
  first; a part covered (>= hole_cover_threshold) by what is already built is a hole and is
  subtracted, any other part is added (true multipart). Invalid geometries are made valid.

Outputs (one GeoPackage per layer, IBGE_CENSO-2000_<NAME>_BRASIL_2000.gpkg, layer
ibge_censo_2000_<name>, CRS label output_crs; coordinates are NOT transformed, see FIELDS/docs):
  * setores        one row per sector code: every basico sector (geometry NULL when the sector has
                   no own polygon) + polygon codes absent from basico. Columns setor_code,
                   grupo_code (range that lists the sector), geometry_source (rural/urban/none).
                   A sector with both a rural and an urban polygon takes the urban one.
  * setoresgrupos  range polygons: grupo_code, municipio_code, n_setores (basico members),
                   n_setores_geometry (members with own polygon), n_codes_not_in_basico.
  * unidades, municipios, distritos, subdistritos: <entity>_code, <entity>_name (basico name,
                   decoded source name when the code is not in basico); code '0' features dropped.
  * sedes          points (one per municipality) from 55mu500g seat coordinates.
  * consistency table (CSV, consistency_csv): one row per finding for later revisions
    (layer;code;issue;value;action;note).

Usage:
    python assemble_geography.py --config assemble_geography.local.json
    python assemble_geography.py --config assemble_geography.local.json --layers municipios,sedes --overwrite
"""

import argparse
import csv
import json
import logging
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

REQUIRED_KEYS = ["legacy_gpkg", "rural_layer", "urban_layer", "urban_t0_folder", "admin_folder",
                 "basico_csv", "output_folder", "report_folder", "consistency_csv", "admin"]
LAYERS = ["unidades", "municipios", "distritos", "subdistritos", "sedes", "setores", "setoresgrupos"]


# ---------------------------------------------------------------------------
# Configuration / logging
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2000_{NAME}_BRASIL_2000.gpkg")
    cfg.setdefault("layer_pattern", "ibge_censo_2000_{name}")
    cfg.setdefault("output_crs", "EPSG:4674")
    cfg.setdefault("metric_crs", "EPSG:5880")
    cfg.setdefault("source_encoding", "cp850")
    cfg.setdefault("hole_cover_threshold", 0.9)
    cfg.setdefault("urban_reproject", {})
    cfg.setdefault("urban_zone_candidates", list(range(18, 26)))
    cfg.setdefault("urban_utm_epsg_base", 32700)
    cfg.setdefault("offset_threshold_km", 5.0)
    cfg.setdefault("prefer_urban", True)
    cfg.setdefault("text_no_data", "NA")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"geography_log_{timestamp}.txt"
    logger = logging.getLogger("censo2000_geography")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
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


class Findings:
    """Rows of the consistency table."""

    FIELDS = ["layer", "code", "issue", "value", "action", "note"]

    def __init__(self):
        self.rows = []

    def add(self, layer, code, issue, value="", action="none", note=""):
        if isinstance(value, float):
            value = f"{value:.4g}"
        self.rows.append(dict(layer=layer, code=code, issue=issue, value=value, action=action,
                              note=note))

    def write(self, path: Path):
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS, delimiter=";", lineterminator="\n")
            w.writeheader()
            w.writerows(self.rows)
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def valid(geoms):
    return shapely.make_valid(np.asarray(geoms, dtype=object), method="structure",
                              keep_collapsed=False)


def polygonal(g):
    """Keep only the polygonal part of a geometry (make_valid can return collections)."""
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    polys = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")]
    return shapely.union_all(polys) if polys else None


def even_odd(parts, thr: float):
    """Combine the parts of one code: covered parts are holes, the others are added.
    Returns (geometry, n_holes, n_islands)."""
    parts = sorted((p for p in parts if p is not None and not p.is_empty),
                   key=lambda p: -p.area)
    acc = parts[0]
    holes = islands = 0
    for p in parts[1:]:
        inside = acc.intersection(p).area / p.area if p.area else 1.0
        if inside >= thr:
            acc = acc.difference(p)
            holes += 1
        else:
            acc = acc.union(p)
            islands += 1
    return polygonal(acc), holes, islands


def combine_by_code(gdf: gpd.GeoDataFrame, code_col: str, thr: float, logger, label: str):
    """One row per code with the even-odd rule applied to repeated codes."""
    gdf = gdf.copy()
    gdf["geometry"] = valid(gdf.geometry.values)
    dup = gdf[code_col].duplicated(keep=False)
    single = gdf[~dup][[code_col, "geometry"]]
    rows, holes, islands = [], 0, 0
    for code, grp in gdf[dup].groupby(code_col, sort=False):
        g, h, i = even_odd(list(grp.geometry), thr)
        rows.append((code, g))
        holes += h
        islands += i
    multi = gpd.GeoDataFrame(rows, columns=[code_col, "geometry"], geometry="geometry",
                             crs=gdf.crs)
    out = pd.concat([single, multi], ignore_index=True)
    logger.info("  %s: %d features -> %d codes (%d repeated codes: %d holes cut, %d parts added)",
                label, len(gdf), len(out), len(multi), holes, islands)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs), holes, islands


def to_metric(gs: gpd.GeoSeries, crs: str) -> gpd.GeoSeries:
    m = gs.to_crs(crs)
    return gpd.GeoSeries(valid(m.values), index=m.index, crs=crs)


def iou(a, b) -> float:
    if a is None or b is None:
        return float("nan")
    inter = a.intersection(b).area
    union = a.area + b.area - inter
    return inter / union if union else float("nan")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def read_basico(cfg: dict) -> pd.DataFrame:
    cols = ["setor_code", "unidade_code", "unidade_name", "municipio_code", "municipio_name",
            "distrito_code", "distrito_name", "subdistrito_code", "subdistrito_name"]
    return pd.read_csv(cfg["basico_csv"], sep=";", dtype=str, usecols=cols,
                       keep_default_na=False)


def read_admin(cfg: dict, source: str) -> gpd.GeoDataFrame:
    path = Path(cfg["admin_folder"]) / source
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        g = gpd.read_file(path, encoding=cfg["source_encoding"])
    return g.set_crs(cfg["output_crs"], allow_override=True)


def read_legacy(cfg: dict, layer: str) -> gpd.GeoDataFrame:
    g = gpd.read_file(cfg["legacy_gpkg"], layer=layer)
    return g.set_crs(cfg["output_crs"], allow_override=True)


# ---------------------------------------------------------------------------
# Admin layers and seats
# ---------------------------------------------------------------------------

def build_admin(name: str, cfg: dict, basico: pd.DataFrame, find: Findings, logger):
    spec = cfg["admin"][name]
    nd = cfg["text_no_data"]
    ent = spec["entity"]
    code_col, name_col = f"{ent}_code", f"{ent}_name"
    g = read_admin(cfg, spec["source"])
    g = g.rename(columns={"GEOCODIGO": code_col, "NOME": "source_name"})
    dropped = g[~g[code_col].str.fullmatch(rf"\d{{{spec['code_length']}}}")]
    for code, n in dropped[code_col].value_counts().items():
        find.add(name, code, "feature_code_invalid", n, "dropped",
                 f"{n} feature(s) with code '{code}' (not {spec['code_length']} digits)")
    g = g.drop(dropped.index)
    src_names = g.drop_duplicates(code_col).set_index(code_col)["source_name"]
    out, _, _ = combine_by_code(g[[code_col, "geometry"]], code_col, cfg["hole_cover_threshold"],
                                logger, name)
    names = basico.drop_duplicates(code_col).set_index(code_col)[name_col]
    out[name_col] = out[code_col].map(names)
    missing = out[name_col].isna()
    for code in out.loc[missing, code_col]:
        find.add(name, code, "code_not_in_basico", src_names.get(code, ""), "kept",
                 "polygon code absent from the sector tables; name from source")
    out.loc[missing, name_col] = out.loc[missing, code_col].map(src_names).fillna(nd)
    for code in sorted(set(names.index) - set(out[code_col])):
        find.add(name, code, "basico_code_without_polygon", names[code], "none",
                 "code used by sectors in basico but absent from the 500k mesh")
    logger.info("  %s: %d polygons, %d codes not in basico, %d basico codes without polygon",
                name, len(out), int(missing.sum()), len(set(names.index) - set(out[code_col])))
    return out[[code_col, name_col, "geometry"]]


def build_sedes(cfg: dict, basico: pd.DataFrame, find: Findings, logger):
    spec = cfg["sedes"]
    g = read_admin(cfg, spec["source"])
    g = g[g[spec["flag"]].astype(str).isin(["True", "1", "S", "true"])]
    g = g[g["GEOCODIGO"].str.fullmatch(r"\d{7}")]
    n_coords = g.groupby("GEOCODIGO")[[spec["x"], spec["y"]]].nunique().max(axis=1)
    for code in n_coords[n_coords > 1].index:
        find.add("sedes", code, "seat_coordinates_differ", int(n_coords[code]), "first kept",
                 "the municipality polygons carry different seat coordinates")
    g = g.drop_duplicates("GEOCODIGO")
    names = basico.drop_duplicates("municipio_code").set_index("municipio_code")["municipio_name"]
    out = gpd.GeoDataFrame(
        {"municipio_code": g["GEOCODIGO"].to_numpy(),
         "municipio_name": g["GEOCODIGO"].map(names).fillna(cfg["text_no_data"]).to_numpy()},
        geometry=gpd.points_from_xy(g[spec["x"]].astype(float), g[spec["y"]].astype(float)),
        crs=cfg["output_crs"])
    for code in sorted(set(names.index) - set(out.municipio_code)):
        find.add("sedes", code, "basico_code_without_seat", names[code], "none", "")
    logger.info("  sedes: %d points", len(out))
    return out


# ---------------------------------------------------------------------------
# Sectors and range groups
# ---------------------------------------------------------------------------

def expand_ranges(codes) -> pd.DataFrame:
    """'UFMMMMMDDSDSSSS-NNNN' -> one row per listed sector code."""
    rows = []
    for c in codes:
        start, end = c.split("-")
        base, first = start[:11], int(start[11:])
        for i in range(first, int(end) + 1):
            rows.append((c, f"{base}{i:04d}"))
    return pd.DataFrame(rows, columns=["grupo_code", "setor_code"])


def zone_fit(t0: gpd.GeoDataFrame, zone: int, muni_geom, cfg: dict):
    """(share of the urban map inside the municipality, distance in degrees) for one zone."""
    g = t0.set_crs(cfg["urban_utm_epsg_base"] + zone, allow_override=True).to_crs(cfg["output_crs"])
    u = shapely.union_all(valid(g.geometry.values))
    share = u.intersection(muni_geom).area / u.area if u.area else 0.0
    return share, u.distance(muni_geom)


def pick_zone(t0: gpd.GeoDataFrame, muni_geom, cfg: dict):
    """UTM zone whose reprojection overlaps the municipality polygon the most; when no zone
    overlaps, the zone that lands nearest to it."""
    fits = {z: zone_fit(t0, z, muni_geom, cfg) for z in cfg["urban_zone_candidates"]}
    z = max(fits, key=lambda k: (fits[k][0], -fits[k][1]))
    return fits[z][0], fits[z][1], z


def urban_layer(cfg: dict, municipios: gpd.GeoDataFrame, find: Findings, logger):
    u = read_legacy(cfg, cfg["urban_layer"]).rename(columns={cfg["urban_code"]: "setor_code"})
    u = u[["setor_code", "geometry"]]
    mun_geom = municipios.set_index("municipio_code").geometry
    fixed = []
    for mun, zone in cfg["urban_reproject"].items():
        shp = Path(cfg["urban_t0_folder"]) / f"{mun}.SHP"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            t0 = gpd.read_file(shp)
        prj = shp.with_suffix(".PRJ")
        m = re.search(r"ZONE\s+(\d+\w?)", prj.read_text(errors="replace")) if prj.exists() else None
        prj_zone = m.group(1) if m else "?"
        if zone == "auto":
            share, dist, zone = pick_zone(t0, mun_geom[mun], cfg)
        else:
            share, dist = zone_fit(t0, int(zone), mun_geom[mun], cfg)
        t0 = t0.set_crs(cfg["urban_utm_epsg_base"] + int(zone), allow_override=True)
        t0 = t0.to_crs(cfg["output_crs"]).rename(columns={cfg["urban_t0_code"]: "setor_code"})
        t0 = t0[["setor_code", "geometry"]]
        n_old = int(u.setor_code.str.startswith(mun).sum())
        u = u[~u.setor_code.str.startswith(mun)]
        fixed.append(t0)
        dist_km = dist * 111.0
        note = (f"{len(t0)} urban sectors (legacy had {n_old}); {share:.1%} inside the "
                f"municipality polygon after the fix")
        if share == 0:
            note += f"; STILL OUTSIDE: no zone overlaps, nearest at ~{dist_km:.0f} km"
        find.add("setores", mun, "urban_wrong_utm_zone", f"{prj_zone}->{zone}S",
                 "reprojected from T0", note)
        logger.info("  urban %s: PRJ zone %s -> %sS, %d sectors, %.1f%% inside municipality%s",
                    mun, prj_zone, zone, len(t0), 100 * share,
                    f" (STILL OUTSIDE, ~{dist_km:.0f} km)" if share == 0 else "")
    u = pd.concat([u] + fixed, ignore_index=True)
    u = gpd.GeoDataFrame(u, geometry="geometry", crs=cfg["output_crs"])
    out, _, _ = combine_by_code(u, "setor_code", cfg["hole_cover_threshold"], logger, "urban")
    return out


def build_setores(cfg: dict, basico: pd.DataFrame, municipios: gpd.GeoDataFrame,
                  find: Findings, logger):
    nd_src = "none"
    r = read_legacy(cfg, cfg["rural_layer"]).rename(columns={cfg["rural_code"]: "code"})
    r = r[["code", "geometry"]]
    is_range = r.code.str.fullmatch(r"\d{15}-\d{4}")
    is_setor = r.code.str.fullmatch(r"\d{15}")
    for code in r.loc[~is_range & ~is_setor, "code"].unique():
        find.add("setores", code, "rural_code_invalid", "", "dropped", "")
    rural, _, _ = combine_by_code(r[is_setor].rename(columns={"code": "setor_code"}), "setor_code",
                                  cfg["hole_cover_threshold"], logger, "rural sectors")
    grupos, _, _ = combine_by_code(r[is_range].rename(columns={"code": "grupo_code"}),
                                   "grupo_code", cfg["hole_cover_threshold"], logger,
                                   "range polygons")
    urban = urban_layer(cfg, municipios, find, logger)

    keys = set(basico.setor_code)
    # sectors with both a rural and an urban polygon
    both = sorted(set(rural.setor_code) & set(urban.setor_code))
    if both:
        rg = to_metric(rural.set_index("setor_code").geometry.loc[both], cfg["metric_crs"])
        ug = to_metric(urban.set_index("setor_code").geometry.loc[both], cfg["metric_crs"])
        keep = "urban" if cfg["prefer_urban"] else "rural"
        for code in both:
            find.add("setores", code, "rural_and_urban_polygon", iou(rg[code], ug[code]),
                     f"{keep} kept", "value = IoU of the two polygons")
    if cfg["prefer_urban"]:
        rural = rural[~rural.setor_code.isin(both)]
    else:
        urban = urban[~urban.setor_code.isin(both)]
    geo = pd.concat([rural.assign(geometry_source="rural"),
                     urban.assign(geometry_source="urban")], ignore_index=True)

    # range membership
    exp = expand_ranges(grupos.grupo_code)
    multi = exp[exp.setor_code.duplicated(keep=False)]
    for code, grp in multi.groupby("setor_code"):
        find.add("setores", code, "setor_in_several_grupos", " | ".join(grp.grupo_code),
                 "narrowest range kept", "")
    exp["span"] = exp.grupo_code.map(lambda c: int(c[16:]) - int(c[11:15]))
    link = exp.sort_values("span").drop_duplicates("setor_code").set_index("setor_code").grupo_code

    for code in sorted(set(geo.setor_code) - keys):
        find.add("setores", code, "polygon_code_not_in_basico", "", "kept", "")
    no_geom = sorted(keys - set(geo.setor_code))
    missing = gpd.GeoDataFrame({"setor_code": no_geom, "geometry_source": nd_src},
                               geometry=[None] * len(no_geom), crs=cfg["output_crs"])
    setores = pd.concat([geo, missing], ignore_index=True)
    setores["grupo_code"] = setores.setor_code.map(link).fillna(cfg["text_no_data"])
    setores = gpd.GeoDataFrame(setores[["setor_code", "grupo_code", "geometry_source", "geometry"]],
                               geometry="geometry", crs=cfg["output_crs"])
    setores = setores.sort_values("setor_code", ignore_index=True)
    lost = setores[(setores.geometry_source == nd_src) & (setores.grupo_code == cfg["text_no_data"])]
    for code in lost.setor_code:
        find.add("setores", code, "setor_without_any_polygon", "", "none",
                 "no own polygon and no range lists it")
    urb_out = urban[~urban.setor_code.isin(link.index)]
    for code in urb_out.setor_code:
        find.add("setores", code, "urban_setor_not_in_any_grupo", "", "none", "")
    logger.info("  setores: %d rows (%d rural, %d urban, %d without polygon of which %d in no "
                "range); %d sectors with both polygons",
                len(setores), int((setores.geometry_source == "rural").sum()),
                int((setores.geometry_source == "urban").sum()), len(no_geom), len(lost), len(both))

    # range groups
    member = exp.drop_duplicates(["grupo_code", "setor_code"])
    member["in_basico"] = member.setor_code.isin(keys)
    member["has_geom"] = member.setor_code.isin(set(geo.setor_code))
    st = member.groupby("grupo_code").agg(n_setores=("in_basico", "sum"),
                                          n_setores_geometry=("has_geom", "sum"),
                                          n_codes_not_in_basico=("in_basico", lambda s: (~s).sum()))
    grupos = grupos.join(st, on="grupo_code")
    grupos.insert(1, "municipio_code", grupos.grupo_code.str[:7])
    for c in ["n_setores", "n_setores_geometry", "n_codes_not_in_basico"]:
        grupos[c] = grupos[c].astype("int64")
    for _, g in grupos[grupos.n_codes_not_in_basico > 0].iterrows():
        find.add("setoresgrupos", g.grupo_code, "grupo_lists_codes_not_in_basico",
                 int(g.n_codes_not_in_basico), "none", "numbering gaps inside the range")
    part = grupos[(grupos.n_setores_geometry > 0) & (grupos.n_setores_geometry < grupos.n_setores)]
    for _, g in part.iterrows():
        find.add("setoresgrupos", g.grupo_code, "grupo_partially_mapped",
                 f"{g.n_setores_geometry}/{g.n_setores}", "none",
                 "only some member sectors have an own polygon")
    urban_fit(grupos, urban, link, cfg, find, logger)
    logger.info("  setoresgrupos: %d ranges, %d fully mapped, %d partially, %d without detail",
                len(grupos), int((grupos.n_setores_geometry >= grupos.n_setores).sum()),
                len(part), int((grupos.n_setores_geometry == 0).sum()))
    return setores, gpd.GeoDataFrame(grupos, geometry="geometry", crs=cfg["output_crs"])


def urban_fit(grupos, urban, link, cfg: dict, find: Findings, logger):
    """Per municipality: median centroid offset between each range polygon and the union of its
    urban sectors. Municipalities above offset_threshold_km go to the consistency table."""
    u = urban.assign(grupo_code=urban.setor_code.map(link)).dropna(subset=["grupo_code"])
    u = u.assign(geometry=to_metric(u.geometry, cfg["metric_crs"]).values)
    uu = u.groupby("grupo_code").geometry.apply(lambda s: shapely.union_all(s.values))
    gg = to_metric(grupos.set_index("grupo_code").geometry, cfg["metric_crs"]).loc[uu.index]
    d = pd.DataFrame({
        "offset": [a.centroid.distance(b.centroid) for a, b in zip(gg.values, uu.values)],
        "iou": [iou(a, b) for a, b in zip(gg.values, uu.values)]}, index=uu.index)
    d["mun"] = d.index.str[:7]
    m = d.groupby("mun").agg(n=("iou", "size"), offset=("offset", "median"), iou=("iou", "median"))
    bad = m[m.offset > 1000 * cfg["offset_threshold_km"]].sort_values("offset", ascending=False)
    for mun, row in bad.iterrows():
        find.add("setores", mun, "urban_offset_from_rural_mesh", round(row.offset / 1000, 2),
                 "none (revision)",
                 f"km, median over {int(row.n)} range(s); median IoU {row.iou:.2f}")
    logger.info("  urban fit: %d municipalities, median offset %.0f m; %d above %.1f km",
                len(m), m.offset.median(), len(bad), cfg["offset_threshold_km"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def write_layer(gdf: gpd.GeoDataFrame, name: str, cfg: dict, overwrite: bool, logger):
    dst = Path(cfg["output_folder"]) / cfg["name_pattern"].format(NAME=name.upper())
    tmp = dst.with_name(dst.stem + ".part.gpkg")
    if tmp.exists():
        tmp.unlink()
    pyogrio.write_dataframe(gdf, tmp, layer=cfg["layer_pattern"].format(name=name),
                            driver="GPKG", promote_to_multi=gdf.geom_type.isin(
                                ["Polygon", "MultiPolygon"]).any())
    tmp.replace(dst)
    logger.info("  wrote %s (%d rows)", dst.name, len(gdf))


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--layers", help=f"Comma-separated subset of {LAYERS}")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    layers = LAYERS if not args.layers else [x.strip() for x in args.layers.split(",")]
    unknown = [x for x in layers if x not in LAYERS]
    if unknown:
        sys.exit(f"Unknown layers: {unknown}")
    out_folder = Path(cfg["output_folder"])
    out_folder.mkdir(parents=True, exist_ok=True)
    todo = []
    for name in layers:
        dst = out_folder / cfg["name_pattern"].format(NAME=name.upper())
        if dst.exists() and not args.overwrite:
            logger.info("%s: SKIP (output exists, use --overwrite)", name)
        else:
            todo.append(name)
    if not todo:
        return

    logger.info("=" * 60)
    logger.info("IBGE Censo 2000 — geography T0/legacy -> T1: %s", ", ".join(todo))
    logger.info("=" * 60)
    basico = read_basico(cfg)
    find = Findings()
    built = {}
    # municipios are needed to place the re-projected urban maps
    need = set(todo) | ({"municipios"} if {"setores", "setoresgrupos"} & set(todo) else set())
    for name in ["unidades", "municipios", "distritos", "subdistritos"]:
        if name in need:
            logger.info("[%s]", name)
            built[name] = build_admin(name, cfg, basico, find, logger)
    if "sedes" in need:
        logger.info("[sedes]")
        built["sedes"] = build_sedes(cfg, basico, find, logger)
    if {"setores", "setoresgrupos"} & need:
        logger.info("[setores + setoresgrupos]")
        built["setores"], built["setoresgrupos"] = build_setores(cfg, basico, built["municipios"],
                                                                 find, logger)
    for name in todo:
        write_layer(built[name], name, cfg, args.overwrite, logger)

    cpath = Path(cfg["consistency_csv"])
    if set(todo) == set(LAYERS):
        find.write(cpath)
        logger.info("Consistency table: %s (%d rows)", cpath, len(find.rows))
    else:
        part = Path(cfg["report_folder"]) / "consistency_partial_run.csv"
        find.write(part)
        logger.info("Partial run: consistency rows written to %s (%d rows), T1 table untouched",
                    part, len(find.rows))
    counts = pd.Series([r["issue"] for r in find.rows]).value_counts()
    for issue, n in counts.items():
        logger.info("  %-36s %d", issue, n)
    logger.info("DONE")


if __name__ == "__main__":
    main()

"""
Assemble the IBGE DTB geography for T1: one GeoPackage per layer and snapshot year.

Snapshots (config "snapshots"):
  1872 ... 1991  IBGE "Evolução da divisão territorial do Brasil 1872-2010" meshes (built by IBGE by
                 joining polygons of the 1991 digital mesh, so all of them nest in the 1991 polygons):
                 05 malha municipal, 03 sede municipal, 01/02 federal and state capitals, 04 state limits,
                 06 litígio (disputed areas, 1940-1991). Folder DTB_1993 holds the 1933 files.
  2000           1:500,000 municipal mesh per UF (no .prj; coordinates labelled EPSG:4674 without
                 transformation, as in the census 2000 T1); seats from the SEDE/LATITUDESE/LONGITUDES columns
  2010           1:250,000 municipal mesh per UF (SIRGAS 2000); seats from the 2022 seats (below) without
                 the 5 municipalities installed in 2013 (dropped by code, "drop_unknown" of the seats source)
  2022           BR_Municipios_2022 and BR_UF_2022, plus the 2022 regional layers (pais, regioes,
                 mesorregioes, microrregioes, regioesintermediarias, regioesimediatas); seats from the census
                 2022 T1 sedes layer (IBGE BR_municipios_CD2022 sedes_municipais)

Sources are a shapefile in a zip ("zip"), a glob of zips ("zips_glob") or a GeoPackage ("gpkg", optional
"layer"), with paths relative to src_folder.

Per snapshot (all in output_crs, area_km2 computed in area_crs, includes water bodies):
  municipios  municipio_code, municipio_name, unidade_code, area_type, area_km2 — one (multi)polygon per
              code; area_type municipio, lagoa (Lagoa Mirim 4300001, Lagoa dos Patos 4300002), litigio
              (disputed area, municipio_code NA) or ilha_nao_atribuida (1991 islands without a municipality)
  sedes       municipio_code, municipio_name, capital_type (federal / estadual / NA) — one point per code
  unidades    unidade_code, unidade_name, area_km2 — the source state limits (code assigned by the largest
              overlap with the municipalities of the same year) or, in 2000/2010, dissolved municipalities

Names (config "names", per snapshot an ordered list of sources): quadro (T1 quadro-municipios of the same
edition), genealogy (T1 genealogia names of that census year), mesh (the source layer). The first source
that has the code is used; disagreements go to report names_differences.csv.

Fixes and findings go to CONSISTENCY (layers municipios / sedes / unidades / regional 2022 layers; rows of
other layers kept): code_fixes (mesh codes that differ from the genealogy), codes of the mesh not in the
genealogy and genealogy codes without a polygon, seats outside their polygon, duplicated or unassigned
seats, capitals not matching a seat.

Usage:
    python assemble_geography.py --config assemble_geography.local.json [--overwrite] [--years 1872 1991 ...]
"""

import argparse
import sys
import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely import make_valid
from shapely.geometry import MultiPolygon, Point
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dtbtools import NA, clean_name, fold, load_config, merge_consistency, norm_code, setup_logging  # noqa: E402

warnings.filterwarnings("ignore", message=".*not successfully written.*")
REQUIRED_KEYS = ["src_folder", "output_folder", "report_folder", "name_pattern", "layer_pattern", "consistency_name",
                 "output_crs", "area_crs", "quadro_csv", "genealogy_csv", "special_codes", "snapshots"]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def shp_in_zip(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as z:
        shps = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if len(shps) != 1:
        raise SystemExit(f"{zip_path}: expected one .shp, found {shps}")
    return f"/vsizip/{zip_path}/{shps[0]}"


def read_source(src: Path, spec: dict) -> gpd.GeoDataFrame:
    """A source layer (one zip, a glob of per-UF zips, or a GeoPackage) with columns code / name (if configured) / geometry."""
    if "gpkg" in spec:
        paths = [str(src / spec["gpkg"])]
        if not Path(paths[0]).exists():
            raise SystemExit(f"No source for {spec}")
    else:
        zips = sorted(src.glob(spec["zips_glob"])) if "zips_glob" in spec else [src / spec["zip"]]
        if not zips:
            raise SystemExit(f"No source for {spec}")
        paths = [shp_in_zip(z) for z in zips]
    parts = []
    for p in paths:
        g = pyogrio.read_dataframe(p, layer=spec.get("layer"), encoding=spec.get("encoding"))
        if g.crs is None:
            g = g.set_crs(spec["crs"])
        parts.append(g)
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=parts[0].crs)
    out = gpd.GeoDataFrame({"geometry": g.geometry}, geometry="geometry", crs=g.crs)
    out["code"] = g[spec["code"]].map(norm_code) if spec.get("code") else ""
    out["name"] = g[spec["name"]].map(clean_name) if spec.get("name") else NA
    for k in spec.get("keep", []):
        out[k] = g[k]
    return out


def polygonal(geom):
    """Valid (multi)polygon; collections returned by make_valid lose their lines and points."""
    if geom is None or geom.is_empty:
        return None
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
    return MultiPolygon(polys) if polys else None


def dissolve(g: gpd.GeoDataFrame, key: str, first: list) -> gpd.GeoDataFrame:
    rows = []
    for k, grp in g.groupby(key, sort=True, dropna=False):
        geom = polygonal(unary_union([make_valid(x) for x in grp.geometry]))
        row = {key: k, "geometry": geom}
        for c in first:
            row[c] = grp[c].iloc[0]
        rows.append(row)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=g.crs)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def name_sources(cfg: dict) -> dict:
    q = pd.read_csv(cfg["quadro_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["edition", "municipio_code", "municipio_name"])
    g = pd.read_csv(cfg["genealogy_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["year", "municipio_code", "municipio_name"])
    g = g[g["municipio_code"] != NA]
    return {"quadro": {int(e): dict(zip(d["municipio_code"], d["municipio_name"])) for e, d in q.groupby("edition")},
            "genealogy": {int(y): dict(zip(d["municipio_code"], d["municipio_name"])) for y, d in g.groupby("year")}}


def pick_names(year: int, codes: pd.Series, mesh_names: pd.Series, order: list, sources: dict, diffs: list) -> list:
    out = []
    for code, mname in zip(codes, mesh_names):
        cands = {}
        for s in order:
            if s == "mesh":
                v = mname
            else:
                v = sources[s].get(year, {}).get(code, NA)
            if v not in (NA, "", None):
                cands[s] = v
        chosen = next((cands[s] for s in order if s in cands), NA)
        if len({fold(v) for v in cands.values()}) > 1:
            diffs.append(dict(year=year, municipio_code=code, chosen=chosen,
                              **{f"name_{s}": cands.get(s, NA) for s in ("quadro", "genealogy", "mesh")}))
        out.append(chosen)
    return out


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

def cut_enclaves(d: gpd.GeoDataFrame, year: int, cfg: dict, cons: list, logger) -> gpd.GeoDataFrame:
    """Polygons covered by another one for >= enclave_share of their area (an enclave left uncut in the
    surrounding municipality): the enclave is subtracted from the surrounding polygon."""
    share = cfg.get("enclave_share", 0.5)
    a = d.to_crs(cfg["area_crs"])
    left, right = a.sindex.query(a.geometry, predicate="intersects")
    keep = left != right
    d = d.copy()
    for i, j in zip(left[keep], right[keep]):
        gi, gj = a.geometry.iloc[i], a.geometry.iloc[j]
        if gi.area > gj.area:
            continue
        inter = gi.intersection(gj).area
        if inter / gi.area >= share:
            ci, cj = d["municipio_code"].iloc[i], d["municipio_code"].iloc[j]
            d.iloc[j, d.columns.get_loc("geometry")] = polygonal(d.geometry.iloc[j].difference(d.geometry.iloc[i]))
            cons.append(dict(layer="municipios", code=cj, issue="enclave_cut", value=f"{year}: {ci} ({inter / 1e6:.1f} km², {inter / gi.area:.0%})",
                             action=f"{ci} subtracted from {cj}", note="Polígono contido em outro (enclave não recortado na malha de origem)"))
            logger.info("  %d: enclave %s cut out of %s (%.1f km2)", year, ci, cj, inter / 1e6)
    return d


def build_municipios(year: int, sp: dict, cfg: dict, sources: dict, gen_codes: set, cons: list, diffs: list, logger):
    src = Path(cfg["src_folder"])
    g = read_source(src, sp["mesh"])
    n0 = len(g)
    g = g[~g.geometry.isna() & ~g.geometry.is_empty].copy()
    if len(g) < n0:
        cons.append(dict(layer="municipios", code=NA, issue="empty_geometry_dropped", value=f"{year}: {n0 - len(g)}",
                         action="dropped", note="Registro da malha sem geometria"))
    fixes = sp.get("code_fixes", {})
    notes = sp.get("code_fix_notes", {})
    for bad, good in fixes.items():
        if not (g["code"] == bad).any():
            raise SystemExit(f"{year} code_fixes: {bad} not in the mesh")
        cons.append(dict(layer="municipios", code=good, issue="mesh_code_fixed", value=f"{year}: {bad}",
                         action=f"code set to {good}", note=notes.get(bad, sp.get("code_fixes_note", "Código da malha difere da genealogia do IBGE"))))
    g["code"] = g["code"].map(lambda c: fixes.get(c, c))  # one mapping: swaps work
    for code, why in sp.get("drop_codes", {}).items():
        m = g["code"] == code
        if not m.any():
            raise SystemExit(f"{year} drop_codes: {code} not in the mesh")
        g = g[~m]
        cons.append(dict(layer="municipios", code=code, issue="mesh_polygon_dropped", value=f"{year}: {int(m.sum())} polígono(s)",
                         action="dropped", note=why))
    g["municipio_code"] = g["code"]
    g["area_type"] = "municipio"
    g["special_name"] = NA
    for spa in sp.get("special_polygons", []):
        m = g["code"] == spa["code"]
        if "mesh_name" in spa:
            m &= g["name"].eq(NA) if spa["mesh_name"] is None else g["name"].eq(spa["mesh_name"])
        if not m.any():
            raise SystemExit(f"{year} special polygon not found: {spa}")
        g.loc[m, "municipio_code"] = spa["municipio_code"] if spa["municipio_code"] != NA else f"__{spa['area_type']}"
        g.loc[m, "area_type"] = spa["area_type"]
        g.loc[m, "special_name"] = spa["name"]
        cons.append(dict(layer="municipios", code=spa["municipio_code"], issue=f"special_area_{spa['area_type']}",
                         value=f"{year}: mesh code {spa['code']}, {int(m.sum())} polígono(s)", action=f"kept as {spa['name']}",
                         note="Área da malha que não é município"))
    for code, info in cfg["special_codes"].items():
        m = g["municipio_code"] == code
        g.loc[m, "area_type"] = info["area_type"]
        g.loc[m, "special_name"] = info["name"]
    if sp.get("litigios"):
        lit = read_source(src, sp["litigios"])
        lit = lit[~lit.geometry.isna()].to_crs(g.crs)
        lit["municipio_code"] = "__litigio_" + lit["name"].map(fold)
        lit["area_type"] = "litigio"
        lit["special_name"] = "Área em " + lit["name"].str.replace("^[Ll]itígio", "litígio", regex=True)
        g = pd.concat([g, lit[["geometry", "code", "name", "municipio_code", "area_type", "special_name"]]], ignore_index=True)
        cons.append(dict(layer="municipios", code=NA, issue="litigio_added", value=f"{year}: {' | '.join(sorted(set(lit['name'])))}",
                         action="disputed areas of the 06 litígio layer added (municipio_code NA)",
                         note="Área em litígio entre UFs, fora da malha municipal"))
    d = dissolve(g, "municipio_code", ["name", "area_type", "special_name"])
    d = cut_enclaves(d, year, cfg, cons, logger).to_crs(cfg["output_crs"])
    if d.geometry.isna().any():
        raise SystemExit(f"{year}: empty geometry after dissolve {d[d.geometry.isna()]['municipio_code'].tolist()}")

    real = d["area_type"] == "municipio"
    codes = set(d.loc[real, "municipio_code"])
    if gen_codes:
        for c in sorted(codes - gen_codes):
            cons.append(dict(layer="municipios", code=c, issue="code_not_in_genealogy", value=str(year), action="kept",
                             note="Polígono com código ausente da genealogia do IBGE neste ano"))
        for c in sorted(gen_codes - codes):
            cons.append(dict(layer="municipios", code=c, issue="genealogy_code_without_polygon", value=str(year),
                             action="none", note="Município da genealogia sem polígono na malha deste ano"))
        logger.info("  %d: mesh vs genealogy: %d mesh-only, %d genealogy-only", year, len(codes - gen_codes), len(gen_codes - codes))
    names = pick_names(year, d.loc[real, "municipio_code"], d.loc[real, "name"], sp["names"], sources, diffs)
    d["municipio_name"] = d["special_name"]
    d.loc[real, "municipio_name"] = names
    d["unidade_code"] = np.where(real | d["area_type"].eq("lagoa"), d["municipio_code"].str[:2], NA)
    d.loc[d["municipio_code"].str.startswith("__"), "municipio_code"] = NA
    d["area_km2"] = d.to_crs(cfg["area_crs"]).area / 1e6
    d = d[["municipio_code", "municipio_name", "unidade_code", "area_type", "area_km2", "geometry"]]
    return d.sort_values(["area_type", "municipio_code"], key=lambda s: s.map(str)).reset_index(drop=True)


def build_sedes(year: int, sp: dict, cfg: dict, muni: gpd.GeoDataFrame, ref_seats, cons: list, logger):
    src = Path(cfg["src_folder"])
    if "seats" in sp:
        s = read_source(src, sp["seats"]).to_crs(cfg["output_crs"])
        s = s.explode(index_parts=False).reset_index(drop=True)
    elif "seats_from_mesh" in sp:
        spec = dict(sp["mesh"], keep=[sp["seats_from_mesh"]["flag"], sp["seats_from_mesh"]["lat"], sp["seats_from_mesh"]["lon"]])
        m = read_source(src, spec)
        f = sp["seats_from_mesh"]
        m = m[m[f["flag"]].astype(bool) & (m[f["lat"]] != 0)]
        s = gpd.GeoDataFrame({"code": m["code"], "name": m["name"]},
                             geometry=[Point(x, y) for x, y in zip(m[f["lon"]], m[f["lat"]])], crs=cfg["output_crs"])
        s = s[~pd.DataFrame({"c": s["code"], "w": s.geometry.to_wkb()}).duplicated()].reset_index(drop=True)
    else:
        return None
    n0 = len(s)
    s = s[~s.geometry.isna() & ~s.geometry.is_empty].copy()
    if len(s) < n0:
        cons.append(dict(layer="sedes", code=NA, issue="empty_geometry_dropped", value=f"{year}: {n0 - len(s)}",
                         action="dropped", note="Registro de sede sem geometria"))
    fixes = sp.get("seats_code_fixes", {})
    fixed = s["code"].map(lambda c: fixes.get(c, c))
    for c in sorted(set(s.loc[fixed != s["code"], "code"])):
        cons.append(dict(layer="sedes", code=fixes[c], issue="seat_code_fixed", value=f"{year}: {c}",
                         action=f"code set to {fixes[c]}", note="Código da sede trocado na fonte"))
    s["code"] = fixed
    mreal = muni[muni["area_type"] == "municipio"].set_index("municipio_code")
    # seats taken from a later year: codes not in this year's mesh are dropped by code, not located
    if sp.get("seats", {}).get("drop_unknown"):
        later = ~s["code"].isin(mreal.index)
        for c, name in zip(s.loc[later, "code"], s.loc[later, "name"]):
            cons.append(dict(layer="sedes", code=c, issue="seat_code_not_in_mesh_dropped", value=f"{year}: {name}",
                             action="dropped", note=sp["seats"]["drop_unknown"]))
        s = s[~later]
    # seats with an unknown code (e.g. 0): assign to the polygon that contains them when it has no seat
    unknown = ~s["code"].isin(mreal.index)
    if unknown.any():
        j = gpd.sjoin(s[unknown], mreal.reset_index()[["municipio_code", "geometry"]], how="left", predicate="within")
        j = j[~j.index.duplicated()]
        have = set(s.loc[~unknown, "code"])
        for idx, r in j.iterrows():
            tgt = r["municipio_code"]
            if isinstance(tgt, str) and tgt not in have:
                cons.append(dict(layer="sedes", code=tgt, issue="seat_code_assigned_by_location", value=f"{year}: {s.at[idx, 'code']}",
                                 action=f"code set to {tgt}", note="Sede com código inválido dentro de município sem sede"))
                s.at[idx, "code"] = tgt
                have.add(tgt)
            else:
                cons.append(dict(layer="sedes", code=s.at[idx, "code"], issue="seat_unknown_code_dropped",
                                 value=f"{year}: {s.at[idx, 'name']}", action="dropped", note="Sede com código ausente da malha"))
        s = s[s["code"].isin(mreal.index)]
    # one point per code: inside the own polygon first, then nearest to the reference seat
    poly = mreal.geometry
    s["inside"] = [poly[c].covers(p) for c, p in zip(s["code"], s.geometry)]
    keep = []
    for c, grp in s.groupby("code", sort=True):
        cand = grp[grp["inside"]] if grp["inside"].any() else grp
        if len(cand) > 1 and ref_seats is not None and c in ref_seats.index:
            dist = cand.to_crs(cfg["area_crs"]).distance(ref_seats.to_crs(cfg["area_crs"]).geometry[c])
            cand = cand.loc[[dist.idxmin()]]
        keep.append(cand.index[0])
        if len(grp) > 1:
            cons.append(dict(layer="sedes", code=c, issue="seat_several_points", value=f"{year}: {len(grp)} pontos",
                             action="kept the point inside the polygon / nearest to the reference seat",
                             note="Mais de um ponto de sede para o mesmo código"))
    s = s.loc[keep]
    for c in s.loc[~s["inside"], "code"]:
        cons.append(dict(layer="sedes", code=c, issue="seat_outside_polygon", value=str(year), action="kept",
                         note="Sede fora do polígono do próprio município"))
    for c in sorted(set(mreal.index) - set(s["code"])):
        cons.append(dict(layer="sedes", code=c, issue="municipio_without_seat", value=str(year), action="none",
                         note="Município sem ponto de sede na fonte"))
    s = s.rename(columns={"code": "municipio_code"})
    s["municipio_name"] = s["municipio_code"].map(mreal["municipio_name"])
    s["capital_type"] = NA
    for kind in ("capital_federal", "capital_estadual"):
        if kind not in sp:
            continue
        cap = read_source(src, sp[kind]).to_crs(cfg["output_crs"])
        for code, name, pt in zip(cap["code"], cap["name"], cap.geometry):
            hit = s.index[s["municipio_code"] == code]
            method = "code"
            if not len(hit):
                d = s.to_crs(cfg["area_crs"]).distance(gpd.GeoSeries([pt], crs=cfg["output_crs"]).to_crs(cfg["area_crs"]).iloc[0])
                hit = [d.idxmin()] if d.min() <= cfg.get("capital_max_distance_m", 5000) else []
                method = "nearest seat"
                cons.append(dict(layer="sedes", code=code, issue=f"{kind}_code_not_seat", value=f"{year}: {name}",
                                 action=f"matched to seat {s.loc[hit[0], 'municipio_code']} by distance" if len(hit) else "not matched",
                                 note="Código da capital não corresponde a uma sede"))
            for h in hit:
                s.at[h, "capital_type"] = kind.split("_")[1]
            if not len(hit):
                logger.warning("  %d: %s %s %s not matched", year, kind, code, name)
            elif method != "code":
                logger.info("  %d: %s %s matched by distance", year, kind, code)
    logger.info("  %d: sedes %d (of %d municipalities), %d outside own polygon, capitals %s", year, len(s), len(mreal),
                int((~s["inside"]).sum()), s["capital_type"].value_counts().to_dict())
    return s[["municipio_code", "municipio_name", "capital_type", "geometry"]].sort_values("municipio_code").reset_index(drop=True)


def fill_gaps(u: gpd.GeoDataFrame, year: int, cfg: dict, cons: list, logger) -> gpd.GeoDataFrame:
    """Holes of the dissolved units: slivers below fill_holes_km2 (precision gaps between municipal polygons)
    are filled; larger holes not covered by another unit (e.g. DF inside GO is covered) are mesh gaps: kept
    and reported."""
    from shapely.geometry import Polygon
    limit = cfg.get("fill_holes_km2", 1.0)
    acrs = cfg["area_crs"]
    others = u.to_crs(acrs)
    geoms, n_fill, a_fill = [], 0, 0.0
    for code, g in zip(u["unidade_code"], u.geometry):
        polys = []
        for p in g.geoms:
            keep = []
            for r in p.interiors:
                h = gpd.GeoSeries([Polygon(r)], crs=u.crs).to_crs(acrs).iloc[0]
                a = h.area / 1e6
                if a < limit:
                    n_fill += 1
                    a_fill += a
                    continue
                keep.append(r)
                cov = others[others["unidade_code"] != code].intersection(h).area.sum() / h.area
                if cov < 0.99:
                    pt = Polygon(r).representative_point()
                    cons.append(dict(layer="unidades", code=code, issue="mesh_gap",
                                     value=f"{year}: {a:.1f} km² at {pt.y:.3f}, {pt.x:.3f}", action="kept as hole",
                                     note="Área sem município na malha de origem (vazio entre polígonos municipais)"))
                    logger.warning("  %d: mesh gap %.1f km2 in unidade %s at (%.3f, %.3f)", year, a, code, pt.y, pt.x)
            polys.append(Polygon(p.exterior, keep))
        geoms.append(polygonal(unary_union(polys)))
    if n_fill:
        cons.append(dict(layer="unidades", code=NA, issue="slivers_filled", value=f"{year}: {n_fill} holes, {a_fill:.2f} km²",
                         action=f"holes < {limit} km² filled", note="Vazios de precisão entre polígonos municipais na dissolução"))
        logger.info("  %d: %d sliver holes filled (%.2f km2)", year, n_fill, a_fill)
    return gpd.GeoDataFrame(u.drop(columns="geometry"), geometry=geoms, crs=u.crs)


def build_unidades(year: int, sp: dict, cfg: dict, muni: gpd.GeoDataFrame, cons: list, logger):
    spec = sp.get("unidades")
    acrs = cfg["area_crs"]
    if spec == "dissolve" or spec is None:
        m = muni[muni["unidade_code"] != NA]
        u = dissolve(m, "unidade_code", [])
        u = fill_gaps(u, year, cfg, cons, logger)
        names = cfg.get("unidade_names", {})
        u["unidade_name"] = u["unidade_code"].map(lambda c: names.get(c, NA))
        cons.append(dict(layer="unidades", code=NA, issue="unidades_dissolved", value=str(year),
                         action="dissolved from the municipalities", note="Sem malha estadual na fonte deste ano"))
    else:
        u = read_source(Path(cfg["src_folder"]), spec).to_crs(cfg["output_crs"])
        if spec.get("code"):
            u["unidade_code"] = u["code"]
        else:
            # code = UF prefix of the municipalities with the largest overlap
            m = muni[muni["area_type"].isin(["municipio", "lagoa"])][["unidade_code", "geometry"]].to_crs(acrs)
            uu = u.to_crs(acrs).reset_index().rename(columns={"index": "uid"})
            o = gpd.overlay(uu[["uid", "geometry"]], m, how="intersection", keep_geom_type=True)
            o["a"] = o.area
            best = o.groupby(["uid", "unidade_code"])["a"].sum().reset_index().sort_values("a").groupby("uid").tail(1)
            share = best.set_index("uid")["a"] / uu.set_index("uid").area
            u["unidade_code"] = u.index.map(best.set_index("uid")["unidade_code"]).fillna(NA)
            for uid, sh in share.items():
                if sh < 0.9:
                    cons.append(dict(layer="unidades", code=u.at[uid, "unidade_code"], issue="unidade_code_weak_overlap",
                                     value=f"{year}: {u.at[uid, 'name']} {sh:.1%}", action="kept", note="Código atribuído pela maior sobreposição"))
            nomatch = u["unidade_code"] == NA
            for nm in u.loc[nomatch, "name"]:
                cons.append(dict(layer="unidades", code=NA, issue="unidade_without_municipalities", value=f"{year}: {nm}",
                                 action="dropped", note="Polígono estadual sem municípios"))
            u = u[~nomatch]
        u["unidade_name"] = u["name"]
        names = u.groupby("unidade_code")["unidade_name"].agg(lambda s: " | ".join(sorted(set(s))))
        u = dissolve(u, "unidade_code", [])
        u["unidade_name"] = u["unidade_code"].map(names)
        dup = names[names.str.contains(r" \| ")]
        for c, nm in dup.items():
            cons.append(dict(layer="unidades", code=c, issue="unidade_several_names", value=f"{year}: {nm}",
                             action="names joined", note="Mais de um polígono estadual com o mesmo código"))
    u = u.to_crs(cfg["output_crs"])
    u["area_km2"] = u.to_crs(acrs).area / 1e6
    logger.info("  %d: unidades %d", year, len(u))
    return u[["unidade_code", "unidade_name", "area_km2", "geometry"]].sort_values("unidade_code").reset_index(drop=True)


def build_extra(spec: dict, cfg: dict, cons: list):
    g = read_source(Path(cfg["src_folder"]), {"zip": spec["zip"], "encoding": spec.get("encoding")})
    raw = pyogrio.read_dataframe(shp_in_zip(Path(cfg["src_folder"]) / spec["zip"]), read_geometry=False, encoding=spec.get("encoding"))
    out = gpd.GeoDataFrame(geometry=[polygonal(x) for x in g.geometry], crs=g.crs).to_crs(cfg["output_crs"])
    for new, old in spec["columns"].items():
        out[new] = raw[old].map(clean_name).values
    for new, (old, mapping) in spec.get("mapped", {}).items():
        out[new] = raw[old].map(lambda v: mapping.get(v, NA)).values
    out["area_km2"] = out.to_crs(cfg["area_crs"]).area / 1e6
    cols = list(spec["columns"]) + list(spec.get("mapped", {})) + ["area_km2", "geometry"]
    return out[cols].sort_values(cols[0]).reset_index(drop=True)


def write_layer(gdf, cfg: dict, name: str, year: int, overwrite: bool, logger):
    out = Path(cfg["output_folder"]) / cfg["name_pattern"].format(NAME=name.upper(), YEAR=year)
    if out.exists() and not overwrite:
        raise SystemExit(f"{out} exists (use --overwrite)")
    tmp = out.with_name(out.stem + ".part.gpkg")
    if tmp.exists():
        tmp.unlink()
    gdf.to_file(tmp, layer=cfg["layer_pattern"].format(name=name, year=year), driver="GPKG", engine="pyogrio",
                promote_to_multi=gdf.geom_type.str.contains("Polygon").any())
    tmp.replace(out)
    logger.info("  wrote %s (%d features)", out.name, len(gdf))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    parser.add_argument("--years", nargs="*", type=int, help="Only these snapshot years (CONSISTENCY then goes to the report folder)")
    args = parser.parse_args()
    cfg = load_config(args.config, REQUIRED_KEYS)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report, "geography")
    Path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
    sources = name_sources(cfg)
    snaps = {int(y): sp for y, sp in cfg["snapshots"].items()}
    years = sorted(snaps) if not args.years else sorted(args.years)
    cons, diffs, summary = [], [], []
    built = {}
    # reference seats for duplicated seat points: the next snapshot that has seats (built on demand)
    for year in sorted(years, reverse=True):
        sp = snaps[year]
        logger.info("Snapshot %d", year)
        gen_codes = set(sources["genealogy"].get(year, {})) if year in sources["genealogy"] else set()
        muni = build_municipios(year, sp, cfg, sources, gen_codes, cons, diffs, logger)
        ref = built.get(sp.get("reference_seats_year"))
        sedes = build_sedes(year, sp, cfg, muni, ref.set_index("municipio_code") if ref is not None else None, cons, logger)
        if sedes is not None:
            built[year] = sedes
        unidades = build_unidades(year, sp, cfg, muni, cons, logger)
        write_layer(muni, cfg, "municipios", year, args.overwrite, logger)
        if sedes is not None:
            write_layer(sedes, cfg, "sedes", year, args.overwrite, logger)
        write_layer(unidades, cfg, "unidades", year, args.overwrite, logger)
        for name, spec in sp.get("extra_layers", {}).items():
            write_layer(build_extra(spec, cfg, cons), cfg, name, year, args.overwrite, logger)
        summary.append(dict(year=year, municipios=int((muni["area_type"] == "municipio").sum()),
                            special_areas=int((muni["area_type"] != "municipio").sum()),
                            sedes=len(sedes) if sedes is not None else 0, unidades=len(unidades),
                            area_km2=round(muni["area_km2"].sum(), 1)))
    pd.DataFrame(summary).sort_values("year").to_csv(report / "geography_summary.csv", sep=";", index=False)
    pd.DataFrame(diffs).to_csv(report / "names_differences.csv", sep=";", index=False)
    logger.info("Name differences between sources: %d (report names_differences.csv)", len(diffs))
    layers = {"municipios", "sedes", "unidades"} | {n for sp in snaps.values() for n in sp.get("extra_layers", {})}
    if args.years:
        target = report / cfg["consistency_name"]
        logger.warning("Partial run: CONSISTENCY written to %s", target)
    else:
        target = Path(cfg["output_folder"]) / cfg["consistency_name"]
    kept = merge_consistency(target, cons, layers)
    logger.info("CONSISTENCY: %d rows for the geography layers (%d rows of other layers kept)", len(cons), kept)


if __name__ == "__main__":
    main()

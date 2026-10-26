"""
Assemble the IBGE Census 1991 geography for T1: `municipios`, `unidades` and `sedes` GeoPackages.

Sources:
  * mesh_zip         IBGE historical municipal mesh of 1991 (shapefile, SIRGAS 2000 Polyconic), read
                     straight from the zip; reprojected to output_crs, made valid and dissolved to one
                     (multi)polygon per code
  * dtb_xls          DTB Municipios 1991: municipio_code (7 digits), municipality names (NOME_EM_MI,
                     always used instead of the mesh / microdata names), mesoregion and microregion codes
  * microdata        the CD91AMOUP<UF>.DBF files, only for the metropolitan region of each municipality
                     (METROP) and the uppercase meso / micro names used to check the reference names
  * reference_names  a table with unidade/regiaomeso/regiaomicro code + name (the 2000 T1 basico; the
                     meso and micro division of 1989 is the same in 1991 and 2000): a reference name is
                     used when it equals the 1991 uppercase name (accents, hyphens and spaces ignored);
                     otherwise a name_overrides entry is required (the run stops without one)
  * labels           metropolitan region names (dictionary categories of METROP)
  * seats_zip        IBGE municipal seats of 1991 (shapefile of single-point multipoints, same codes as
                     the mesh); records without geometry are dropped, seats_code_fixes swaps wrong codes
                     (Itapipoca / Amontada), and a municipality with several points keeps the one
                     nearest to its seat in reference_seats (the 2000 T1 sedes)

Mesh fixes (all recorded in CONSISTENCY):
  * code_fixes       wrong codes of the mesh (e.g. Itapipoca 2306045 -> 2306405)
  * special_areas    mesh polygons that are not municipalities: the Patos and Mirim lagoons (coded
                     4300002 / 4300001 as in 2000-2022), the PI/CE disputed area and the islands not
                     assigned to any municipality (municipio_code NA); area_type tells them apart
  * mesh names that differ from the DTB are listed (the DTB name is used)

Outputs (output_folder):
  municipios  municipio_code, municipio_name, area_type, unidade_code/name, regiaomeso_code/name,
              regiaomicro_code/name, regiaometropolitana_code/name, area_km2 (computed in area_crs)
  unidades    unidade_code, unidade_name, area_km2 — dissolved from the municipalities and lagoons
              (the PI/CE disputed area and the unassigned islands belong to no unit)
  sedes       municipio_code, municipio_name (DTB), one point per DTB municipality; every seat must
              fall inside its own municipality polygon (the run stops otherwise). Report
              seats_vs_reference.csv: distance of each seat to its reference seat (2000)
  CONSISTENCY rows of layers municipios / unidades / sedes are replaced; rows of other layers are kept
              (partial runs write the file to the report folder)

Usage:
    python assemble_geography.py --config assemble_geography.local.json [--overwrite]
"""

import argparse
import csv
import glob
import json
import logging
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import make_valid
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assemble_microdata import DBF  # noqa: E402

REQUIRED_KEYS = ["mesh_zip", "mesh_member", "dtb_xls", "microdata_folder", "reference_names_csv",
                 "labels", "dictionary_xls", "seats_zip", "seats_member", "reference_seats",
                 "output_folder", "report_folder"]
NA = "NA"
CONSISTENCY_FIELDS = ["layer", "code", "issue", "value", "action", "note"]
LAYERS = ("municipios", "unidades", "sedes")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("mesh_code_field", "BR91POLY_I")
    cfg.setdefault("mesh_name_field", "NOMEMUNICP")
    cfg.setdefault("mesh_encoding", "cp1252")
    cfg.setdefault("microdata_glob", "*/CD91AMOUP*.DBF")
    cfg.setdefault("name_pattern", "IBGE_CENSO-1991_{NAME}_BRASIL_1991.gpkg")
    cfg.setdefault("layer_pattern", "ibge_censo_1991_{name}")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    cfg.setdefault("output_crs", "EPSG:4674")
    cfg.setdefault("area_crs", "ESRI:102033")
    cfg.setdefault("code_fixes", {})
    cfg.setdefault("special_areas", [])
    cfg.setdefault("name_overrides", {})
    cfg.setdefault("seats_code_fixes", {})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"geography_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_geography")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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


def fold(text) -> str:
    """Upper case, no accents, hyphens and spaces collapsed — for name comparison only."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[\s\-]+", " ", text).strip()


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def read_dtb(path: str) -> pd.DataFrame:
    d = pd.read_excel(path, dtype=str, engine="calamine")
    d.columns = [c.strip() for c in d.columns]
    d = d.rename(columns={"COD MUN": "municipio_code", "NOME_EM_MI": "municipio_name", "UF": "unidade_code"})
    d["regiaomeso_code"] = d["unidade_code"] + d["MESORREG"].str.zfill(2)
    d["regiaomicro_code"] = d["unidade_code"] + d["MICRORREG"].str.zfill(3)
    d["municipio_name"] = d["municipio_name"].str.strip()
    if d["municipio_code"].duplicated().any():
        raise SystemExit("DTB: duplicated municipio_code")
    return d


def read_microdata(cfg: dict, logger) -> tuple:
    """(metropolitan region per 7-digit-prefix municipality, uppercase names per meso/micro code)."""
    metro, names = {}, {"unidade": {}, "regiaomeso": {}, "regiaomicro": {}}
    files = sorted(glob.glob(str(Path(cfg["microdata_folder"]) / cfg["microdata_glob"])))
    for f in files:
        d = DBF(Path(f))
        uf, mun, met = (d.raw(c).astype(str) for c in ("UFNUM", "MUNICNUM", "METROP"))
        ufn, meso, meson, micro, micron = (d.raw(c).astype(str) for c in
                                           ("UFNOM", "MESONUM", "MESONOM", "MICRONUM", "MICRONOM"))
        for key in set(zip(uf, mun, met)):
            code6 = key[0] + key[1].zfill(4)
            if metro.setdefault(code6, key[2]) != key[2]:
                raise SystemExit(f"METROP varies within municipality {code6}")
        for a, b in set(zip(uf, ufn)):
            names["unidade"][a] = b
        for a, m, b in set(zip(uf, meso, meson)):
            names["regiaomeso"][a + m.zfill(2)] = b
        for a, m, b in set(zip(uf, micro, micron)):
            names["regiaomicro"][a + m.zfill(3)] = b
    logger.info("Microdata: %d files, %d municipalities, %d meso, %d micro", len(files), len(metro),
                len(names["regiaomeso"]), len(names["regiaomicro"]))
    return metro, names


def read_metro_labels(cfg: dict) -> dict:
    with open(cfg["labels"], encoding="utf-8") as f:
        labels = json.load(f)
    d = pd.read_excel(cfg["dictionary_xls"], header=None, dtype=str, engine="calamine").fillna("")
    out, cur = {}, None
    for _, r in d.iloc[2:].iterrows():
        if r[1].strip():
            cur = r[1].strip()
        elif cur == "METROP" and r[2].strip() and r[2].strip() != "Categorías":
            out[str(int(r[2]))] = labels[r[3].strip()][0]
    return out


def resolve_names(level: str, codes: set, upper: dict, ref: dict, overrides: dict, cons: list) -> dict:
    """Name per code: the reference name when it matches the 1991 uppercase name, else an override."""
    out, missing = {}, []
    for c in sorted(codes):
        if c in overrides:
            out[c] = overrides[c]
            cons.append(dict(layer="municipios", code=c, issue=f"{level}_name_override",
                             value=f"1991: {upper.get(c, '')} | reference: {ref.get(c, '')}",
                             action=f"name set to {overrides[c]}",
                             note="Nome de 1991 (DBF, maiúsculo sem acento) difere da referência de 2000"))
        elif c in ref and fold(ref[c]) == fold(upper.get(c, ref[c])):
            out[c] = ref[c]
        else:
            missing.append((c, upper.get(c), ref.get(c)))
    if missing:
        raise SystemExit(f"{level}: names need a name_overrides entry: {missing[:10]}")
    return out


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------

def read_mesh(cfg: dict, logger) -> gpd.GeoDataFrame:
    path = f"/vsizip/{cfg['mesh_zip']}/{cfg['mesh_member']}"
    g = gpd.read_file(path, encoding=cfg["mesh_encoding"])
    g["mesh_code"] = g[cfg["mesh_code_field"]].astype("int64").astype(str)
    g["mesh_name"] = g[cfg["mesh_name_field"]].where(g[cfg["mesh_name_field"]].notna(), None)
    logger.info("Mesh: %d polygons, CRS %s", len(g), g.crs.name)
    g = g.to_crs(cfg["output_crs"])
    g["geometry"] = g.geometry.map(make_valid)
    return g[["mesh_code", "mesh_name", "geometry"]]


def polygonal(geom):
    """Keep only the polygonal part (make_valid may return collections with lines/points)."""
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [p for p in getattr(geom, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(parts)


def dissolve(g: gpd.GeoDataFrame, key: str) -> gpd.GeoDataFrame:
    rows = []
    for k, grp in g.groupby(key, sort=True):
        geom = polygonal(make_valid(unary_union(list(grp.geometry))))
        rows.append({key: k, "n_parts_source": len(grp), "geometry": geom})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=g.crs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_sedes(cfg: dict, municipios: gpd.GeoDataFrame, cons: list, logger) -> gpd.GeoDataFrame:
    path = f"/vsizip/{cfg['seats_zip']}/{cfg['seats_member']}"
    s = gpd.read_file(path, encoding=cfg["mesh_encoding"])
    logger.info("Seats: %d records, CRS %s", len(s), s.crs.name)
    s["municipio_code"] = s[cfg["mesh_code_field"]].astype("int64").astype(str)
    empty = s.geometry.isna() | s.geometry.is_empty
    for code, name in s.loc[empty, ["municipio_code", cfg["mesh_name_field"]]].itertuples(index=False):
        cons.append(dict(layer="sedes", code=code, issue="seat_without_geometry", value=NA if pd.isna(name) else name,
                         action="dropped", note="Registro da camada de sedes sem geometria"))
    s = s[~empty].to_crs(cfg["output_crs"])
    if (s.geometry.map(lambda g: len(getattr(g, "geoms", [g]))) != 1).any():
        raise SystemExit("sedes: multipoint with more than one point")
    s["geometry"] = s.geometry.map(lambda g: g.geoms[0] if hasattr(g, "geoms") else g)
    fixes = cfg["seats_code_fixes"]
    hit = s["municipio_code"].isin(list(fixes))
    for bad, good in fixes.items():
        if not (s["municipio_code"] == bad).any():
            raise SystemExit(f"seats_code_fixes: {bad} not in the seats")
        cons.append(dict(layer="sedes", code=good, issue="seat_code_fixed", value=bad, action=f"code set to {good}",
                         note=f"Ponto da sede cai no polígono do município {good}, códigos trocados na fonte"))
    s.loc[hit, "municipio_code"] = s.loc[hit, "municipio_code"].map(fixes)

    ref = gpd.read_file(cfg["reference_seats"]).to_crs(cfg["area_crs"]).set_index("municipio_code").geometry
    s["ref_km"] = [p.distance(ref[c]) / 1000 if c in ref.index else float("nan")
                   for c, p in zip(s["municipio_code"], s.geometry.to_crs(cfg["area_crs"]))]
    s = s.sort_values(["municipio_code", "ref_km"])
    dup = s["municipio_code"].duplicated(keep=False)
    for code, grp in s[dup].groupby("municipio_code"):
        if grp["ref_km"].isna().all():
            raise SystemExit(f"sedes: {code} has {len(grp)} points and no reference seat")
        for km, p in grp[["ref_km", "geometry"]].iloc[1:].itertuples(index=False):
            cons.append(dict(layer="sedes", code=code, issue="seat_extra_point", value=f"{p.x:.5f} {p.y:.5f}",
                             action="dropped", note=f"Ponto a {km:.1f} km da sede de referência, mantido o ponto a "
                             f"{grp['ref_km'].iloc[0]:.2f} km"))
    s = s.drop_duplicates("municipio_code")

    muni = municipios[municipios["area_type"] == "municipio"].set_index("municipio_code")
    unknown = sorted(set(s["municipio_code"]) - set(muni.index))
    missing = sorted(set(muni.index) - set(s["municipio_code"]))
    if unknown or missing:
        raise SystemExit(f"sedes vs DTB: codes not in DTB {unknown[:10]}, municipalities without seat {missing[:10]}")
    outside = [c for c, p in zip(s["municipio_code"], s.geometry) if not muni.geometry[c].intersects(p)]
    if outside:
        raise SystemExit(f"sedes: seats outside their own municipality polygon: {outside[:10]}")
    s["municipio_name"] = s["municipio_code"].map(muni["municipio_name"])
    report = Path(cfg["report_folder"]) / "seats_vs_reference.csv"
    s[["municipio_code", "municipio_name", "ref_km"]].round(3).sort_values("ref_km", ascending=False) \
        .to_csv(report, sep=";", index=False)
    logger.info("sedes: %d seats, all inside their polygon; distance to the reference seat: median %.2f km, "
                "%d over 2 km (see %s)", len(s), s["ref_km"].median(), int((s["ref_km"] > 2).sum()), report.name)
    return gpd.GeoDataFrame(s[["municipio_code", "municipio_name", "geometry"]].sort_values("municipio_code")
                            .reset_index(drop=True), geometry="geometry", crs=cfg["output_crs"])


def merge_consistency(path: Path, rows: list, layers: tuple):
    kept = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter=";") if r["layer"] not in layers]
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONSISTENCY_FIELDS, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(kept + rows)
    tmp.replace(path)
    return len(kept)


def write_layer(gdf: gpd.GeoDataFrame, cfg: dict, name: str, overwrite: bool, logger):
    out = Path(cfg["output_folder"]) / cfg["name_pattern"].format(NAME=name.upper())
    if out.exists() and not overwrite:
        raise SystemExit(f"{out} exists (use --overwrite)")
    tmp = out.with_name(out.stem + ".part.gpkg")
    if tmp.exists():
        tmp.unlink()
    gdf.to_file(tmp, layer=cfg["layer_pattern"].format(name=name), driver="GPKG")
    tmp.replace(out)
    logger.info("Wrote %s (%d features)", out, len(gdf))


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    Path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
    cons = []

    dtb = read_dtb(cfg["dtb_xls"])
    metro, upper = read_microdata(cfg, logger)
    metro_names = read_metro_labels(cfg)
    ref = pd.read_csv(cfg["reference_names_csv"], sep=";", dtype=str,
                      usecols=["unidade_code", "unidade_name", "regiaomeso_code", "regiaomeso_name",
                               "regiaomicro_code", "regiaomicro_name"]).drop_duplicates()
    names = {}
    for level in ("unidade", "regiaomeso", "regiaomicro"):
        r = dict(ref[[f"{level}_code", f"{level}_name"]].drop_duplicates().itertuples(index=False))
        names[level] = resolve_names(level, set(dtb[f"{level}_code"]), upper[level], r,
                                     cfg["name_overrides"].get(level, {}), cons)
        logger.info("  %-12s %4d names (%d overrides)", level, len(names[level]),
                    len(cfg["name_overrides"].get(level, {})))

    # --- mesh -> codes --------------------------------------------------------------
    g = read_mesh(cfg, logger)
    for bad, good in cfg["code_fixes"].items():
        n = int((g["mesh_code"] == bad).sum())
        if not n:
            raise SystemExit(f"code_fixes: {bad} not in the mesh")
        g.loc[g["mesh_code"] == bad, "mesh_code"] = good
        cons.append(dict(layer="municipios", code=good, issue="mesh_code_fixed", value=bad,
                         action=f"code set to {good}", note=f"Código da malha de 1991 difere do DTB ({n} polígono(s))"))
    g["municipio_code"] = g["mesh_code"]
    g["area_type"] = "municipio"
    g["special_name"] = None
    g["unidade_override"] = None
    for sp in cfg["special_areas"]:
        m = g["mesh_code"] == sp["mesh_code"]
        if "mesh_name" in sp:
            m &= g["mesh_name"].isna() if sp["mesh_name"] is None else (g["mesh_name"] == sp["mesh_name"])
        n = int(m.sum())
        if not n:
            raise SystemExit(f"special area not found in the mesh: {sp}")
        key = sp["municipio_code"] if sp["municipio_code"] != NA else f"__{sp['area_type']}"
        g.loc[m, "municipio_code"] = key
        g.loc[m, "area_type"] = sp["area_type"]
        g.loc[m, "special_name"] = sp["name"]
        g.loc[m, "unidade_override"] = sp["unidade_code"]
        cons.append(dict(layer="municipios", code=sp["municipio_code"], issue=f"special_area_{sp['area_type']}",
                         value=f"mesh code {sp['mesh_code']}, {n} polígono(s)", action=f"kept as {sp['name']}",
                         note="Área da malha que não é município do DTB"))
    muni = g[g["area_type"] == "municipio"]
    unknown = sorted(set(muni["municipio_code"]) - set(dtb["municipio_code"]))
    missing = sorted(set(dtb["municipio_code"]) - set(muni["municipio_code"]))
    if unknown or missing:
        raise SystemExit(f"Mesh vs DTB: codes not in DTB {unknown[:10]}, DTB codes without polygon {missing[:10]}")

    # names of the mesh that differ from the DTB (the DTB name is used)
    dn = dict(zip(dtb["municipio_code"], dtb["municipio_name"]))
    for code, mesh_name in muni[["municipio_code", "mesh_name"]].drop_duplicates().itertuples(index=False):
        if fold(mesh_name) != fold(dn[code]):
            cons.append(dict(layer="municipios", code=code, issue="mesh_name_differs", value=mesh_name,
                             action=f"DTB name used: {dn[code]}", note="Nome da malha de 1991 difere do DTB 1991"))

    d = dissolve(g, "municipio_code")
    attrs = g.drop_duplicates("municipio_code").set_index("municipio_code")[
        ["area_type", "special_name", "unidade_override"]]
    d = d.join(attrs, on="municipio_code")
    for code, n in d.loc[d["n_parts_source"] > 1, ["municipio_code", "n_parts_source"]].itertuples(index=False):
        if d.loc[d["municipio_code"] == code, "area_type"].iloc[0] == "municipio":
            cons.append(dict(layer="municipios", code=code, issue="multipart_dissolved", value=str(n),
                             action="parts dissolved into one multipolygon", note="Município em várias partes (ilhas)"))

    # --- attributes ------------------------------------------------------------------------
    dt = dtb.set_index("municipio_code")
    is_m = d["area_type"] == "municipio"
    d["municipio_name"] = d["municipio_code"].map(dt["municipio_name"]).where(is_m, d["special_name"])
    d["unidade_code"] = d["municipio_code"].map(dt["unidade_code"]).where(is_m, d["unidade_override"])
    d["regiaomeso_code"] = d["municipio_code"].map(dt["regiaomeso_code"]).where(is_m, NA)
    d["regiaomicro_code"] = d["municipio_code"].map(dt["regiaomicro_code"]).where(is_m, NA)
    d["unidade_name"] = d["unidade_code"].map(names["unidade"]).fillna(NA)
    d["regiaomeso_name"] = d["regiaomeso_code"].map(names["regiaomeso"]).fillna(NA)
    d["regiaomicro_name"] = d["regiaomicro_code"].map(names["regiaomicro"]).fillna(NA)
    met = d["municipio_code"].str[:6].map(metro).where(is_m)
    d["regiaometropolitana_code"] = met.where(met.notna() & (met != "0"), NA)
    d["regiaometropolitana_name"] = d["regiaometropolitana_code"].map(metro_names).fillna(NA)
    d["municipio_code"] = d["municipio_code"].where(~d["municipio_code"].str.startswith("__"), NA)
    d["area_km2"] = (d.geometry.to_crs(cfg["area_crs"]).area / 1e6).round(3)
    cols = ["municipio_code", "municipio_name", "area_type", "unidade_code", "unidade_name",
            "regiaomeso_code", "regiaomeso_name", "regiaomicro_code", "regiaomicro_name",
            "regiaometropolitana_code", "regiaometropolitana_name", "area_km2", "geometry"]
    order = d["area_type"].map({"municipio": 0, "lagoa": 1, "litigio": 2, "ilha_nao_atribuida": 3})
    municipios = d.assign(_o=order).sort_values(["_o", "municipio_code"])[cols].reset_index(drop=True)
    if municipios[cols[:-1]].isna().any().any():
        raise SystemExit("municipios: empty attribute values")
    if not municipios.is_valid.all():
        raise SystemExit("municipios: invalid geometries after dissolve")
    logger.info("municipios: %s", municipios["area_type"].value_counts().to_dict())

    u = dissolve(municipios[municipios["area_type"].isin(["municipio", "lagoa"])], "unidade_code")
    u["unidade_name"] = u["unidade_code"].map(names["unidade"])
    u["area_km2"] = (u.geometry.to_crs(cfg["area_crs"]).area / 1e6).round(3)
    unidades = u[["unidade_code", "unidade_name", "area_km2", "geometry"]]

    sedes = build_sedes(cfg, municipios, cons, logger)

    write_layer(municipios, cfg, "municipios", args.overwrite, logger)
    write_layer(unidades, cfg, "unidades", args.overwrite, logger)
    write_layer(sedes, cfg, "sedes", args.overwrite, logger)
    logger.info("Area: municipalities %.0f km2, lagoons %.0f, disputed %.0f, unassigned islands %.1f; units %.0f",
                *(municipios.loc[municipios["area_type"] == t, "area_km2"].sum()
                  for t in ("municipio", "lagoa", "litigio", "ilha_nao_atribuida")), unidades["area_km2"].sum())

    cpath = Path(cfg["output_folder"]) / cfg["consistency_name"]
    kept = merge_consistency(cpath, cons, LAYERS)
    logger.info("Consistency: %d geography rows (+%d kept from other layers) -> %s", len(cons), kept, cpath)


if __name__ == "__main__":
    main()

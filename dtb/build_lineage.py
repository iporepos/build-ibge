"""
Build the municipal lineage ("splitting") graph of the DTB snapshots for T1.

Nodes are the municipalities of each snapshot (T1 municipios GeoPackages, area_type municipio); edges link a
municipality of one snapshot to the municipalities of the next snapshot that share territory with it. Every
pair of consecutive snapshots is overlaid in area_crs; each intersection gives area_km2, share_from (part
of the old municipality) and share_to (part of the new one).

Relation of an edge (codes are the key: IBGE never reuses the code of an extinct municipality):
  continuation  same code in both snapshots
  split         new code that received territory of a municipality that still exists (desmembramento)
  recode        old code extinct and new code created on the same territory (share_from and share_to >=
                recode_share), e.g. Mato Grosso do Sul 1977, Tocantins 1988, extinct-and-recreated municipalities
  redistribution new code that received territory of a municipality that became extinct (not a recode)
  absorption    extinct code whose territory went to a municipality that still exists (anexação)
  transfer      territory exchanged between two municipalities that exist in both snapshots
  emergence     new code on territory that was no municipality in the previous snapshot: a special area
                (name_from = disputed area / lagoon / islands) or no polygon (name_from "Fora da malha de
                <year>", e.g. Acre before 1903); code_from is NA
Edges of a new code are kept when share_to >= parent_share (or it is the largest contribution); edges of an
extinct code when share_from >= parent_share (or largest). Transfers need share_from or share_to >=
transfer_share and area_km2 >= transfer_min_km2, both set separately for snapshot pairs on the same
cartographic base (1872-1991, all built from the 1991 mesh) and on different bases (1991-2000-2010-2022,
where boundary differences of the scales are mostly cartographic, not legal). Smaller overlaps are noise.

Checks written on every edge:
  primary       1 for the largest contribution to a new code / largest destination of an extinct code
  point_rule    1 when the representative point of the new (split/recode/redistribution) or extinct
                (absorption) polygon falls in the other end of the edge (the "centroid" spatial join)
  genealogy     the IBGE genealogy (1872-2010) for the same event: agrees (the note names this municipality),
                agrees_indirect (the note names a municipality created in the same interval from this one),
                disagrees, not_documented (no genealogy row / note), NA for continuation and transfer
  same_trajectory 1 when the genealogy puts code_from (year_from) and code_to (year_to) on the same trajectory
                (rows of the same final code, e.g. the Goiás-era and Tocantins-era rows of a TO municipality)
  created_edition first quadro-municipios edition after year_from that lists code_to (new codes only;
                gives the year of creation inside long intervals, e.g. 1993 / 1997 / 2001 / 2005 / 2013)
  same_base     1 when both snapshots share the cartographic base (config snapshots)

Outputs: T1 linhagem CSV (edges); reports lineage_summary.csv (edges per interval and relation) and
lineage_checks.csv (point rule and genealogy agreement per interval), lineage_disagreements.csv.

Usage:
    python build_lineage.py --config build_lineage.local.json [--overwrite]
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dtbtools import NA, load_config, setup_logging, write_csv  # noqa: E402

REQUIRED_KEYS = ["input_folder", "mesh_pattern", "output_folder", "report_folder", "output_name", "area_crs",
                 "snapshots", "genealogy_csv", "quadro_csv", "noise_share", "parent_share", "recode_share",
                 "transfer"]
EDGE_COLS = ["year_from", "year_to", "code_from", "name_from", "code_to", "name_to", "relation", "area_km2",
             "share_from", "share_to", "primary", "point_rule", "genealogy", "same_trajectory", "created_edition",
             "same_base"]


def load_snapshot(cfg: dict, year: int):
    """Municipalities (area_type municipio) and the special areas (disputed areas, lagoons, islands)."""
    path = Path(cfg["input_folder"]) / cfg["mesh_pattern"].format(YEAR=year)
    g = gpd.read_file(path).to_crs(cfg["area_crs"])
    special = g[g["area_type"] != "municipio"][["municipio_name", "geometry"]].reset_index(drop=True)
    g = g[g["area_type"] == "municipio"][["municipio_code", "municipio_name", "geometry"]]
    g["area"] = g.area / 1e6
    g["pt"] = g.geometry.representative_point()
    return g.reset_index(drop=True), special


def emergence(o_all: pd.DataFrame, a, special_a, b, year_from: int, cfg: dict) -> pd.DataFrame:
    """Edges of new codes whose territory was not in any municipality of the previous snapshot: a special
    area of it (e.g. a disputed area) or no polygon at all (e.g. Acre in 1900). code_from is NA."""
    covered = o_all.groupby("code_to")["share_to"].sum()
    new = b[~b["municipio_code"].isin(set(a["municipio_code"]))]
    new = new[new["municipio_code"].map(covered).fillna(0) < 1 - cfg["parent_share"]]
    rows = []
    for _, r in new.iterrows():
        near = a.iloc[a.sindex.query(r.geometry, predicate="intersects")]
        rest = r.geometry.difference(near.union_all()) if len(near) else r.geometry
        for _, sp in special_a.iloc[special_a.sindex.query(rest, predicate="intersects")].iterrows():
            part = rest.intersection(sp.geometry)
            rest = rest.difference(sp.geometry)
            if part.area / 1e6 / r["area"] >= cfg["parent_share"]:
                rows.append(dict(code_from=NA, name_from=sp["municipio_name"], code_to=r["municipio_code"],
                                 area_km2=part.area / 1e6, share_from=part.area / sp.geometry.area, share_to=part.area / 1e6 / r["area"]))
        if rest.area / 1e6 / r["area"] >= cfg["parent_share"]:
            rows.append(dict(code_from=NA, name_from=f"Fora da malha de {year_from}", code_to=r["municipio_code"],
                             area_km2=rest.area / 1e6, share_from=np.nan, share_to=rest.area / 1e6 / r["area"]))
    e = pd.DataFrame(rows, columns=["code_from", "name_from", "code_to", "area_km2", "share_from", "share_to"])
    e["relation"] = "emergence"
    e["new_to"] = True
    e["extinct_from"] = False
    e["primary"] = [int(c not in set(o_all["code_to"])) for c in e["code_to"]]
    return e


def overlay(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame, noise: float) -> pd.DataFrame:
    o = gpd.overlay(a[["municipio_code", "geometry"]], b[["municipio_code", "geometry"]], how="intersection",
                    keep_geom_type=True)
    o["area_km2"] = o.area / 1e6
    o = o.groupby(["municipio_code_1", "municipio_code_2"], as_index=False)["area_km2"].sum()
    o = o.rename(columns={"municipio_code_1": "code_from", "municipio_code_2": "code_to"})
    o["share_from"] = o["area_km2"] / o["code_from"].map(a.set_index("municipio_code")["area"])
    o["share_to"] = o["area_km2"] / o["code_to"].map(b.set_index("municipio_code")["area"])
    return o[(o["share_from"] >= noise) | (o["share_to"] >= noise)].reset_index(drop=True)


def point_in(points: gpd.GeoDataFrame, polys: gpd.GeoDataFrame) -> dict:
    """code of the points layer -> code of the polygon containing its representative point."""
    pts = gpd.GeoDataFrame({"code": points["municipio_code"]}, geometry=points["pt"], crs=points.crs)
    j = gpd.sjoin(pts, polys[["municipio_code", "geometry"]], how="left", predicate="within")
    j = j[~j.index.duplicated()]
    return dict(zip(j["code"], j["municipio_code"]))


def classify(o: pd.DataFrame, a: gpd.GeoDataFrame, b: gpd.GeoDataFrame, cfg: dict, same_base: bool) -> pd.DataFrame:
    A, B = set(a["municipio_code"]), set(b["municipio_code"])
    o = o.copy()
    o["new_to"] = ~o["code_to"].isin(A)
    o["extinct_from"] = ~o["code_from"].isin(B)
    ps, rs = cfg["parent_share"], cfg["recode_share"]
    tr = cfg["transfer"]["same_base" if same_base else "different_base"]
    # largest contribution to each new code / largest destination of each extinct code
    o["primary"] = 0
    o.loc[o["code_from"] == o["code_to"], "primary"] = 1
    i_new = o[o["new_to"]].groupby("code_to")["area_km2"].idxmax()
    i_ext = o[o["extinct_from"]].groupby("code_from")["area_km2"].idxmax()
    o.loc[i_new.values, "primary"] = 1
    o.loc[i_ext.values, "primary"] = 1

    rel = np.full(len(o), "", dtype=object)
    same = (o["code_from"] == o["code_to"]).values
    new, ext = o["new_to"].values, o["extinct_from"].values
    sf, st, km2, prim = o["share_from"].values, o["share_to"].values, o["area_km2"].values, o["primary"].values == 1
    rel[same] = "continuation"
    recode = new & ext & (sf >= rs) & (st >= rs)
    rel[recode] = "recode"
    keep_new = new & ((st >= ps) | prim) & ~recode
    rel[keep_new & ~ext] = "split"
    rel[keep_new & ext] = "redistribution"
    keep_ext = ext & ~new & ((sf >= ps) | prim)
    rel[keep_ext] = "absorption"
    trans = ~same & ~new & ~ext & ((sf >= tr["share"]) | (st >= tr["share"])) & (km2 >= tr["min_km2"])
    rel[trans] = "transfer"
    # an extinct code's largest destination may be a new code that kept it as a minor part
    rel[(rel == "") & ext & new & prim] = "redistribution"
    o["relation"] = rel
    return o[o["relation"] != ""].reset_index(drop=True)


def genealogy_index(cfg: dict):
    g = pd.read_csv(cfg["genealogy_csv"], sep=";", dtype=str, keep_default_na=False)
    g["year"] = g["year"].astype(int)
    by_row = {r: d.set_index("year") for r, d in g.groupby("source_row")}
    at = {(y, c): r for r, y, c in zip(g["source_row"], g["year"], g["municipio_code"]) if c != NA}
    traj = {(y, c): t for t, y, c in zip(g["trajectory_code"], g["year"], g["municipio_code"]) if c != NA}
    return by_row, at, traj


def expected_refs(by_row, at, year_from: int, year_to: int, code: str, when: str):
    """Reference codes of the genealogy note for a new code (note in year_from of the row that has the code
    in year_to) or an extinct code (note in year_to of the row that has the code in year_from)."""
    key = (year_to, code) if when == "new" else (year_from, code)
    row = at.get(key)
    if row is None:
        return None
    d = by_row[row]
    y = year_from if when == "new" else year_to
    if y not in d.index:
        return None
    r = d.loc[y]
    if r["reference_code"] in (NA, ""):
        return set()
    codes = [c for c in r["reference_code"].split(" | ") if c != NA]
    return set(codes)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config, REQUIRED_KEYS)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report, "lineage")
    years = [int(y) for y in cfg["snapshots"]]
    bases = {int(y): b for y, b in cfg["snapshots"].items()}
    by_row, at, traj = genealogy_index(cfg)
    gen_years = sorted({y for y, _ in at})
    quadro = pd.read_csv(cfg["quadro_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["edition", "municipio_code"])
    quadro["edition"] = quadro["edition"].astype(int)
    first_seen = quadro.groupby("municipio_code")["edition"].apply(sorted).to_dict()

    edges, checks = [], []
    snaps = {years[0]: load_snapshot(cfg, years[0])}
    for ya, yb in zip(years[:-1], years[1:]):
        a, special_a = snaps.pop(ya)
        b, _ = snaps[yb] = load_snapshot(cfg, yb)
        same_base = bases[ya] == bases[yb]
        o_all = overlay(a, b, cfg["noise_share"])
        o = classify(o_all, a, b, cfg, same_base)
        o["name_from"] = o["code_from"].map(a.set_index("municipio_code")["municipio_name"])
        em = emergence(o_all, a, special_a, b, ya, cfg)
        if len(em):
            logger.info("  emergence: %s", em[["name_from", "code_to"]].values.tolist())
            o = pd.concat([o, em], ignore_index=True)
        o.insert(0, "year_from", ya)
        o.insert(1, "year_to", yb)
        o["same_base"] = int(same_base)
        o["name_to"] = o["code_to"].map(b.set_index("municipio_code")["municipio_name"])
        # point rule
        pb, pa = point_in(b, a), point_in(a, b)
        o["point_rule"] = pd.Series([NA] * len(o), index=o.index, dtype=object)
        m = o["relation"].isin(["split", "recode", "redistribution"])
        o.loc[m, "point_rule"] = (o.loc[m, "code_to"].map(pb) == o.loc[m, "code_from"]).astype(int)
        m = o["relation"] == "absorption"
        o.loc[m, "point_rule"] = (o.loc[m, "code_from"].map(pa) == o.loc[m, "code_to"]).astype(int)
        # genealogy
        documented = ya in gen_years and yb in gen_years
        parents = o[o["new_to"]].groupby("code_to")["code_from"].apply(set).to_dict()
        # one trajectory = the genealogy rows of one final (2010 or extinct) code; Tocantins has a Goiás-era row
        o["same_trajectory"] = [int(traj.get((ya, cf)) is not None and traj.get((ya, cf)) == traj.get((yb, ct)))
                                for cf, ct in zip(o["code_from"], o["code_to"])] if documented else NA
        o["genealogy"] = NA
        if documented:
            vals = []
            for r in o.itertuples():
                if r.relation in ("split", "recode", "redistribution"):
                    exp = expected_refs(by_row, at, ya, yb, r.code_to, "new")
                    if r.same_trajectory == 1:
                        # the genealogy row itself links both codes (recode, extinct and recreated...)
                        vals.append("agrees")
                    elif exp is None or not exp:
                        vals.append("not_documented")
                    elif r.code_from in exp:
                        vals.append("agrees")
                    elif any(r.code_from in parents.get(e, set()) for e in exp):
                        vals.append("agrees_indirect")
                    else:
                        vals.append("disagrees")
                elif r.relation == "absorption":
                    exp = expected_refs(by_row, at, ya, yb, r.code_from, "extinct")
                    vals.append("not_documented" if not exp else "agrees" if r.code_to in exp else "disagrees")
                else:
                    vals.append(NA)
            o["genealogy"] = vals
        o["created_edition"] = [next((e for e in first_seen.get(ct, []) if ya < e <= yb), NA) if nt else NA
                                for ct, nt in zip(o["code_to"], o["new_to"])]
        edges.append(o[EDGE_COLS])
        rc = o["relation"].value_counts().to_dict()
        pr = o[o["point_rule"] != NA]
        logger.info("%d -> %d (%s base): %d edges %s | point rule %d/%d | genealogy %s", ya, yb,
                    "same" if same_base else "different", len(o), rc, int((pr["point_rule"] == 1).sum()), len(pr),
                    o.loc[o["genealogy"] != NA, "genealogy"].value_counts().to_dict())
        for relname, grp in o[o["relation"] != "continuation"].groupby("relation"):
            p = grp[grp["point_rule"] != NA]
            checks.append(dict(year_from=ya, year_to=yb, relation=relname, edges=len(grp), primary=int(grp["primary"].sum()),
                               point_rule_1=int((p["point_rule"] == 1).sum()), point_rule_n=len(p),
                               point_rule_1_primary=int(((p["point_rule"] == 1) & (p["primary"] == 1)).sum()),
                               **{f"genealogy_{k}": int(v) for k, v in grp["genealogy"].value_counts().items() if k != NA}))
        n_new = len(set(b["municipio_code"]) - set(a["municipio_code"]))
        n_ext = len(set(a["municipio_code"]) - set(b["municipio_code"]))
        got_new = o.loc[o["relation"].isin(["split", "recode", "redistribution", "emergence"]), "code_to"].nunique()
        got_ext = o.loc[o["relation"].isin(["absorption", "recode", "redistribution"]), "code_from"].nunique()
        if got_new != n_new or got_ext < n_ext:
            logger.warning("  %d new codes (%d with parents), %d extinct (%d with destination)", n_new, got_new, n_ext, got_ext)

    e = pd.concat(edges, ignore_index=True)
    e["area_km2"] = e["area_km2"].round(4)
    e["share_from"] = e["share_from"].round(6)
    e["share_to"] = e["share_to"].round(6)
    out = Path(cfg["output_folder"]) / cfg["output_name"].format(YEAR=f"{years[0]}-{years[-1]}")
    write_csv(e, out, args.overwrite, logger)
    e.groupby(["year_from", "year_to", "relation"]).size().unstack(fill_value=0).to_csv(report / "lineage_summary.csv", sep=";")
    pd.DataFrame(checks).fillna(0).to_csv(report / "lineage_checks.csv", sep=";", index=False)
    dis = e[(e["genealogy"] == "disagrees") | ((e["point_rule"] == 0) & (e["primary"] == 1))]
    dis.to_csv(report / "lineage_disagreements.csv", sep=";", index=False)
    logger.info("Edges: %d %s; disagreements report %d rows", len(e), e["relation"].value_counts().to_dict(), len(dis))


if __name__ == "__main__":
    main()

"""
Visual checks of the DTB T1 (PNG figures in output_folder).

  01_snapshots.png        small multiples: municipal mesh of every snapshot (state limits on top, special
                          areas highlighted) — coverage, holes, nesting at a glance
  02_area_by_snapshot.png total mesh area and unidades area per snapshot, and number of municipalities
  03_new_by_check.png     map per interval group of the new municipalities colored by the agreement of their
                          primary parent with the genealogy / centroid rule
  04_transfers.png        transfers between continuing municipalities per interval (same vs different base)
  05_zoom_<label>.png     boundaries of selected snapshots over a region (config zooms): how the mesh splits
  06_shares.png           distribution of share_to of the primary parent of new codes, per interval

Usage:
    python check_visuals.py --config check_visuals.local.json
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dtbtools import NA, load_config, setup_logging  # noqa: E402

REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder", "snapshots", "lineage_csv", "simplify_deg"]
INK, MUTED, GRID, SURFACE, LINE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#b9b8b2"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
                                                          "#008300", "#6250d6", "#e34948")


def style(ax, grid=True):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=8)
    if grid:
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)


def mapax(ax, title):
    ax.set_axis_off()
    ax.set_aspect("equal")
    ax.set_title(title, loc="left", fontsize=10, color=INK)


def load(cfg, name, year, simplify=True):
    p = Path(cfg["input_folder"]) / f"IBGE_DTB_{name.upper()}_BRASIL_{year}.gpkg"
    g = gpd.read_file(p)
    if simplify and cfg["simplify_deg"]:
        g["geometry"] = g.geometry.simplify(cfg["simplify_deg"], preserve_topology=True)
    return g


def fig_snapshots(cfg, out, logger):
    years = cfg["snapshots"]
    n = len(years)
    cols = 5
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.4, rows * 3.6), facecolor=SURFACE)
    for ax in axes.flat:
        ax.set_axis_off()
    for ax, y in zip(axes.flat, years):
        m = load(cfg, "municipios", y)
        u = load(cfg, "unidades", y)
        real = m[m["area_type"] == "municipio"]
        real.plot(ax=ax, facecolor="#f2f1ee", edgecolor=LINE, linewidth=0.08)
        sp = m[m["area_type"] != "municipio"]
        colors = {"lagoa": BLUE, "litigio": RED, "ilha_nao_atribuida": YELLOW}
        for t, g in sp.groupby("area_type"):
            g.plot(ax=ax, facecolor=colors.get(t, MAGENTA), edgecolor="none")
        u.boundary.plot(ax=ax, color=MUTED, linewidth=0.35)
        mapax(ax, f"{y}  ·  {len(real):,} municípios".replace(",", "."))
    handles = [Patch(facecolor="#f2f1ee", edgecolor=LINE, label="município"), Line2D([], [], color=MUTED, label="limite estadual"),
               Patch(facecolor=RED, label="área em litígio"), Patch(facecolor=BLUE, label="lagoa"),
               Patch(facecolor=YELLOW, label="ilhas não atribuídas (1991)")]
    fig.legend(handles=handles, loc="lower right", frameon=False, fontsize=9, ncol=1)
    fig.suptitle("DTB T1 — malhas municipais por levantamento", x=0.01, ha="left", fontsize=13, color=INK)
    fig.tight_layout()
    fig.savefig(out / "01_snapshots.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
    plt.close(fig)
    logger.info("01_snapshots.png")


def fig_areas(cfg, out, logger):
    rows = []
    for y in cfg["snapshots"]:
        m = load(cfg, "municipios", y, simplify=False)
        u = load(cfg, "unidades", y, simplify=False)
        rows.append(dict(year=y, n=int((m["area_type"] == "municipio").sum()),
                         area_mun=m.loc[m["area_type"] == "municipio", "area_km2"].sum() / 1e6,
                         area_special=m.loc[m["area_type"] != "municipio", "area_km2"].sum() / 1e6,
                         area_uf=u["area_km2"].sum() / 1e6))
    d = pd.DataFrame(rows)
    d.to_csv(out / "02_area_by_snapshot.csv", sep=";", index=False)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), facecolor=SURFACE)
    ax = axes[0]
    style(ax)
    x = np.arange(len(d))
    ax.bar(x - 0.2, d["area_mun"], 0.38, color=BLUE, label="municípios")
    ax.bar(x - 0.2, d["area_special"], 0.38, bottom=d["area_mun"], color=YELLOW, label="áreas especiais (lagoa, litígio, ilhas)")
    ax.bar(x + 0.2, d["area_uf"], 0.38, color=LINE, label="unidades")
    ax.set_xticks(x, d["year"], rotation=45)
    ax.set_ylim(8.2, 8.6)
    ax.set_ylabel("milhões de km²", color=MUTED, fontsize=9)
    ax.set_title("Área total por levantamento (eixo a partir de 8,2)", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax = axes[1]
    style(ax)
    ax.bar(x, d["n"], 0.6, color=BLUE)
    for xi, v in zip(x, d["n"]):
        ax.annotate(f"{v:,}".replace(",", "."), (xi, v), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=7, color=INK)
    ax.set_xticks(x, d["year"], rotation=45)
    ax.set_title("Municípios por levantamento", loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out / "02_area_by_snapshot.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
    plt.close(fig)
    logger.info("02_area_by_snapshot.png\n%s", d.round(4).to_string(index=False))


def fig_new_checks(cfg, e, out, logger):
    groups = cfg["interval_groups"]
    fig, axes = plt.subplots(1, len(groups), figsize=(len(groups) * 4.6, 5.2), facecolor=SURFACE)
    cats = [("agrees", "genealogia e centroide concordam", BLUE), ("point_only", "só a genealogia discorda", YELLOW),
            ("gen_only", "só o centroide discorda", MAGENTA), ("both", "ambos discordam", RED),
            ("not_documented", "sem nota na genealogia (inclui 2010→2022)", LINE)]
    for ax, gr in zip(np.atleast_1d(axes), groups):
        y0, y1 = gr["from"], gr["to"]
        base = load(cfg, "unidades", y1)
        base.plot(ax=ax, facecolor="#f2f1ee", edgecolor=LINE, linewidth=0.3)
        p = e[(e["year_from"] >= y0) & (e["year_to"] <= y1) & (e["primary"] == "1") &
              e["relation"].isin(["split", "recode", "redistribution"])].copy()
        gen_ok = p["genealogy"].str.startswith("agrees")
        pt_ok = p["point_rule"] == "1"
        p["cat"] = np.select([p["genealogy"].isin(["not_documented", NA]), gen_ok & pt_ok, ~gen_ok & pt_ok, gen_ok & ~pt_ok],
                             ["not_documented", "agrees", "point_only", "gen_only"], "both")
        pts = []
        for yt, g in p.groupby("year_to"):
            m = load(cfg, "municipios", yt).set_index("municipio_code")
            g = g[g["code_to"].isin(m.index)]
            pts.append(gpd.GeoDataFrame(g, geometry=m.loc[g["code_to"]].geometry.representative_point().values, crs=m.crs))
        pts = pd.concat(pts)
        for key, lab, col in cats:
            s = pts[pts["cat"] == key]
            if len(s):
                s.plot(ax=ax, color=col, markersize=3 if key == "agrees" else 10, zorder=3 if key == "agrees" else 4)
        counts = pts["cat"].value_counts()
        mapax(ax, f"{y0}→{y1}: {len(pts):,} códigos novos".replace(",", "."))
        txt = "\n".join(f"{lab}: {counts.get(k, 0)}" for k, lab, _ in cats if counts.get(k, 0))
        ax.text(0.01, 0.01, txt, transform=ax.transAxes, fontsize=7, color=INK, va="bottom")
    handles = [Line2D([], [], marker="o", linestyle="", color=c, label=l) for _, l, c in cats]
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False, fontsize=9)
    fig.suptitle("Códigos novos: o pai principal (maior área) confere com a genealogia do IBGE e com o centroide?",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(out / "03_new_by_check.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
    plt.close(fig)
    logger.info("03_new_by_check.png")


def fig_transfers(cfg, e, out, logger):
    t = e[e["relation"] == "transfer"].copy()
    t["km2"] = t["area_km2"].astype(float)
    t["sh"] = np.maximum(t["share_from"].astype(float), t["share_to"].astype(float))
    t["interval"] = t["year_from"].astype(str) + "→" + t["year_to"].astype(str).str[-2:]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4), facecolor=SURFACE)
    ax = axes[0]
    style(ax)
    order = sorted(t["interval"].unique(), key=lambda s: int(s[:4]))
    c = t.groupby("interval").size().reindex(order)
    cols = [BLUE if int(i[:4]) < 1991 else ORANGE for i in order]
    ax.bar(range(len(order)), c.values, color=cols)
    ax.set_xticks(range(len(order)), order, rotation=45, ha="right")
    ax.set_title("Transferências entre municípios contínuos", loc="left", fontsize=11, color=INK)
    ax.legend(handles=[Patch(color=BLUE, label="mesma base cartográfica (1872–1991)"),
                       Patch(color=ORANGE, label="bases diferentes (1991→2000→2010→2022)")], frameon=False, fontsize=8)
    ax = axes[1]
    style(ax, grid=False)
    ax.grid(color=GRID, linewidth=0.6)
    for same, col in ((1, BLUE), (0, ORANGE)):
        s = t[t["same_base"] == str(same)]
        ax.scatter(s["km2"], s["sh"], s=12, color=col, edgecolor=SURFACE, linewidth=0.5, alpha=0.85)
    ax.set_xscale("log")
    ax.set_xlabel("área transferida (km², log)", color=MUTED, fontsize=9)
    ax.set_ylabel("maior parcela (share_from / share_to)", color=MUTED, fontsize=9)
    ax.set_title("Tamanho das transferências", loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out / "04_transfers.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
    plt.close(fig)
    logger.info("04_transfers.png")


def fig_zooms(cfg, out, logger):
    for z in cfg.get("zooms", []):
        years = z["years"]
        fig, axes = plt.subplots(1, len(years), figsize=(len(years) * 3.6, 4.2), facecolor=SURFACE)
        x0, y0, x1, y1 = z["bbox"]
        for ax, y in zip(np.atleast_1d(axes), years):
            m = load(cfg, "municipios", y, simplify=False).cx[x0:x1, y0:y1]
            m.plot(ax=ax, facecolor="#f2f1ee", edgecolor=MUTED, linewidth=0.4)
            if z.get("highlight"):
                h = m[m["municipio_code"].isin(z["highlight"])]
                h.plot(ax=ax, facecolor=BLUE, edgecolor=MUTED, linewidth=0.4, alpha=0.8)
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
            mapax(ax, f"{y}: {len(m[m['area_type'] == 'municipio'])} municípios")
        fig.suptitle(z["title"], x=0.01, ha="left", fontsize=12, color=INK)
        fig.tight_layout()
        fig.savefig(out / f"05_zoom_{z['label']}.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
        plt.close(fig)
        logger.info("05_zoom_%s.png", z["label"])


def fig_shares(cfg, e, out, logger):
    p = e[(e["primary"] == "1") & e["relation"].isin(["split", "recode", "redistribution"])].copy()
    p["st"] = p["share_to"].astype(float)
    p["interval"] = p["year_from"].astype(str) + "→" + p["year_to"].astype(str).str[-2:]
    order = sorted(p["interval"].unique(), key=lambda s: int(s[:4]))
    fig, ax = plt.subplots(figsize=(11, 4.2), facecolor=SURFACE)
    style(ax)
    data = [p.loc[p["interval"] == i, "st"].values for i in order]
    bp = ax.boxplot(data, widths=0.5, patch_artist=True, showfliers=True,
                    flierprops=dict(marker="o", markersize=3, markerfacecolor=ORANGE, markeredgecolor="none"))
    for b in bp["boxes"]:
        b.set(facecolor="#d6e4f7", edgecolor=BLUE)
    for k in ("whiskers", "caps", "medians"):
        for l in bp[k]:
            l.set(color=BLUE)
    ax.set_xticks(range(1, len(order) + 1), order, rotation=45, ha="right")
    ax.set_ylim(0, 1.05)
    ax.set_title("Parte do território do código novo vinda do pai principal (share_to); 1 = um só pai",
                 loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out / "06_shares.png", dpi=cfg.get("dpi", 130), facecolor=SURFACE)
    plt.close(fig)
    logger.info("06_shares.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--only", nargs="*", help="Only these figures (01 02 03 04 05 06)")
    args = parser.parse_args()
    cfg = load_config(args.config, REQUIRED_KEYS)
    logger = setup_logging(Path(cfg["report_folder"]), "visuals")
    out = Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    e = pd.read_csv(cfg["lineage_csv"], sep=";", dtype=str, keep_default_na=False)
    e["year_from"] = e["year_from"].astype(int)
    e["year_to"] = e["year_to"].astype(int)
    run = lambda k: not args.only or k in args.only  # noqa: E731
    if run("01"):
        fig_snapshots(cfg, out, logger)
    if run("02"):
        fig_areas(cfg, out, logger)
    if run("03"):
        fig_new_checks(cfg, e, out, logger)
    if run("04"):
        fig_transfers(cfg, e, out, logger)
    if run("05"):
        fig_zooms(cfg, out, logger)
    if run("06"):
        fig_shares(cfg, e, out, logger)


if __name__ == "__main__":
    main()

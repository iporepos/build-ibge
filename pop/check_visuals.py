"""
Visual checks of the POP T1 (PNG figures in output_folder).

  01_brasil_total.png      national population 1872-2026: sum of census municipalities, sum of estimate
                           municipalities and published Brasil totals
  02_unidades.png          small multiples per UF (code of the year: Goiás before 1989 includes Tocantins,
                           Mato Grosso before 1979 includes MS): census and estimate sums
  03_growth.png            annual growth of every municipality between consecutive estimate years (and
                           censuses of growth_censuses as points): distribution per interval and the largest jumps
  04_estimate_vs_census.png estimates next to a census (config census_pairs) per municipality
  05_map_population.png    population of the last estimate year on the DTB 2022 mesh, and annual growth
                           between two estimate years (config map_growth)
  growth_outliers.csv      municipality-intervals with |annual growth| above outlier_growth

censos_csv, estimativas_csv and unidades_csv are glob patterns of the per-year T1 files
(e.g. .../IBGE_POP_CENSOS-MUNICIPIOS_BRASIL_[0-9][0-9][0-9][0-9].csv); all matching files are read.

Usage:
    python check_visuals.py --config check_visuals.local.json
"""

import argparse
import glob
import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, LogNorm, TwoSlopeNorm  # noqa: E402

INK, MUTED, GRID, SURFACE, LINE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#b9b8b2"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SEQ = LinearSegmentedColormap.from_list("seq_blue", ["#eaf2fc", "#9cc3ef", "#2a78d6", "#123f78"])
DIV = LinearSegmentedColormap.from_list("div", ["#b8461b", "#f2b79c", "#ecebe7", "#9cc3ef", "#1d5aa6"])
NODATA = -9999
REQUIRED_KEYS = ["censos_csv", "estimativas_csv", "unidades_csv", "mesh_2022", "output_folder", "unidade_siglas"]


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


def millions(ax, decimals=0):
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1e6:.{decimals}f}".replace(".", ",")))


def read_all(pattern: str, dtype: dict) -> pd.DataFrame:
    paths = sorted(glob.glob(pattern))
    if not paths:
        sys.exit(f"No file matches {pattern}")
    return pd.concat([pd.read_csv(p, sep=";", dtype=dtype) for p in paths], ignore_index=True)


def load(cfg):
    c = read_all(cfg["censos_csv"], {"municipio_code": str})
    e = read_all(cfg["estimativas_csv"], {"municipio_code": str, "unidade_code": str})
    u = read_all(cfg["unidades_csv"], {"code": str})
    c = c[c["population"] != NODATA]
    e = e[e["population"] != NODATA]
    c["uf"] = c["municipio_code"].str[:2]
    e["uf"] = e["municipio_code"].str[:2]
    return c, e, u


def fig_total(c, e, u, out, dpi):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), facecolor=SURFACE, gridspec_kw={"width_ratios": [1.3, 1]})
    cs = c.groupby("year")["population"].sum()
    es = e.groupby("year")["population"].sum()
    br = u[u["level"] == "brasil"].set_index("year")["population"]
    for ax, y0 in zip(axes, (1872, 1990)):
        style(ax)
        s = cs.loc[y0:]
        ax.plot(s.index, s.values, color=BLUE, linewidth=2, marker="o", markersize=6, markeredgecolor=SURFACE,
                markeredgewidth=2, label="censos (soma dos municípios)", zorder=3)
        ax.plot(es.index, es.values, color=ORANGE, linewidth=0, marker="o", markersize=5, markeredgecolor=SURFACE,
                markeredgewidth=1.5, label="estimativas (soma dos municípios)", zorder=4)
        ax.plot(br.index, br.values, color=INK, linewidth=0, marker="_", markersize=12, label="estimativas: total Brasil publicado", zorder=5)
        millions(ax)
        ax.set_ylabel("milhões de habitantes", color=MUTED, fontsize=9)
    for y in (1872, 1940, 1991, 2010, 2022):
        below = y == cs.index.max() and y > 2010  # last census sits under the estimate markers
        axes[0].annotate(f"{cs[y] / 1e6:.1f}".replace(".", ","), (y, cs[y]), textcoords="offset points",
                         xytext=(4, -14) if below else (-6, 6), ha="left" if below else "right", fontsize=8, color=INK)
    axes[0].set_title(f"Censos {cs.index.min()}–{cs.index.max()} e estimativas {es.index.min()}–{es.index.max()}", loc="left", fontsize=11, color=INK)
    axes[0].legend(frameon=False, fontsize=9, loc="upper left")
    axes[1].set_title(f"Detalhe 1990–{es.index.max()} (eixo a partir de 140 milhões)", loc="left", fontsize=11, color=INK)
    axes[1].set_ylim(140e6, 220e6)
    fig.tight_layout()
    fig.savefig(out / "01_brasil_total.png", dpi=dpi, facecolor=SURFACE)
    plt.close(fig)
    return pd.DataFrame({"census_sum": cs, "estimate_sum": es, "brasil_published": br})


def fig_unidades(c, e, out, siglas, dpi):
    ufs = sorted(set(c["uf"]) | set(e["uf"]))
    ufs = [u for u in ufs if u in siglas]
    cols = 7
    rows = int(np.ceil(len(ufs) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.3, rows * 1.9), facecolor=SURFACE, sharex=True)
    cs = c.groupby(["uf", "year"])["population"].sum()
    es = e.groupby(["uf", "year"])["population"].sum()
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, uf in zip(axes.flat, ufs):
        ax.set_visible(True)
        style(ax)
        if uf in cs.index.get_level_values(0):
            s = cs.loc[uf]
            ax.plot(s.index, s.values, color=BLUE, linewidth=1.6, marker="o", markersize=2.5)
        if uf in es.index.get_level_values(0):
            s = es.loc[uf]
            ax.plot(s.index, s.values, color=ORANGE, linewidth=0, marker="o", markersize=2)
        top = max(cs.loc[uf].max() if uf in cs.index.get_level_values(0) else 0,
                  es.loc[uf].max() if uf in es.index.get_level_values(0) else 0)
        millions(ax, 0 if top >= 1e7 else 1 if top >= 1e6 else 2)
        ax.set_title(f"{siglas[uf]} ({uf})", loc="left", fontsize=9, color=INK)
        ax.tick_params(labelsize=7)
        ax.set_ylim(bottom=0)
    fig.suptitle("Por UF (código do ano; milhões): censos em azul, estimativas em laranja. GO inclui TO até 1980, MT inclui MS até 1970",
                 x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out / "02_unidades.png", dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


def growth_table(c, e, censuses):
    """Annual growth per municipality between consecutive series points (estimates + the given census years)."""
    pts = pd.concat([e[["year", "municipio_code", "municipio_name", "population"]].assign(src="estimativa"),
                     c[c["year"].isin(censuses)][["year", "municipio_code", "municipio_name", "population"]].assign(src="censo")])
    pts = pts.sort_values(["municipio_code", "year"])
    pts["prev_year"] = pts.groupby("municipio_code")["year"].shift()
    pts["prev_pop"] = pts.groupby("municipio_code")["population"].shift()
    g = pts.dropna(subset=["prev_year"]).copy()
    g = g[(g["population"] > 0) & (g["prev_pop"] > 0)]
    g["years"] = g["year"] - g["prev_year"]
    g["growth"] = (g["population"] / g["prev_pop"]) ** (1 / g["years"]) - 1
    g["interval"] = g["prev_year"].astype(int).astype(str) + "→" + g["year"].astype(str).str[-2:]
    return g


def fig_growth(g, out, dpi, limit, censuses):
    order = sorted(g["interval"].unique(), key=lambda s: int(s[:4]))
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), facecolor=SURFACE, gridspec_kw={"height_ratios": [2, 1]})
    ax = axes[0]
    style(ax)
    data = [100 * g.loc[g["interval"] == i, "growth"].clip(-0.3, 0.3).values for i in order]
    bp = ax.boxplot(data, widths=0.55, patch_artist=True, showfliers=True,
                    flierprops=dict(marker="o", markersize=2, markerfacecolor=ORANGE, markeredgecolor="none", alpha=0.6))
    for b in bp["boxes"]:
        b.set(facecolor="#d6e4f7", edgecolor=BLUE)
    for k in ("whiskers", "caps", "medians"):
        for ln in bp[k]:
            ln.set(color=BLUE)
    ax.set_xticks(range(1, len(order) + 1), order, rotation=60, ha="right", rotation_mode="anchor", fontsize=7)
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.set_ylabel("% ao ano (cortado em ±30)", color=MUTED, fontsize=9)
    ax.set_title("Crescimento anual por município entre pontos consecutivos da série (estimativas; " + ", ".join(map(str, censuses)) + " = censo)",
                 loc="left", fontsize=11, color=INK)
    ax = axes[1]
    style(ax)
    n = g.assign(out=g["growth"].abs() > limit).groupby("interval")["out"].sum().reindex(order)
    ax.bar(range(len(order)), n.values, color=ORANGE)
    ax.set_xticks(range(len(order)), order, rotation=60, ha="right", rotation_mode="anchor", fontsize=7)
    ax.set_title(f"Municípios com |crescimento| > {limit:.0%} ao ano (desmembramentos, populações judiciais, recontagens)",
                 loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(out / "03_growth.png", dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


def fig_vs_census(c, e, out, dpi, pairs):
    fig, axes = plt.subplots(1, len(pairs), figsize=(4.2 * len(pairs), 4.4), facecolor=SURFACE)
    for ax, (ye, yc) in zip(np.atleast_1d(axes), pairs):
        style(ax, grid=False)
        ax.grid(color=GRID, linewidth=0.6)
        a = e[e["year"] == ye].set_index("municipio_code")["population"]
        b = c[c["year"] == yc].set_index("municipio_code")["population"]
        j = pd.concat([a.rename("e"), b.rename("c")], axis=1, join="inner")
        j = j[(j["e"] > 0) & (j["c"] > 0)]
        r = np.log10(j["e"] / j["c"])
        far = r.abs() > np.log10(1.25)
        ax.scatter(j.loc[~far, "c"], j.loc[~far, "e"], s=4, color=BLUE, alpha=0.5, edgecolor="none")
        ax.scatter(j.loc[far, "c"], j.loc[far, "e"], s=10, color=ORANGE, edgecolor="none")
        lo, hi = j.min().min() * 0.8, j.max().max() * 1.2
        ax.plot([lo, hi], [lo, hi], color=MUTED, linewidth=0.8)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(f"censo {yc}", color=MUTED, fontsize=9)
        ax.set_ylabel(f"estimativa {ye}", color=MUTED, fontsize=9)
        ax.set_title(f"{ye} × censo {yc}: {len(j):,} pares, {int(far.sum())} fora de ±25%".replace(",", "."),
                     loc="left", fontsize=10, color=INK)
    fig.suptitle("Estimativas ao lado de um censo, por município (mesmo código); laranja = diferença > 25%", x=0.01,
                 ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out / "04_estimate_vs_census.png", dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


def fig_maps(cfg, e, out, dpi):
    m = gpd.read_file(cfg["mesh_2022"])
    m["geometry"] = m.geometry.simplify(cfg.get("simplify_deg", 0.01), preserve_topology=True)
    y1 = int(e["year"].max())
    y0, ya = cfg["map_growth"]
    p1 = e[e["year"] == y1].set_index("municipio_code")["population"]
    a = e[e["year"] == y0].set_index("municipio_code")["population"]
    b = e[e["year"] == ya].set_index("municipio_code")["population"]
    gr = 100 * ((b / a) ** (1 / (ya - y0)) - 1)
    m["pop"] = m["municipio_code"].map(p1)
    m["gr"] = m["municipio_code"].map(gr)
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.6), facecolor=SURFACE)
    for ax in axes:
        ax.set_axis_off()
        ax.set_aspect("equal")
    real = m[m["area_type"] == "municipio"]
    real.plot(ax=axes[0], column="pop", cmap=SEQ, norm=LogNorm(vmin=1e3, vmax=1e7), linewidth=0, legend=True,
              legend_kwds={"shrink": 0.6, "label": "habitantes (log)"}, missing_kwds={"color": "#d9d8d3"})
    axes[0].set_title(f"População estimada {y1} (malha DTB 2022; cinza = sem estimativa)", loc="left", fontsize=11, color=INK)
    lim = cfg.get("growth_map_limit", 3)
    real.plot(ax=axes[1], column="gr", cmap=DIV, norm=TwoSlopeNorm(vmin=-lim, vcenter=0, vmax=lim), linewidth=0,
              legend=True, legend_kwds={"shrink": 0.6, "label": "% ao ano (cortado)"}, missing_kwds={"color": "#d9d8d3"})
    axes[1].set_title(f"Crescimento anual das estimativas {y0}–{ya}", loc="left", fontsize=11, color=INK)
    for ax in axes:
        m[m["area_type"] != "municipio"].plot(ax=ax, color="#b7d3f3", linewidth=0)
    fig.tight_layout()
    fig.savefig(out / "05_map_population.png", dpi=dpi, facecolor=SURFACE)
    plt.close(fig)
    return int(real["pop"].isna().sum())


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config is missing {missing}")
    out = Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    dpi = cfg.get("dpi", 130)
    c, e, u = load(cfg)
    tot = fig_total(c, e, u, out, dpi)
    tot.to_csv(out / "01_brasil_total.csv", sep=";")
    print(tot.dropna(how="all").to_string())
    fig_unidades(c, e, out, cfg["unidade_siglas"], dpi)
    censuses = cfg.get("growth_censuses", [2010])
    g = growth_table(c, e, censuses)
    limit = cfg.get("outlier_growth", 0.15)
    fig_growth(g, out, dpi, limit, censuses)
    o = g[g["growth"].abs() > limit].sort_values("growth")
    o[["interval", "municipio_code", "municipio_name", "prev_pop", "population", "growth"]].to_csv(out / "growth_outliers.csv", sep=";", index=False)
    print(f"growth outliers > {limit:.0%}: {len(o)}")
    print(o.groupby("interval").size().to_string())
    fig_vs_census(c, e, out, dpi, cfg.get("census_pairs", [[1992, 1991], [2000, 2000], [2009, 2010], [2011, 2010]]))
    miss = fig_maps(cfg, e, out, dpi)
    print("municipalities of the 2022 mesh without estimate:", miss)


if __name__ == "__main__":
    main()

"""
Sanity check of the T1 census data: distribution (CDF) of the tract mean income of household heads, per census.

The one income measure every census has at the tract level is the mean nominal monthly income of the heads of
(permanent private) households. For each census of the config:
  * mean: the tract mean (table, variable), in the currency of the census
  * with_income_from_all_heads (optional): when the mean is over all heads (with and without income), it is put
    on the "heads with income" basis of the later censuses: mean x (with + without) / with, from the band counts
  * weight: the heads (households) of the tract; the exceedance curve gives, for a share p of the households,
    the tract mean that p of the households live above (the CDF with the axes flipped)
  * minimum_wage: the minimum wage at the census date, so that the four currencies share one scale
Tracts with a flagged (< 0) or missing mean or weight, with weight 0, or with mean 0 (no head with income) are
left out and counted. The national CDF uses the states that have data; states without any row are named in the
figures (e.g. a state missing from a table).

Outputs (output_folder):
  income_quantiles.csv         per census and state (BR = national): tracts and heads used / left out, weighted
                               quantiles and mean of the tract means, in the currency and in minimum wages, and the
                               spread ratios p90/p10 and p75/p25 (unit free: the shape of the distribution)
  income_exceedance.csv        national exceedance curves per census: the tract mean exceeded by a share p of the
                               households, in minimum wages and relative to the national median
  income_exceedance_national_<scale>.png  national exceedance curves (scale log / linear): in minimum wages, and
                               relative to the national median
  income_exceedance_states_<scale>.png  one panel per state with the censuses overlaid, relative to the state median
                               (shape only: the flatter the curve, the more alike the tracts of the state; a
                               horizontal line at 1 would be perfect equality)
  income_spread_states.png     p90/p10 and p75/p25 of the tract means per state and census
  log check_income_<timestamp>.txt

Usage:
    python check_income.py --config check_income.local.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

REQUIRED_KEYS = ["output_folder", "censuses", "states"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
YEARS = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]  # sequential blue, oldest light


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("points", 400)
    cfg.setdefault("linear_max", {"minimum_wages": 20, "relative_to_median": 6})
    cfg.setdefault("quantiles", [0.1, 0.25, 0.5, 0.75, 0.9])
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"check_income_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("check_income")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger


def read_columns(c: dict, table: str, columns: list) -> pd.DataFrame:
    path = Path(c["folder"]) / c["table_pattern"].format(NAME=table.upper())
    d = pd.read_csv(path, sep=";", usecols=["setor_code"] + columns, dtype={"setor_code": str})
    return d.set_index("setor_code")


def tract_means(c: dict) -> pd.DataFrame:
    """setor_code -> mean (currency, heads with income basis), weight (heads)."""
    m = read_columns(c, c["mean"]["table"], [c["mean"]["variable"]])[c["mean"]["variable"]].rename("mean")
    w = read_columns(c, c["weight"]["table"], [c["weight"]["variable"]])[c["weight"]["variable"]].rename("weight")
    d = pd.concat([m, w], axis=1)
    conv = c.get("with_income_from_all_heads")
    if conv:
        b = read_columns(c, conv["table"], conv["with_income"] + conv["without_income"]).clip(lower=0)
        inc = b[conv["with_income"]].sum(axis=1).reindex(d.index)
        zero = b[conv["without_income"]].sum(axis=1).reindex(d.index)
        ok = (d["mean"] > 0) & (inc > 0)
        d["mean"] = d["mean"].where(~ok, d["mean"] * (inc + zero) / inc.where(inc > 0))
    d["state"] = d.index.str[:2]
    return d


def weighted_quantiles(x: np.ndarray, w: np.ndarray, qs) -> np.ndarray:
    o = np.argsort(x)
    cw = np.cumsum(w[o]) / w.sum()
    return x[o][np.minimum(np.searchsorted(cw, qs), len(x) - 1)]


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=7, length=0)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def exceedance_axes(ax, lo, hi, scale="log", small=False):
    """x: share of households above (0-100%); y: income on a log or linear scale. small: panel of a grid, no axis
    label."""
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1] if small else [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1])
    if scale == "log":
        ax.set_yscale("log")
        ax.set_ylim(lo, hi)
        ticks = [t for t in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50) if lo <= t <= hi]
        ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
        ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    else:
        ax.set_ylim(0, hi)
    if not small:
        ax.set_xlabel("Share of households living in tracts above this income (exceedance)", color=INK_2, fontsize=8)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    args = ap.parse_args()
    cfg = load_config(args.config)
    out = Path(cfg["output_folder"])
    logger = setup_logging(out)
    states = cfg["states"]
    qs = cfg["quantiles"]
    probs = np.linspace(0, 1, cfg["points"] + 1)[1:-1]
    rows, cdf_rows = [], []
    data, missing_by_year, wages = {}, {}, {}

    for c in sorted(cfg["censuses"], key=lambda c: c["year"]):
        year = c["year"]
        sm = c["minimum_wage"]
        wages[year] = f"{c['currency']} {sm:,.0f}" if float(sm).is_integer() else f"{c['currency']} {sm:,.2f}"
        d = tract_means(c)
        d = d[d.state.isin(states)]
        present = sorted(d.state.unique())
        missing_by_year[year] = [states[s] for s in sorted(set(states) - set(present))]
        bad_mean = d["mean"].isna() | (d["mean"] < 0)
        bad_weight = d["weight"].isna() | (d["weight"] <= 0)
        zero = ~bad_mean & (d["mean"] == 0)
        use = ~bad_mean & ~bad_weight & ~zero
        heads = d["weight"].clip(lower=0).fillna(0)
        d = d.assign(sm=d["mean"] / sm)
        data[year] = d[use]
        logger.info("%d: %d tracts, used %d; left out: mean flagged/missing %d (%d heads), weight 0/flagged %d, "
                    "mean 0 %d (%d heads); states missing %s", year, len(d), int(use.sum()), int(bad_mean.sum()),
                    int(heads[bad_mean].sum()), int((bad_weight & ~bad_mean).sum()), int(zero.sum()),
                    int(heads[zero].sum()), missing_by_year[year] or "none")
        for s in present + ["BR"]:
            sel = (d.state == s) if s != "BR" else pd.Series(True, index=d.index)
            u = d[sel & use]
            x, w = u["mean"].to_numpy(float), u["weight"].to_numpy(float)
            q = weighted_quantiles(x, w, qs)
            row = dict(year=year, state=states.get(s, s), currency=c["currency"], minimum_wage=sm,
                       tracts=int(sel.sum()), tracts_used=len(u), heads_used=int(w.sum()),
                       heads_left_out=int(heads[sel & ~use].sum()),
                       mean_of_tract_means=round(float(np.average(x, weights=w)), 2))
            row.update({f"p{int(100 * k)}": round(float(v), 2) for k, v in zip(qs, q)})
            row.update({f"p{int(100 * k)}_sm": round(float(v / sm), 3) for k, v in zip(qs, q)})
            p = dict(zip([0.1, 0.25, 0.5, 0.75, 0.9], weighted_quantiles(x, w, [0.1, 0.25, 0.5, 0.75, 0.9])))
            row.update(p90_p10=round(float(p[0.9] / p[0.1]), 3), p75_p25=round(float(p[0.75] / p[0.25]), 3))
            row["missing_states"] = " ".join(missing_by_year[year]) if s == "BR" else None
            rows.append(row)
            if s == "BR":
                med = q[qs.index(0.5)] / sm if 0.5 in qs else weighted_quantiles(x, w, [0.5])[0] / sm
                ex = weighted_quantiles(x / sm, w, 1 - probs)
                cdf_rows += [dict(year=year, exceedance=round(float(p), 5), minimum_wages=round(float(v), 5),
                                  relative_to_median=round(float(v / med), 5)) for p, v in zip(probs, ex)]

    qt = pd.DataFrame(rows)
    cd = pd.DataFrame(cdf_rows)
    qt.to_csv(out / "income_quantiles.csv", sep=";", index=False)
    cd.to_csv(out / "income_exceedance.csv", sep=";", index=False)
    years = sorted(data)
    colors = dict(zip(years, YEARS))
    wage_note = ("Minimum wage at the census date: " + "; ".join(f"{y} {wages[y]}" for y in years) + ".").replace("$", r"\$")
    base_note = ("Tract mean of the monthly nominal income of the household heads with income (1991: converted from "
                 "the all-heads mean with the band counts); weighted by the households of the tract.")
    miss = [f"{y}: {', '.join(v)}" for y, v in missing_by_year.items() if v]
    miss_note = f"States without data left out: {'; '.join(miss)}." if miss else ""

    order = list(states)
    lin = cfg["linear_max"]
    for scale in ("log", "linear"):
        cut_note = ("Linear scale cut at " + ", ".join(f"{v:g} ({k.replace('_', ' ')})" for k, v in lin.items()) +
                    ": the richest tracts run off the top.") if scale == "linear" else ""
        # --- national exceedance curves ---------------------------------------------------------------
        fig, axes = plt.subplots(1, 2, figsize=(12, 5.2), facecolor=SURFACE)
        for ax, col, lo, hi, ylabel in (
                (axes[0], "minimum_wages", 0.25, 50, "Tract mean income of the heads, in minimum wages"),
                (axes[1], "relative_to_median", 0.1, 20, "Tract mean income relative to the national median")):
            style(ax)
            ax.axvline(0.5, color=INK_2, linewidth=0.6, linestyle=(0, (2, 2)))
            if col == "relative_to_median":
                ax.axhline(1, color=INK_2, linewidth=0.6, linestyle=(0, (2, 2)))
            for y in years:
                v = cd[cd.year == y]
                ax.plot(v.exceedance, v[col], color=colors[y], linewidth=2, label=str(y))
            exceedance_axes(ax, lo, hi if scale == "log" else lin[col], scale)
            ax.set_ylabel(ylabel + f" ({scale} scale)", color=INK_2, fontsize=8)
        axes[0].set_title("In minimum wages of the census date", color=INK, fontsize=10, loc="left")
        axes[1].set_title("Relative to the national median (shape only: flatter = more equal)", color=INK, fontsize=10,
                          loc="left")
        axes[1].legend(frameon=False, fontsize=8, loc="upper right", title="Census", title_fontsize=8)
        med_txt = "National medians (minimum wages): " + ", ".join(
            f"{y} {qt[(qt.year == y) & (qt.state == 'BR')].p50_sm.iloc[0]:.2f}" for y in years)
        fig.suptitle("Brazil: tract mean income of household heads exceeded by a share of the households, by census",
                     color=INK, fontsize=12, x=0.01, ha="left")
        fig.text(0.01, 0.01, "\n".join(t for t in (base_note, wage_note + " " + med_txt, miss_note, cut_note) if t),
                 color=INK_2, fontsize=7, va="bottom")
        fig.tight_layout(rect=(0, 0.1, 1, 0.97))
        fig.savefig(out / f"income_exceedance_national_{scale}.png", dpi=150)
        plt.close(fig)

        # --- states, censuses overlaid -----------------------------------------------------------------
        cols = 7
        nrows = -(-len(order) // cols)
        fig, axes = plt.subplots(nrows, cols, figsize=(cols * 2.2, nrows * 2.2), facecolor=SURFACE, sharex=True,
                                 sharey=True)
        for i, (ax, s) in enumerate(zip(axes.flat, order)):
            style(ax)
            ax.axvline(0.5, color=INK_2, linewidth=0.5, linestyle=(0, (2, 2)))
            ax.axhline(1, color=INK_2, linewidth=0.5, linestyle=(0, (2, 2)))
            lacking = []
            for y in years:
                u = data[y][data[y].state == s]
                if u.empty:
                    lacking.append(str(y))
                    continue
                x, w = u["mean"].to_numpy(float), u.weight.to_numpy(float)
                ax.plot(probs, weighted_quantiles(x, w, 1 - probs) / weighted_quantiles(x, w, [0.5])[0],
                        color=colors[y], linewidth=1.4)
            exceedance_axes(ax, 0.1, 20 if scale == "log" else lin["relative_to_median"], scale, small=True)
            ax.set_title(states[s] + (f"  (no {', '.join(lacking)})" if lacking else ""), color=INK, fontsize=9,
                         loc="left", fontweight="bold")
        for ax in list(axes.flat)[len(order):]:
            ax.axis("off")
        fig.legend(handles=[Line2D([], [], color=colors[y], linewidth=2, label=str(y)) for y in years], ncol=4,
                   frameon=False, fontsize=8, loc="lower right", title="Census", title_fontsize=8)
        fig.suptitle("Tract mean income of household heads exceeded by a share of the households, per state, relative "
                     f"to the state median (shape only; {scale} scale)", color=INK, fontsize=12, x=0.01, ha="left")
        fig.supxlabel("Share of households living in tracts above this income (exceedance)", color=INK_2, fontsize=9,
                      y=0.045)
        fig.supylabel(f"Tract mean income relative to the state median ({scale} scale)", color=INK_2, fontsize=9)
        fig.text(0.01, 0.005, base_note + "\nA flatter curve means tracts more alike within the state (a horizontal line "
                 "at 1 = perfect equality); a high left end means a few rich tracts." + (f" Linear scale cut at {lin['relative_to_median']:g}: the richest tracts run off the top." if scale == "linear" else ""), color=INK_2, fontsize=7,
                 va="bottom")
        fig.tight_layout(rect=(0, 0.06, 1, 0.97))
        fig.savefig(out / f"income_exceedance_states_{scale}.png", dpi=130)
        plt.close(fig)

    # --- state spread: p90/p10 (dot) and p75/p25 (open dot) ----------------------------------------
    st = qt[qt.state != "BR"]
    last = st[st.year == years[-1]].sort_values("p90_p10")
    order_uf = list(last.state) + [states[s] for s in order if states[s] not in set(last.state)]
    fig, ax = plt.subplots(figsize=(8, 8.5), facecolor=SURFACE)
    style(ax)
    ax.grid(axis="y", visible=False)
    off = np.linspace(-0.27, 0.27, len(years))
    for k, y in enumerate(years):
        v = st[st.year == y].set_index("state").reindex(order_uf)
        pos = np.arange(len(order_uf)) + off[k]
        ax.hlines(pos, v.p75_p25, v.p90_p10, color=colors[y], linewidth=1.0, alpha=0.6)
        ax.plot(v.p75_p25, pos, "o", color=SURFACE, markersize=5, markeredgecolor=colors[y], markeredgewidth=1.2)
        ax.plot(v.p90_p10, pos, "o", color=colors[y], markersize=5, markeredgecolor=SURFACE, markeredgewidth=0.8,
                label=str(y))
        br = qt[(qt.year == y) & (qt.state == "BR")].iloc[0]
        ax.axvline(br.p90_p10, color=colors[y], linewidth=0.8, linestyle=(0, (3, 2)))
    ax.set_yticks(range(len(order_uf)), order_uf)
    ax.set_ylim(-0.6, len(order_uf) - 0.4)
    ax.set_xscale("log")
    ticks = [1.5, 2, 3, 4, 6, 8, 10]
    ax.set_xticks(ticks, [f"{t:g}" for t in ticks])
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("Ratio between quantiles of the tract mean income of the heads (household weighted, log scale): "
                  "p90/p10 (dot), p75/p25 (open dot)", color=INK_2, fontsize=8)
    ax.legend(frameon=False, fontsize=8, loc="lower right", title="Census (dashed: national p90/p10)",
              title_fontsize=8)
    ax.set_title(f"Spread of the tract mean income of household heads per state\n(sorted by the {years[-1]} p90/p10; "
                 "further right = more unequal between tracts)", color=INK, fontsize=10, loc="left")
    ax.set_xlim(1.4, 12)
    fig.text(0.01, 0.005, base_note + (" " + miss_note if miss_note else ""), color=INK_2, fontsize=6.5,
             va="bottom", wrap=True)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out / "income_spread_states.png", dpi=150)
    plt.close(fig)

    pd.set_option("display.width", 220)
    cols_show = ["year", "tracts", "tracts_used", "heads_used", "heads_left_out", "mean_of_tract_means", "p10_sm",
                 "p25_sm", "p50_sm", "p75_sm", "p90_sm", "p90_p10", "p75_p25", "missing_states"]
    logger.info("\nNational (quantiles in minimum wages)\n%s", qt[qt.state == "BR"][cols_show].to_string(index=False))
    logger.info("Wrote %s", out)


if __name__ == "__main__":
    main()

"""
Sanity check of the T1 census data: age pyramids by sex, national and by state, for every census.

For each census of the config, the age-by-sex variables are found in the VARIABLES catalog: for each sex, a
list of sources {table, regex, optional min_age / max_age}; the regex runs on description_en and captures the
age band (lo, optional hi, optional open = "or older"). Where bands overlap (e.g. single years and an
"80 or older" total), the finest non-overlapping set is kept; the kept bands must cover every age from 0 to an
open top band, otherwise the run stops. Bands are then summed into the common `groups` of the config (every
census must provide them: a band may not straddle a group limit).

Values are summed per state (first two digits of setor_code); flagged cells (< 0: hidden, no data) count as
0 and are reported. The pyramid total is compared with the census population variable. The national pyramid
is the sum of the states that have data; states without any row in the age tables are left out and named in
the figure (e.g. a state missing from a table).

Outputs (output_folder):
  pyramids.csv                 year, state (BR = national), sex, group, persons, share of the population
  pyramid_checks.csv           per census and state: persons in the pyramid vs the population variable,
                               flagged cells, missing states
  pyramid_national.png         one pyramid per census (share of the population, male left, female right)
  pyramid_national_overlay.png the censuses overlaid as outlines on one pyramid
  pyramid_states_<year>.png    one pyramid per state for a census
  pyramid_states_overlay.png   one panel per state with the censuses overlaid
  log check_pyramids_<timestamp>.txt

Usage:
    python check_pyramids.py --config check_pyramids.local.json
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

REQUIRED_KEYS = ["output_folder", "groups", "censuses", "states"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
MALE = "#2a78d6"
FEMALE = "#eb6834"
YEARS = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]  # sequential blue, oldest light


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"check_pyramids_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("check_pyramids")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger


def group_label(g) -> str:
    return f"{g[0]}+" if g[1] is None else f"{g[0]}-{g[1]}"


def per_five(groups) -> np.ndarray:
    """Factor that puts a group on a 5-years-of-age scale (a 10-year group is halved; the open top group is kept)."""
    return np.array([1.0 if g[1] is None else 5 / (g[1] - g[0] + 1) for g in groups])


def find_bands(variables: pd.DataFrame, sources: list, year: int, sex: str) -> list:
    """[(table, variable, lo, hi)] (hi None = open) covering every age once, finest bands first."""
    found = []
    for s in sources:
        for r in variables[variables.table == s["table"]].itertuples():
            m = re.match(s["regex"], r.description_en or "")
            if not m:
                continue
            g = m.groupdict()
            lo = int(g["lo"])
            hi = None if g.get("open") else (int(g["hi"]) if g.get("hi") else lo)
            if lo < s.get("min_age", 0) or (hi if hi is not None else 999) > s.get("max_age", 999):
                continue
            found.append((s["table"], r.variable_name, lo, hi))
    width = lambda b: (b[3] if b[3] is not None else 999) - b[2]
    kept = []
    for b in sorted(found, key=lambda b: (width(b), b[2])):
        top = lambda x: x[3] if x[3] is not None else 999
        if all(top(b) < k[2] or b[2] > top(k) for k in kept):
            kept.append(b)
    kept.sort(key=lambda b: b[2])
    age = 0
    for b in kept:
        if b[2] != age:
            sys.exit(f"{year} {sex}: ages {age}-{b[2] - 1} not covered by the variables found")
        if b[3] is None:
            break
        age = b[3] + 1
    if not kept or kept[-1][3] is not None:
        sys.exit(f"{year} {sex}: no open top band (e.g. 80 or older) found")
    return kept


def band_to_group(bands: list, groups: list, year: int, sex: str) -> dict:
    """variable -> group index; a band must lie inside one group."""
    out = {}
    for table, var, lo, hi in bands:
        for i, (glo, ghi) in enumerate(groups):
            inside = lo >= glo and (ghi is None or (hi is not None and hi <= ghi))
            if inside:
                out[(table, var)] = i
                break
        else:
            sys.exit(f"{year} {sex}: band {lo}-{hi} ({table}.{var}) straddles the common groups")
    return out


def style(ax, ticks=True):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=7, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if not ticks:
        ax.set_yticklabels([])


def pct_formatter(x, _):
    return f"{abs(x):.0f}%"


def draw_pyramid(ax, m, f, labels, xmax, bar=True, color=None, lw=1.6, label=None, ms=4):
    y = np.arange(len(labels))
    if bar:
        ax.barh(y, -m, height=0.82, color=MALE, edgecolor=SURFACE, linewidth=0.6)
        ax.barh(y, f, height=0.82, color=FEMALE, edgecolor=SURFACE, linewidth=0.6)
    else:
        kw = dict(color=color, linewidth=lw, marker="o", markersize=ms, markeredgecolor=SURFACE, markeredgewidth=0.8)
        ax.plot(-m, y, label=label, **kw)
        ax.plot(f, y, **kw)
    ax.axvline(0, color=INK_2, linewidth=0.6)
    ax.set_xlim(-xmax, xmax)
    ax.set_yticks(y, labels)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(pct_formatter))


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    args = ap.parse_args()
    cfg = load_config(args.config)
    out = Path(cfg["output_folder"])
    logger = setup_logging(out)
    states = cfg["states"]
    groups = cfg["groups"]
    labels = [group_label(g) for g in groups]
    scale = per_five(groups)
    plot_labels = [l + ("*" if k != 1 else "") for l, k in zip(labels, scale)]
    scale_note = ("* " + ", ".join(l for l, k in zip(labels, scale) if k != 1) +
                  " drawn per 5 years of age (share divided by the group width / 5)") if (scale != 1).any() else ""
    rows, checks = [], []
    missing_by_year = {}

    for c in sorted(cfg["censuses"], key=lambda c: c["year"]):
        year = c["year"]
        folder = Path(c["folder"])
        variables = pd.read_csv(folder / c["variables"], sep=";", dtype=str)
        need = {}
        mapping = {}
        for sex in ("male", "female"):
            bands = find_bands(variables, c[sex], year, sex)
            mapping[sex] = band_to_group(bands, groups, year, sex)
            for table, var, _, _ in bands:
                need.setdefault(table, set()).add(var)
            logger.info("%d %-6s %d bands from %s, top %s", year, sex, len(bands),
                        sorted({b[0] for b in bands}), f"{bands[-1][2]}+")
        per_state = {sex: np.zeros((0, len(groups))) for sex in ("male", "female")}
        frames = {}
        flagged = 0
        for table, vars_ in need.items():
            path = folder / c["table_pattern"].format(NAME=table.upper())
            d = pd.read_csv(path, sep=";", usecols=["setor_code"] + sorted(vars_), dtype={"setor_code": str})
            flagged += int((d[sorted(vars_)] < 0).sum().sum())
            st = d["setor_code"].str[:2]
            frames[table] = d[sorted(vars_)].clip(lower=0).groupby(st).sum()
        present = sorted(set.union(*[set(f.index) for f in frames.values()]) & set(states))
        missing = sorted(set(states) - set(present))
        missing_by_year[year] = [states[s] for s in missing]
        mat = {}
        for sex in ("male", "female"):
            a = pd.DataFrame(0.0, index=present, columns=range(len(groups)))
            for (table, var), gi in mapping[sex].items():
                a[gi] += frames[table][var].reindex(present, fill_value=0)
            mat[sex] = a
        pc = c["population"]
        pop = pd.read_csv(folder / c["table_pattern"].format(NAME=pc["table"].upper()), sep=";",
                          usecols=["setor_code", pc["variable"]], dtype={"setor_code": str})
        pop = pop[pc["variable"]].clip(lower=0).groupby(pop["setor_code"].str[:2]).sum()
        for s in present + ["BR"]:
            m = mat["male"].sum() if s == "BR" else mat["male"].loc[s]
            f = mat["female"].sum() if s == "BR" else mat["female"].loc[s]
            total = m.sum() + f.sum()
            ref = pop.reindex(present).sum() if s == "BR" else pop.get(s, np.nan)
            checks.append(dict(year=year, state=states.get(s, s), persons_in_pyramid=int(total), population=int(ref),
                               difference=int(total - ref), difference_pct=round(100 * (total / ref - 1), 3),
                               male_share_pct=round(100 * m.sum() / total, 2),
                               flagged_cells=flagged if s == "BR" else None,
                               missing_states=" ".join(missing_by_year[year]) if s == "BR" else None))
            for sex, v in (("male", m), ("female", f)):
                for gi, n in v.items():
                    rows.append(dict(year=year, state=states.get(s, s), sex=sex, group=labels[gi], persons=int(n),
                                     share_pct=round(100 * n / total, 4)))
        logger.info("%d: %d states, missing %s, flagged cells %d", year, len(present), missing_by_year[year] or "none",
                    flagged)

    pyr = pd.DataFrame(rows)
    chk = pd.DataFrame(checks)
    pyr.to_csv(out / "pyramids.csv", sep=";", index=False)
    chk.to_csv(out / "pyramid_checks.csv", sep=";", index=False)
    years = sorted(pyr.year.unique())

    def shares(year, state):
        g = pyr[(pyr.year == year) & (pyr.state == state)]
        m = g[g.sex == "male"].set_index("group").share_pct.reindex(labels).to_numpy() * scale
        f = g[g.sex == "female"].set_index("group").share_pct.reindex(labels).to_numpy() * scale
        return m, f

    # --- national, one pyramid per census ------------------------------------------------------
    xmax = np.ceil(max(max(np.nanmax(v) for v in shares(y, "BR")) for y in years) + 0.5)
    fig, axes = plt.subplots(1, len(years), figsize=(3.2 * len(years), 4.6), facecolor=SURFACE, sharey=True)
    for ax, y in zip(axes, years):
        style(ax)
        m, f = shares(y, "BR")
        draw_pyramid(ax, m, f, plot_labels, xmax)
        r = chk[(chk.year == y) & (chk.state == "BR")].iloc[0]
        note = f"{r.persons_in_pyramid / 1e6:,.1f} M, men {r.male_share_pct:.1f}%"
        if missing_by_year[y]:
            note += f"\nwithout {', '.join(missing_by_year[y])} (no data)"
        ax.set_title(f"{y}\n", color=INK, fontsize=11, loc="left", fontweight="bold")
        ax.text(0, 1.01, note, transform=ax.transAxes, fontsize=8, color=INK_2, va="bottom")
    axes[0].set_ylabel("Age group", color=INK_2, fontsize=8)
    fig.legend(handles=[Patch(color=MALE, label="Male"), Patch(color=FEMALE, label="Female")], ncol=2, frameon=False,
               fontsize=8, loc="lower center")
    fig.suptitle("Brazil: population by sex and age group (share of the population)", color=INK, fontsize=12,
                 x=0.01, ha="left")
    fig.text(0.01, 0.01, scale_note, color=INK_2, fontsize=7)
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    fig.savefig(out / "pyramid_national.png", dpi=150)
    plt.close(fig)

    # --- national, censuses overlaid -----------------------------------------------------------
    fig, ax = plt.subplots(figsize=(8, 5.6), facecolor=SURFACE)
    style(ax)
    for y, col in zip(years, YEARS):
        m, f = shares(y, "BR")
        draw_pyramid(ax, m, f, plot_labels, xmax, bar=False, color=col, label=str(y), lw=2, ms=6)
    ax.set_xlabel("Male                                                                   Female", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.0), title="Census", title_fontsize=8)
    fig.text(0.01, 0.01, scale_note, color=INK_2, fontsize=7)
    miss = [f"{y}: {', '.join(v)}" for y, v in missing_by_year.items() if v]
    ax.set_title("Brazil: age structure by census (share of the population)" +
                 (f"\nstates without data left out: {'; '.join(miss)}" if miss else ""), color=INK, fontsize=10, loc="left")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out / "pyramid_national_overlay.png", dpi=150)
    plt.close(fig)

    # --- states, one figure per census ---------------------------------------------------------
    order = list(states.values())
    st_max = np.ceil(max(np.nanmax(v) for y in years for uf in states.values()
                         if not chk[(chk.year == y) & (chk.state == uf)].empty for v in shares(y, uf)) + 0.5)
    cols = 7
    nrows = -(-len(order) // cols)
    for y in years:
        fig, axes = plt.subplots(nrows, cols, figsize=(cols * 2.2, nrows * 2.5), facecolor=SURFACE, sharey=True)
        for i, (ax, uf) in enumerate(zip(axes.flat, order)):
            style(ax)
            r = chk[(chk.year == y) & (chk.state == uf)]
            if r.empty:
                ax.set_xticks([])
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color=INK_2, fontsize=9)
                ax.set_title(uf, color=INK, fontsize=9, loc="left", fontweight="bold")
                continue
            m, f = shares(y, uf)
            draw_pyramid(ax, m, f, plot_labels, st_max)
            ax.set_title(f"{uf}  {r.iloc[0].persons_in_pyramid / 1e6:,.2f} M", color=INK, fontsize=8, loc="left")
            if i % cols:
                ax.tick_params(axis="y", labelleft=False)
        for ax in list(axes.flat)[len(order):]:
            ax.axis("off")
        fig.legend(handles=[Patch(color=MALE, label="Male"), Patch(color=FEMALE, label="Female")], ncol=2,
                   frameon=False, fontsize=8, loc="lower right")
        fig.suptitle(f"Census {y}: population by sex and age group per state (share of the state population)",
                     color=INK, fontsize=12, x=0.01, ha="left")
        fig.text(0.01, 0.005, scale_note, color=INK_2, fontsize=7)
        fig.tight_layout(rect=(0, 0.02, 1, 0.97))
        fig.savefig(out / f"pyramid_states_{y}.png", dpi=130)
        plt.close(fig)

    # --- states, censuses overlaid -------------------------------------------------------------
    fig, axes = plt.subplots(nrows, cols, figsize=(cols * 2.2, nrows * 2.5), facecolor=SURFACE, sharey=True)
    for i, (ax, uf) in enumerate(zip(axes.flat, order)):
        style(ax)
        for y, col in zip(years, YEARS):
            if chk[(chk.year == y) & (chk.state == uf)].empty:
                continue
            m, f = shares(y, uf)
            draw_pyramid(ax, m, f, plot_labels, st_max, bar=False, color=col, lw=1.4, label=str(y), ms=0)
        lacking = [str(y) for y in years if chk[(chk.year == y) & (chk.state == uf)].empty]
        ax.set_title(uf + (f"  (no {', '.join(lacking)})" if lacking else ""), color=INK, fontsize=9, loc="left",
                     fontweight="bold")
        if i % cols:
            ax.tick_params(axis="y", labelleft=False)
    for ax in list(axes.flat)[len(order):]:
        ax.axis("off")
    fig.legend(handles=[Line2D([], [], color=c, linewidth=2, label=str(y)) for y, c in zip(years, YEARS)], ncol=4,
               frameon=False, fontsize=8, loc="lower right", title="Census (male left, female right)", title_fontsize=8)
    fig.suptitle("Age structure per state across censuses (share of the state population)", color=INK, fontsize=12,
                 x=0.01, ha="left")
    fig.text(0.01, 0.005, scale_note, color=INK_2, fontsize=7)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(out / "pyramid_states_overlay.png", dpi=130)
    plt.close(fig)

    pd.set_option("display.width", 200)
    logger.info("\nPyramid totals vs population (national)\n%s",
                chk[chk.state == "BR"].drop(columns="state").to_string(index=False))
    worst = chk[chk.state != "BR"].assign(a=lambda d: d.difference_pct.abs()).sort_values("a", ascending=False).head(8)
    logger.info("\nLargest state differences\n%s", worst.drop(columns=["a", "flagged_cells", "missing_states"]).to_string(index=False))
    logger.info("Wrote %s", out)


if __name__ == "__main__":
    main()

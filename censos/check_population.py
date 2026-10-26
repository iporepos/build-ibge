"""
Sanity check of the T1 census data: total resident population, national and by state, across censuses.

For each census of the config, the population variable of a T1 table is summed over its rows (tracts, or
municipalities for an alternative); the state is the first two digits of the key (setor_code or
municipio_code). Flagged values (< 0: -9999 no data, -1 hidden, -2 uncertain) are left out of the sums and
counted, so a total that is only a lower bound shows up. Each census may list alternative variables
(another table holding the same population) that are compared state by state, and a reference_total (the
population published by IBGE) that the national sum is compared with.

Outputs (output_folder):
  population_total.csv      year, variable, rows, flagged rows, population, IBGE total, difference
  population_by_state.csv   one row per state, the population of each census and the change between censuses
  population_alternatives.csv  per census and alternative: national totals and the largest state difference
  population_total.png      national population per census
  population_by_state.png   population per census, one panel per state
  log check_population_<timestamp>.txt

Usage:
    python check_population.py --config check_population.local.json
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
import pandas as pd

REQUIRED_KEYS = ["output_folder", "censuses", "states"]
SERIES = "#2a78d6"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"check_population_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("check_population")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger


def by_state(path: str, variable: str, key: str = "setor_code") -> tuple:
    """(population per state code, rows, flagged rows) of one variable of a T1 table."""
    df = pd.read_csv(path, sep=";", usecols=[key, variable], dtype={key: str})
    v = pd.to_numeric(df[variable], errors="raise")
    flagged = v < 0
    pop = v.where(~flagged, 0).groupby(df[key].str[:2]).sum()
    return pop, len(df), int(flagged.sum())


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_total(total: pd.DataFrame, path: Path):
    fig, ax = plt.subplots(figsize=(7, 4), facecolor=SURFACE)
    style(ax)
    y = total["population"] / 1e6
    ax.plot(total["year"], y, color=SERIES, linewidth=2, marker="o", markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2)
    for x, v in zip(total["year"], y):
        ax.annotate(f"{v:,.1f} M", (x, v), textcoords="offset points", xytext=(0, 10), ha="center",
                    fontsize=9, color=INK)
    ax.set_xticks(total["year"])
    ax.set_ylim(0, y.max() * 1.15)
    ax.set_ylabel("Resident population (millions)", color=INK_2, fontsize=9)
    ax.set_title("Brazil: resident population by census (sum of the T1 tracts)", color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_states(states: pd.DataFrame, years: list, path: Path):
    n = len(states)
    cols = 6
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.3, rows * 1.9), facecolor=SURFACE)
    for ax, (_, r) in zip(axes.flat, states.iterrows()):
        style(ax)
        y = r[[str(t) for t in years]].astype(float) / 1e6
        ax.plot(years, y, color=SERIES, linewidth=2, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1)
        ax.set_ylim(0, y.max() * 1.2)
        ax.set_xticks(years)
        ax.set_xticklabels([f"'{str(t)[2:]}" for t in years])
        ax.set_title(f"{r['uf']}  {y.iloc[-1]:,.2f} M", color=INK, fontsize=9, loc="left")
    for ax in list(axes.flat)[n:]:
        ax.axis("off")
    fig.suptitle("Resident population by state and census (millions; title: last census)", color=INK,
                 fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    args = ap.parse_args()
    cfg = load_config(args.config)
    out = Path(cfg["output_folder"])
    logger = setup_logging(out)
    states = cfg["states"]

    totals, per_state, alts = [], {}, []
    for c in sorted(cfg["censuses"], key=lambda c: c["year"]):
        pop, n, flagged = by_state(c["table"], c["variable"], c.get("key", "setor_code"))
        unknown = sorted(set(pop.index) - set(states))
        if unknown:
            logger.warning("%d: state codes not in the config: %s", c["year"], unknown)
        per_state[str(c["year"])] = pop
        ref = c.get("reference_total")
        total = int(pop.sum())
        totals.append(dict(year=c["year"], variable=c["label"], rows=n, rows_flagged=flagged, population=total,
                           ibge_total=ref, difference=None if ref is None else total - ref,
                           difference_pct=None if ref is None else round(100 * (total / ref - 1), 4)))
        for a in c.get("alternatives", []):
            apop, an, aflag = by_state(a["table"], a["variable"], a.get("key", "setor_code"))
            d = apop.reindex(pop.index.union(apop.index), fill_value=0) - pop.reindex(
                pop.index.union(apop.index), fill_value=0)
            worst = d.abs().idxmax()
            alts.append(dict(year=c["year"], variable=c["label"], alternative=a["label"], population=total,
                             alternative_population=int(apop.sum()), difference=int(apop.sum()) - total,
                             alternative_rows_flagged=aflag, states_differing=int((d != 0).sum()),
                             largest_state_difference=f"{states.get(worst, worst)} {int(d[worst]):+d}"))

    total = pd.DataFrame(totals)
    years = total["year"].tolist()
    st = pd.DataFrame(per_state).fillna(0).astype("int64")
    st.index.name = "unidade_code"
    st.insert(0, "uf", [states.get(k, k) for k in st.index])
    for a, b in zip(years, years[1:]):
        st[f"change_{a}_{b}_pct"] = (100 * (st[str(b)] / st[str(a)].where(st[str(a)] > 0) - 1)).round(2)
    st = st.reset_index()
    total.to_csv(out / "population_total.csv", sep=";", index=False)
    st.to_csv(out / "population_by_state.csv", sep=";", index=False)
    pd.DataFrame(alts).to_csv(out / "population_alternatives.csv", sep=";", index=False)
    plot_total(total, out / "population_total.png")
    plot_states(st, years, out / "population_by_state.png")

    pd.set_option("display.width", 200)
    logger.info("National total (flagged values left out of the sums)\n%s\n",
                total.drop(columns="variable").to_string(index=False))
    for r in totals:
        logger.info("  %d: %s", r["year"], r["variable"])
    if alts:
        logger.info("\nAlternative variables\n%s\n", pd.DataFrame(alts).drop(columns="variable").to_string(index=False))
    logger.info("\nBy state\n%s\n", st.to_string(index=False))
    logger.info("Wrote %s", out)


if __name__ == "__main__":
    main()

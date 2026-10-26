"""
Sanity check of the T1 census data: coverage of every table and layer by state.

For each census of the config, every T1 file matching `pattern` (CSV tables and GeoPackage layers) is read on
its key column only: the first of `keys` the file has (setor_code for the tract tables and layers,
municipio_code for municipal tables and microdata). The state is the first two digits of the key. Files with
none of the keys are skipped (catalogs, link tables keyed otherwise). For each file:
  * rows per state, and the states of the config that have no row
  * tract files only: tracts not in the reference (the `reference` layer or table of the census, e.g. setores)
    and reference tracts missing from the file, split by the census `population` variable into populated
    (population > 0), empty (0) and unknown (tract absent from the population table, e.g. geometry only)
A file is flagged when a state of the config has no row, or when it lacks more than `missing_share_flag` of
the populated tracts of some state. Files listed in the census `expected_partial` (with the reason) are
reported but not flagged. State codes in `ignore_state_codes` (e.g. NA of special areas) are not states.

Outputs (output_folder):
  coverage_summary.csv        one row per census and file: key, rows, states, missing states, set differences, flag
  coverage_by_state.csv       rows per census, file and state (long), with the reference rows and their ratio
  coverage_<year>.png         per census: tract files x states, share of the populated tracts present
  log check_coverage_<timestamp>.txt

Usage:
    python check_coverage.py --config check_coverage.local.json
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

REQUIRED_KEYS = ["output_folder", "censuses", "states"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
CRITICAL = "#d03b3b"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("keys", ["setor_code", "municipio_code"])
    cfg.setdefault("missing_share_flag", 0.0)
    cfg.setdefault("ignore_state_codes", ["NA"])
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"check_coverage_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("check_coverage")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger


def layer_name(path: Path) -> str:
    """Layer name from the T1 file name: IBGE_CENSO-2000_PESSOA01_BRASIL_2000.csv -> pessoa01."""
    m = re.match(r"^[^_]+_[^_]+_(.+)_[^_]+_[^_]+$", path.stem)
    return (m.group(1) if m else path.stem).lower()


def read_keys(path: Path, keys: list):
    """(key name, Series of key values) of a CSV or the first feature/attribute table of a GeoPackage."""
    if path.suffix == ".csv":
        with open(path, encoding="utf-8") as f:
            header = f.readline().rstrip("\n").split(";")
        key = next((k for k in keys if k in header), None)
        if key is None:
            return None, None
        t = pacsv.read_csv(path, parse_options=pacsv.ParseOptions(delimiter=";"),
                           convert_options=pacsv.ConvertOptions(include_columns=[key], column_types={key: pa.string()}))
        return key, t.column(key).to_pandas()
    if path.suffix == ".gpkg":
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table = con.execute("SELECT table_name FROM gpkg_contents ORDER BY data_type = 'features' DESC").fetchone()[0]
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            key = next((k for k in keys if k in cols), None)
            if key is None:
                return None, None
            vals = [r[0] for r in con.execute(f'SELECT "{key}" FROM "{table}"')]
        finally:
            con.close()
        return key, pd.Series(vals, dtype="object").astype(str)
    return None, None


def plot_census(year: int, mat: pd.DataFrame, states: dict, path: Path):
    """Heat map: tract files x states, share of the populated tracts present; no row in a state is marked in red."""
    share = mat.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(1.0 + 0.42 * mat.shape[1], 0.8 + 0.3 * mat.shape[0]), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    bounds = [0.0, 0.5, 0.9, 0.99, 0.999, 1.0, np.inf]
    idx = np.digitize(np.nan_to_num(share, nan=-1), bounds) - 1
    rgb = np.zeros(share.shape + (3,))
    cmap = [matplotlib.colors.to_rgb(c) for c in BLUES]
    for i in range(len(BLUES)):
        rgb[idx == i] = cmap[i]
    rgb[(share == 0) | np.isnan(share)] = matplotlib.colors.to_rgb(SURFACE)
    ax.imshow(rgb, aspect="auto")
    for (r, c), v in np.ndenumerate(share):
        if v == 0:
            ax.text(c, r, "0", ha="center", va="center", fontsize=7, color=CRITICAL, fontweight="bold")
    ax.set_xticks(range(mat.shape[1]), [states.get(s, s) for s in mat.columns], fontsize=7, color=INK_2)
    ax.set_yticks(range(mat.shape[0]), mat.index, fontsize=7, color=INK_2)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks(np.arange(-0.5, mat.shape[1]), minor=True)
    ax.set_yticks(np.arange(-0.5, mat.shape[0]), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="both", length=0)
    labels = ["< 50%", "50-90%", "90-99%", "99-99.9%", "99.9-<100%", "100%"]
    handles = [matplotlib.patches.Patch(color=c, label=l) for c, l in zip(BLUES, labels)]
    handles.append(matplotlib.lines.Line2D([], [], marker="$0$", color=CRITICAL, linestyle="", label="no row"))
    ax.legend(handles=handles, ncol=7, fontsize=7, frameon=False, loc="upper left", bbox_to_anchor=(0, -0.06 - 1.2 / mat.shape[0]))
    ax.set_title(f"Census {year}: populated tracts present in each tract table, by state (* expected partial)",
                 color=INK, fontsize=10, loc="left")
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
    all_states = set(states)
    ignore = set(cfg["ignore_state_codes"])
    summary, long = [], []

    for c in sorted(cfg["censuses"], key=lambda c: c["year"]):
        year = c["year"]
        partial = c.get("expected_partial", {})
        files = sorted(p for p in Path(c["folder"]).glob(c["pattern"]) if p.suffix in (".csv", ".gpkg"))
        data = {}
        for p in files:
            key, vals = read_keys(p, cfg["keys"])
            if key is None:
                logger.info("%d %-28s no key column, skipped", year, layer_name(p))
                continue
            data[layer_name(p)] = (key, vals)
        if c["reference"] not in data or data[c["reference"]][0] != "setor_code":
            sys.exit(f"{year}: reference {c['reference']} not found or not keyed by setor_code")
        pc = c["population"]
        pt = Path(c["folder"]) / next(p.name for p in files if layer_name(p) == pc["table"])
        pop = pd.read_csv(pt, sep=";", usecols=["setor_code", pc["variable"]], dtype={"setor_code": str})
        pop = pop.set_index("setor_code")[pc["variable"]]
        ref = pd.Index(sorted(set(data[c["reference"]][1])))
        status = pd.Series("unknown", index=ref)
        known = ref.intersection(pop.index)
        status[known] = np.where(pop.reindex(known) > 0, "populated", "empty")
        ref_st = ref.str[:2]
        populated = status == "populated"
        pop_by_state = pd.Series(populated.values, index=ref_st).groupby(level=0).sum()
        logger.info("%d reference %s: %d tracts, %d populated, %d empty, %d without population data", year,
                    c["reference"], len(ref), int(populated.sum()), int((status == "empty").sum()),
                    int((status == "unknown").sum()))
        mat = {}
        for name, (key, vals) in data.items():
            st = vals.str[:2].value_counts()
            st = st[~st.index.isin(ignore)]
            miss_states = sorted(all_states - set(st.index))
            extra_states = sorted(set(st.index) - all_states)
            row = dict(year=year, layer=name, key=key, rows=len(vals), distinct=vals.nunique(), states=len(st),
                       missing_states=" ".join(states[s] for s in miss_states), unknown_states=" ".join(extra_states))
            flag = bool(miss_states or extra_states)
            if key == "setor_code":
                present = ref.isin(set(vals))
                lack = status[~present]
                row.update(not_in_reference=int((~pd.Index(vals.unique()).isin(ref)).sum()),
                           missing_populated=int((lack == "populated").sum()), missing_empty=int((lack == "empty").sum()),
                           missing_unknown=int((lack == "unknown").sum()))
                got = pd.Series((present & populated.values), index=ref_st).groupby(level=0).sum()
                share = (got / pop_by_state.where(pop_by_state > 0)).reindex(sorted(all_states))
                worst = share.idxmin() if share.notna().any() else None
                row["worst_state"] = (f"{states.get(worst, worst)} {100 * share[worst]:.2f}% of populated tracts"
                                      if worst is not None and share[worst] < 1 else "")
                flag = flag or (worst is not None and 1 - share[worst] > cfg["missing_share_flag"])
                mat[name + (" *" if name in partial else "")] = share.rename(name)
            if name in partial:
                row["expected_partial"] = partial[name]
                flag = False
            row["flag"] = "FLAG" if flag else ""
            summary.append(row)
            for s, n in st.items():
                long.append(dict(year=year, layer=name, key=key, unidade_code=s, uf=states.get(s, s), rows=int(n)))
        m = pd.DataFrame(mat).T
        first = [n for n in m.index if n.split(" ")[0] == c["reference"]]
        order = first + sorted(n for n in m.index if n not in first)
        plot_census(year, m.loc[order], states, out / f"coverage_{year}.png")

    sm = pd.DataFrame(summary)
    for col in ("not_in_reference", "missing_populated", "missing_empty", "missing_unknown"):
        sm[col] = sm[col].astype("Int64")
    lg = pd.DataFrame(long)
    sm.to_csv(out / "coverage_summary.csv", sep=";", index=False)
    lg.to_csv(out / "coverage_by_state.csv", sep=";", index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    for year, g in sm.groupby("year"):
        logger.info("\n== %d\n%s", year, g.drop(columns=["year", "unknown_states", "distinct"]).astype(object).fillna("").to_string(index=False))
    flagged = sm[sm.flag == "FLAG"]
    logger.info("\n%d of %d files flagged: %s", len(flagged), len(sm),
                ", ".join(f"{r.year} {r.layer}" for r in flagged.itertuples()) or "none")
    logger.info("Wrote %s", out)


if __name__ == "__main__":
    main()

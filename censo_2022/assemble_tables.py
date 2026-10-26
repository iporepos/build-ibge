"""
Assemble IBGE Census 2022 sector-level tables from Tier 0 to Tier 1.

Reads the layers catalog (IBGE_CENSO-2022_LAYERS_X_X.csv), selects the non-geography /
non-auxiliary tables, and for each one writes a harmonized CSV:

  * key column        -> setor_code (15-character text)
  * V columns         -> upper-case V##### (quantitative variables)
  * other columns     -> text, renamed through config column_map (e.g. cd_regiao -> regiao_code)
  * decimal comma     -> decimal point (V columns and numeric text columns such as area_km2)
  * flags in V columns (same convention as the 2010 FIELDS table):
        empty -> no_data (-9999), "X" -> hidden (-1), "." -> uncertain (-2)
  * flags in text columns: empty or "." -> text_no_data ("NA")

Every run also writes flag-impact reports (per table and per variable).

Usage:
    python assemble_tables.py --config assemble_tables.local.json
    python assemble_tables.py --config assemble_tables.local.json --assess-only
    python assemble_tables.py --config assemble_tables.local.json --tables renda,corraca --overwrite
"""

import argparse
import csv
import glob
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["layers_csv", "input_folder", "output_folder", "report_folder", "sources"]

V_RE = re.compile(r"^[Vv]\d+$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
KEY_RE = re.compile(r"^\d{15}$")
DEC_COMMA_RE = re.compile(r"^-?\d+,\d+$")

# Upper edges of the prevalence bands (share of X). Index 0 is "none"; the band labels
# are built from these edges, the last band is everything above the last edge.
ROW_BANDS = [0.10, 0.50, 0.999999]   # sectors: share of the table's variables hidden
VAR_BANDS = [0.01, 0.05, 0.25, 0.50, 0.999999]  # variables: share of sectors hidden


def band(frac: float, edges: list) -> int:
    """Index (1..len(edges)+1) of the band containing frac > 0."""
    for i, e in enumerate(edges, 1):
        if frac <= e:
            return i
    return len(edges) + 1


def band_labels(edges: list) -> list:
    labels, lo = ["none"], 0
    for e in edges:
        hi = round(e * 100, 1)
        labels.append(f"{lo:g}-<100%" if hi >= 100 else f"{lo:g}-{hi:g}%")
        lo = hi
    labels.append("100%")
    return labels


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2022_{NAME}_BRASIL_2022.csv")
    cfg.setdefault("skip_themes", ["geography", "auxiliary"])
    cfg.setdefault("encodings", {})
    cfg.setdefault("text_no_data", "NA")
    cfg.setdefault("column_map", {})
    cfg.setdefault("key_name", "setor_code")
    cfg.setdefault("population_table", "basico")
    cfg.setdefault("population_variable", "V0001")
    flags = cfg.setdefault("flags", {})
    flags.setdefault("no_data", -9999)
    flags.setdefault("hidden", -1)
    flags.setdefault("uncertain", -2)
    flags.setdefault("hidden_token", "X")
    flags.setdefault("uncertain_token", ".")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"assemble_log_{timestamp}.txt"

    logger = logging.getLogger("censo2022_assemble")
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


def read_layers(path: str, skip_themes: list) -> list:
    """Return layer names (in catalog order) whose theme is not skipped."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return [r["name"] for r in rows if r["theme"] not in skip_themes]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def process_table(name: str, src: Path, dst, cfg: dict, pop: dict, logger):
    """
    Stream one T0 CSV, apply the harmonization and count flags.
    Writes to `dst` (a Path) unless it is None (assess-only).
    Returns (table_stats, variable_stats_rows).
    """
    fl = cfg["flags"]
    no_data, hidden, uncertain = str(fl["no_data"]), str(fl["hidden"]), str(fl["uncertain"])
    hid_tok, unc_tok = fl["hidden_token"], fl["uncertain_token"]
    text_nd = cfg["text_no_data"]
    enc = cfg["encodings"].get(name, "utf-8")

    cmap = cfg["column_map"]
    pop_var = cfg["population_variable"]
    is_pop_table = name == cfg["population_table"]

    with open(src, encoding=enc, newline="") as fin:
        reader = csv.reader(fin, delimiter=";")
        header = next(reader)

        new_header, vidx, tidx = [], [], []
        for i, h in enumerate(header):
            if i == 0:
                new_header.append(cfg["key_name"])
            elif V_RE.match(h):
                new_header.append("V" + h[1:])
                vidx.append(i)
            else:
                new_header.append(cmap.get(h.lower(), h.lower()))
                tidx.append(i)
        if len(set(new_header)) != len(new_header):
            raise ValueError(f"duplicate column names after mapping in {name}")
        unmapped = [header[i] for i in tidx if cmap and header[i].lower() not in cmap]
        if unmapped:
            logger.warning("  text columns kept as lower-case source names (not in column_map): %s",
                           unmapped)
        nv = len(vidx)
        cnt_x = [0] * nv
        cnt_dot = [0] * nv
        cnt_empty = [0] * nv
        cnt_text_nd = [0] * len(tidx)
        row_bands = [0] * (len(ROW_BANDS) + 2)
        cnt_bad = 0
        bad_examples = []
        bad_keys = 0
        rows = rows_any_x = rows_all_x = rows_any_dot = 0
        pop_total = pop_any_x = 0.0
        pop_col = new_header.index(pop_var) if (is_pop_table and pop_var in new_header) else None

        fout = writer = None
        if dst is not None:
            tmp = dst.with_name(dst.name + ".part")
            fout = open(tmp, "w", encoding="utf-8", newline="")
            writer = csv.writer(fout, delimiter=";", lineterminator="\n")
            writer.writerow(new_header)

        try:
            for row in reader:
                rows += 1
                key = row[0]
                if not KEY_RE.match(key):
                    bad_keys += 1
                x_in_row = dot_in_row = 0
                for j, i in enumerate(vidx):
                    v = row[i]
                    if v.isdigit():
                        continue
                    if v == "":
                        cnt_empty[j] += 1
                        row[i] = no_data
                    elif v == hid_tok:
                        cnt_x[j] += 1
                        x_in_row += 1
                        row[i] = hidden
                    elif v == unc_tok:
                        cnt_dot[j] += 1
                        dot_in_row += 1
                        row[i] = uncertain
                    else:
                        v2 = v.replace(",", ".")
                        if NUM_RE.match(v2):
                            row[i] = v2
                        else:
                            cnt_bad += 1
                            if len(bad_examples) < 5:
                                bad_examples.append((key, header[i], v))
                            row[i] = no_data
                for k, i in enumerate(tidx):
                    v = row[i]
                    if v == "" or v == unc_tok:
                        cnt_text_nd[k] += 1
                        row[i] = text_nd
                    elif "," in v and DEC_COMMA_RE.match(v):
                        row[i] = v.replace(",", ".")  # e.g. area_km2
                if x_in_row:
                    rows_any_x += 1
                    if x_in_row == nv:
                        rows_all_x += 1
                    row_bands[band(x_in_row / nv, ROW_BANDS)] += 1
                else:
                    row_bands[0] += 1
                if dot_in_row:
                    rows_any_dot += 1
                if pop_col is not None:
                    try:
                        pop[key] = float(row[pop_col])
                    except ValueError:
                        pass
                p = pop.get(key)
                if p is not None and p >= 0:
                    pop_total += p
                    if x_in_row:
                        pop_any_x += p
                if writer:
                    writer.writerow(row)
        finally:
            if fout:
                fout.close()

    if dst is not None:
        tmp.replace(dst)

    cells = rows * nv
    t = dict(
        table=name, rows=rows, v_cols=nv, cells=cells,
        x_cells=sum(cnt_x), dot_cells=sum(cnt_dot), empty_cells=sum(cnt_empty),
        x_pct=round(100 * sum(cnt_x) / cells, 3) if cells else 0,
        dot_pct=round(100 * sum(cnt_dot) / cells, 4) if cells else 0,
        empty_pct=round(100 * sum(cnt_empty) / cells, 4) if cells else 0,
        rows_any_x=rows_any_x,
        rows_any_x_pct=round(100 * rows_any_x / rows, 2) if rows else 0,
        rows_all_x=rows_all_x, rows_any_dot=rows_any_dot,
        cols_all_x=sum(1 for c in cnt_x if c == rows and rows),
        cols_gt50_x=sum(1 for c in cnt_x if rows and c / rows > 0.5),
        pop_pct_in_rows_with_x=0.0,
        bad_values=cnt_bad, bad_keys=bad_keys,
        text_cols=len(tidx), text_no_data_cells=sum(cnt_text_nd),
    )
    # population share is filled later by the caller (needs the final pop total)
    t["_pop_any_x"] = pop_any_x
    t["_pop_total"] = pop_total
    t["_text_nd"] = {new_header[i]: cnt_text_nd[k] for k, i in enumerate(tidx) if cnt_text_nd[k]}
    # prevalence distributions: variables by their X share, sectors by their X share
    var_bands = [0] * (len(VAR_BANDS) + 2)
    for c in cnt_x:
        var_bands[0 if c == 0 else band(c / rows, VAR_BANDS)] += 1
    t["_var_bands"] = var_bands
    t["_row_bands"] = row_bands
    if cnt_bad:
        logger.warning("  %d unexpected values in V columns, e.g. %s", cnt_bad, bad_examples)
    if bad_keys:
        logger.warning("  %d keys are not 15 digits", bad_keys)

    v_rows = [
        dict(table=name, variable=new_header[i], x=cnt_x[j], dot=cnt_dot[j], empty=cnt_empty[j],
             x_pct=round(100 * cnt_x[j] / rows, 3) if rows else 0)
        for j, i in enumerate(vidx)
    ]
    return t, v_rows


def log_assessment(logger, t_rows: list, v_rows: list):
    """Write the flag-impact assessment into the log."""
    L = logger.info
    L("=" * 60)
    L("FLAG ASSESSMENT  (X = hidden, '.' = uncertain, empty = no data)")
    L("=" * 60)
    L("Prevalence of each flag per table (share of V cells)")
    L("%-20s %9s %6s %12s %9s %9s %9s %9s %8s", "table", "rows", "Vcols", "cells",
      "X %", "'.' %", "empty %", "rowsX %", "popX %")
    for t in t_rows:
        L("%-20s %9d %6d %12d %9.3f %9.4f %9.4f %9.2f %8.2f", t["table"], t["rows"], t["v_cols"],
          t["cells"], t["x_pct"], t["dot_pct"], t["empty_pct"], t["rows_any_x_pct"],
          t["pop_pct_in_rows_with_x"])
    cells = sum(t["cells"] for t in t_rows)
    x = sum(t["x_cells"] for t in t_rows)
    L("-" * 60)
    L("TOTAL cells=%d  X=%d (%.3f%%)  '.'=%d  empty=%d", cells, x,
      100 * x / cells if cells else 0, sum(t["dot_cells"] for t in t_rows),
      sum(t["empty_cells"] for t in t_rows))
    L("rowsX % = sectors with at least one X; popX % = share of the 2022 population "
      "(basico V0001) living in those sectors")

    L("-" * 60)
    L("Variables by X prevalence (number of variables whose X share of sectors is in the band)")
    vl = band_labels(VAR_BANDS)
    L("%-20s " % "table" + " ".join("%12s" % x for x in vl))
    for t in t_rows:
        L("%-20s " % t["table"] + " ".join("%12d" % n for n in t["_var_bands"]))
    L("-" * 60)
    L("Sectors by X prevalence (number of sectors whose X share of the table's variables is in the band)")
    rl = band_labels(ROW_BANDS)
    L("%-20s " % "table" + " ".join("%12s" % x for x in rl))
    for t in t_rows:
        L("%-20s " % t["table"] + " ".join("%12d" % n for n in t["_row_bands"]))
    L("-" * 60)
    L("Fully hidden: sectors with all variables X / variables X in every sector / variables >50% X")
    for t in t_rows:
        if t["rows_all_x"] or t["cols_all_x"] or t["cols_gt50_x"]:
            L("  %-20s sectors_all_X=%d  vars_all_X=%d  vars_gt50%%_X=%d",
              t["table"], t["rows_all_x"], t["cols_all_x"], t["cols_gt50_x"])

    L("-" * 60)
    L("Top 15 variables by X share")
    for v in sorted(v_rows, key=lambda r: -r["x_pct"])[:15]:
        L("  %-20s %-8s X=%d (%.2f%%)", v["table"], v["variable"], v["x"], v["x_pct"])

    L("-" * 60)
    L("Variables with '.' (uncertain) or empty cells")
    flagged = [v for v in v_rows if v["dot"] or v["empty"]]
    for v in flagged:
        L("  %-20s %-8s '.'=%d  empty=%d", v["table"], v["variable"], v["dot"], v["empty"])
    if not flagged:
        L("  none")

    L("-" * 60)
    L("Text columns converted to the text no-data value (empty or '.')")
    for t in t_rows:
        for col, n in sorted(t["_text_nd"].items(), key=lambda kv: -kv[1]):
            L("  %-20s %-12s %d", t["table"], col, n)
    L("=" * 60)


def write_csv(path: Path, rows: list, fields: list):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--tables", help="Comma-separated layer names to process (default: all)")
    parser.add_argument("--assess-only", action="store_true",
                        help="Only count flags and write reports; do not write T1 tables")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing output tables")
    args = parser.parse_args()

    cfg = load_config(args.config)
    report_folder = Path(cfg["report_folder"])
    logger = setup_logging(report_folder)

    layers = read_layers(cfg["layers_csv"], cfg["skip_themes"])
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",")]
        unknown = [t for t in wanted if t not in layers]
        if unknown:
            sys.exit(f"Tables not in scope of the layers catalog: {unknown}")
        layers = [t for t in layers if t in wanted]
    # the population table must run first (population weights for the impact report)
    layers.sort(key=lambda n: n != cfg["population_table"])

    no_source = [n for n in layers if n not in cfg["sources"]]
    if no_source:
        sys.exit(f"No source defined in config for tables: {no_source}")

    input_folder = Path(cfg["input_folder"])
    output_folder = Path(cfg["output_folder"])
    if not input_folder.is_dir():
        sys.exit(f"Input folder does not exist: {input_folder}")

    logger.info("=" * 60)
    logger.info("IBGE Censo 2022 — T0 -> T1 table assembly%s",
                " (assess only)" if args.assess_only else "")
    logger.info("Tables: %s", ", ".join(layers))
    logger.info("=" * 60)

    if not args.assess_only:
        output_folder.mkdir(parents=True, exist_ok=True)

    pop: dict = {}
    t_rows, v_rows_all, failed = [], [], []
    for n, name in enumerate(layers, 1):
        matches = sorted(glob.glob(str(input_folder / cfg["sources"][name])))
        if len(matches) != 1:
            logger.error("[%d/%d] %s: source pattern matched %d files — skipped",
                         n, len(layers), name, len(matches))
            failed.append(name)
            continue
        src = Path(matches[0])
        dst = None
        if not args.assess_only:
            dst = output_folder / cfg["name_pattern"].format(NAME=name.upper())
            if dst.exists() and not args.overwrite:
                logger.info("[%d/%d] %s: SKIP (output exists, use --overwrite)", n, len(layers), name)
                continue
        logger.info("[%d/%d] %s  <-  %s", n, len(layers), name, src.name)
        try:
            t, v = process_table(name, src, dst, cfg, pop, logger)
        except Exception as exc:
            logger.error("  FAILED: %s", exc)
            failed.append(name)
            continue
        t_rows.append(t)
        v_rows_all.extend(v)
        logger.info("  rows=%d  V=%d  X=%.3f%%  '.'=%d  empty=%d  rows_with_X=%.1f%%",
                    t["rows"], t["v_cols"], t["x_pct"], t["dot_cells"], t["empty_cells"],
                    t["rows_any_x_pct"])

    for t in t_rows:
        tot = t.pop("_pop_total")
        anyx = t.pop("_pop_any_x")
        t["pop_pct_in_rows_with_x"] = round(100 * anyx / tot, 2) if tot else 0
    if t_rows:
        log_assessment(logger, t_rows, v_rows_all)
    for t in t_rows:
        t.pop("_text_nd")
        for key, edges in (("_var_bands", VAR_BANDS), ("_row_bands", ROW_BANDS)):
            prefix = "vars_x_" if key == "_var_bands" else "rows_x_"
            for lab, n in zip(band_labels(edges), t.pop(key)):
                t[prefix + lab.replace("%", "pct").replace("<", "lt").replace("-", "_")] = n

    if t_rows:
        write_csv(report_folder / "flags_by_table.csv", t_rows, list(t_rows[0].keys()))
        write_csv(report_folder / "flags_by_variable.csv", v_rows_all,
                  ["table", "variable", "x", "dot", "empty", "x_pct"])
        logger.info("Reports written to %s", report_folder)

    logger.info("=" * 60)
    logger.info("DONE — %d processed, %d failed", len(t_rows), len(failed))
    if failed:
        logger.info("Failed: %s", ", ".join(failed))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

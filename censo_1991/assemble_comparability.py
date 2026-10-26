"""
Assemble the IBGE tract comparability tables 2022 -> 1991 into the T1 link table `comparabilidade-2022`.

Source: SRC/tabelas_de_comparabilidade/Comparabilidade_2022_1991_<UF>.csv (IBGE, sent on request;
RS so far, more UFs as they arrive): SETOR_2022;SETOR_1991, one row per pair, many-to-many. Every
file matching the glob is read, so a new UF only needs a re-run.
Output (T1): IBGE_CENSO-1991_COMPARABILIDADE-2022_BRASIL_1991.csv
  setor_code_2022; setor_code_1991   (the year in the name; setor_code_1991 = setor_code of the tract tables)
Checks (the run stops, nothing is written, on failure): header, 15-digit codes, each UF in one file.
Findings to CONSISTENCY (layer comparabilidade-2022): exact duplicate pairs (dropped), 1991 codes of
the table absent from the tract tables (kept), and per municipality of a covered UF the tracts of
basico missing from the table.

Usage:
    python assemble_comparability.py --config assemble_comparability.local.json [--overwrite]
"""

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

from assemble_geography import merge_consistency

REQUIRED_KEYS = ["source_folder", "basico", "output_folder", "report_folder"]
LAYER = "comparabilidade-2022"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("source_glob", "Comparabilidade_2022_1991_*.csv")
    cfg.setdefault("source_encoding", "utf-8-sig")
    cfg.setdefault("columns", {"SETOR_2022": "setor_code_2022", "SETOR_1991": "setor_code_1991"})
    cfg.setdefault("tract_column", "setor_code_1991")
    if cfg["tract_column"] not in cfg["columns"].values():
        sys.exit(f"tract_column {cfg['tract_column']} is not an output of columns")
    cfg.setdefault("output_name", "IBGE_CENSO-1991_COMPARABILIDADE-2022_BRASIL_1991.csv")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"comparability_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_comparability")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    out_dir = Path(cfg["output_folder"])
    out_path = out_dir / cfg["output_name"]
    if out_path.exists() and not args.overwrite:
        sys.exit(f"{out_path} exists (use --overwrite)")

    k91 = cfg["tract_column"]
    k22 = next(c for c in cfg["columns"].values() if c != k91)
    files = sorted(Path(cfg["source_folder"]).glob(cfg["source_glob"]))
    if not files:
        sys.exit(f"No file matches {cfg['source_glob']} in {cfg['source_folder']}")
    frames, cons, ufs = [], [], {}
    for f in files:
        d = pd.read_csv(f, sep=";", dtype=str, encoding=cfg["source_encoding"])
        d.columns = [c.strip() for c in d.columns]
        if sorted(d.columns) != sorted(cfg["columns"]):
            sys.exit(f"{f.name}: columns {list(d.columns)}, expected {list(cfg['columns'])}")
        d = d.rename(columns=cfg["columns"])[list(cfg["columns"].values())].apply(lambda s: s.str.strip())
        for c in d.columns:
            bad = ~d[c].str.fullmatch(r"\d{15}")
            if bad.any():
                sys.exit(f"{f.name}: {int(bad.sum())} values of {c} are not 15 digits, e.g. {d.loc[bad, c].head(3).tolist()}")
        for u in sorted(set(d[k91].str[:2])):
            if u in ufs:
                sys.exit(f"UF {u} in {ufs[u]} and {f.name}")
            ufs[u] = f.name
        dup = d.duplicated()
        for a, b in d[dup].itertuples(index=False):
            cons.append(dict(layer=LAYER, code=b, issue="duplicate_pair", value=f"{f.name}: {a}", action="dropped",
                             note="pair listed twice"))
        d = d[~dup]
        logger.info("  %s: %d pairs, %d tracts %s, %d tracts 1991", f.name, len(d), d[k22].nunique(), k22,
                    d[k91].nunique())
        frames.append(d)
    pairs = pd.concat(frames, ignore_index=True).sort_values([k91, k22])

    basico = pd.read_csv(cfg["basico"], sep=";", dtype=str, usecols=["setor_code", "municipio_code"])
    known = set(basico.setor_code)
    absent = sorted(set(pairs[k91]) - known)
    for code in absent:
        n = int((pairs[k91] == code).sum())
        cons.append(dict(layer=LAYER, code=code, issue="tract_not_in_tract_tables", value=f"{n} pairs", action="kept",
                         note="1991 code of the IBGE table absent from the DVD tract tables (e.g. tract without data)"))
    covered = basico[basico.setor_code.str[:2].isin(ufs)]
    lacking = covered[~covered.setor_code.isin(set(pairs[k91]))]
    for mun, g in lacking.groupby("municipio_code"):
        cons.append(dict(layer=LAYER, code=mun, issue="tracts_missing_in_table", value=str(len(g)), action="none",
                         note=f"tracts of the municipality absent from the IBGE table, e.g. {', '.join(g.setor_code.head(3))}"))
    logger.info("UFs covered: %s; 1991 codes not in the tract tables: %d; tracts of covered UFs missing from the "
                "table: %d of %d", sorted(ufs), len(absent), len(lacking), len(covered))
    per1991 = pairs.groupby(k91)[k22].nunique()
    per2022 = pairs.groupby(k22)[k91].nunique()
    logger.info("Many-to-many: %d of %d tracts 1991 map to >1 tract 2022; %d of %d tracts 2022 map to >1 tract 1991",
                int((per1991 > 1).sum()), len(per1991), int((per2022 > 1).sum()), len(per2022))

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    pairs.to_csv(tmp, sep=";", index=False, lineterminator="\n")
    tmp.replace(out_path)
    logger.info("Wrote %s (%d pairs)", out_path, len(pairs))
    cons_path = out_dir / cfg["consistency_name"]
    kept = merge_consistency(cons_path, cons, (LAYER,))
    logger.info("Consistency: %d rows (+%d kept from other layers) -> %s %s", len(cons), kept, cons_path,
                dict(Counter(r["issue"] for r in cons)))


if __name__ == "__main__":
    main()

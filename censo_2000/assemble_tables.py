"""
Assemble IBGE Census 2000 sector-level tables from Tier 0 to Tier 1.

T0 is the "Agregados por setores censitários dos resultados do universo - 2a edição": one folder
per state (SP split in SP1/SP2) with 21 MS-Excel 97 workbooks each (Basico_UF.xls, Domicilio_UF.xls,
...). Each T1 table is the concatenation of the state workbooks of one kind, written as
IBGE_CENSO-2000_<NAME>_BRASIL_2000.csv (';' separated, UTF-8):

  * key column        -> setor_code (15 digits, unique, checked)
  * V columns         -> V0001..V3216 kept; basico Var01..Var14 -> V01..V14 (config basico_rename)
  * Situacao / Tipo_do_setor of the non-basico tables are checked against basico and dropped
    (basico leads the sector attributes)
  * basico text columns renamed through config column_map; situacao_type (Urbana/Rural) derived
    from situacao_code; placeholder names ("Não existe / não identificado", "Não pertence") -> NA,
    and for the columns in placeholder_code_columns the code -> NA too
  * flags in V columns. IBGE left a cell BLANK when it omitted the value to protect the respondent
    (documentation section 4), the same meaning as "X" in 2010/2022:
        blank -> hidden (-1)
        blank -> 0 when a zero rule applies: the sector has nothing to count, e.g. population
              (morador V0237) == 0, so 0 is the actual value (config zero_rules)
        other non-numeric -> no_data (-9999), logged (none expected)

Every run writes reports (flags per table / per variable) to report_folder.

Usage:
    python assemble_tables.py --config assemble_tables.local.json
    python assemble_tables.py --config assemble_tables.local.json --assess-only
    python assemble_tables.py --config assemble_tables.local.json --tables basico,morador --overwrite
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder", "sources"]

V_RE = re.compile(r"^V\d+$")
KEY_RE = r"^\d{15}$"
NUM_RE = r"^-?\d+(\.\d+)?$"
INT_FLOAT_RE = r"^(-?\d+)\.0+$"


# ---------------------------------------------------------------------------
# Configuration / logging
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2000_{NAME}_BRASIL_2000.csv")
    cfg.setdefault("key_name", "setor_code")
    cfg.setdefault("source_key", "Cod_setor")
    cfg.setdefault("check_columns", {"Situacao": "situacao_code", "Tipo_do_setor": "tipo_code"})
    cfg.setdefault("text_no_data", "NA")
    cfg.setdefault("column_map", {})
    cfg.setdefault("basico_table", "basico")
    cfg.setdefault("basico_rename", {})
    cfg.setdefault("situacao_type", {})
    cfg.setdefault("placeholder_names", [])
    cfg.setdefault("placeholder_code_columns", {})
    cfg.setdefault("zero_rules", [])
    cfg.setdefault("workers", os.cpu_count())
    flags = cfg.setdefault("flags", {})
    flags.setdefault("no_data", -9999)
    flags.setdefault("hidden", -1)
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"assemble_log_{timestamp}.txt"

    logger = logging.getLogger("censo2000_assemble")
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


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def find_sources(input_folder: Path, prefix: str) -> list:
    """One workbook per state folder: <prefix>_<UF>.xls (case-insensitive)."""
    pat = re.compile(rf"^{re.escape(prefix)}_[A-Z0-9]+\.xls$", re.I)
    found = []
    for d in sorted(p for p in input_folder.iterdir() if p.is_dir()):
        m = [f for f in d.iterdir() if pat.match(f.name)]
        if len(m) != 1:
            raise ValueError(f"{d.name}: expected 1 '{prefix}_UF.xls', found {len(m)}")
        found.append(m[0])
    return found


def read_workbook(path: Path) -> pd.DataFrame:
    """First sheet, every cell as text (None for blank), header stripped."""
    df = pd.read_excel(path, engine="calamine", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def read_table(files: list, workers: int) -> pd.DataFrame:
    with ProcessPoolExecutor(min(workers, len(files))) as ex:
        parts = list(ex.map(read_workbook, files))
    headers = {tuple(p.columns) for p in parts}
    if len(headers) != 1:
        raise ValueError(f"headers differ between state workbooks: {len(headers)} variants")
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def zero_mask(cfg: dict, name: str, refs: dict, keys: pd.Series):
    """Rows (bool array) where blanks become 0, and the rule label per row."""
    mask = np.zeros(len(keys), dtype=bool)
    for rule in cfg["zero_rules"]:
        tables = rule["tables"]
        if tables != "*" and name not in tables:
            continue
        ref = refs[rule["reference"]]
        mask |= keys.map(ref).eq(0).to_numpy()
    return mask


def process_table(name: str, df: pd.DataFrame, cfg: dict, basico_attr, refs: dict, logger):
    """Harmonize one concatenated table in place. Returns (df, table_stats, variable_rows)."""
    key = cfg["key_name"]
    fl = cfg["flags"]
    hidden, no_data = str(fl["hidden"]), str(fl["no_data"])
    is_basico = name == cfg["basico_table"]

    df = df.rename(columns={cfg["source_key"]: key})
    keys = df[key].str.strip()
    df[key] = keys
    bad_keys = int((~keys.str.match(KEY_RE)).sum())
    dup_keys = int(keys.duplicated().sum())
    if bad_keys or dup_keys:
        raise ValueError(f"{bad_keys} keys not 15 digits, {dup_keys} duplicated keys")

    if is_basico:
        df = df.rename(columns=cfg["basico_rename"])
    vcols = [c for c in df.columns if V_RE.match(c)]
    tcols = [c for c in df.columns if c != key and c not in vcols]

    # non-basico: check the repeated sector attributes against basico, then drop them
    mism = {}
    if not is_basico:
        for src_col, b_col in cfg["check_columns"].items():
            if src_col not in df.columns:
                continue
            if basico_attr is not None:
                other = keys.map(basico_attr[b_col])
                n = int((df[src_col].str.strip() != other).sum())
                mism[src_col] = n
                if n:
                    logger.warning("  %s differs from basico in %d sectors", src_col, n)
            df = df.drop(columns=src_col)
        extra = [c for c in tcols if c not in cfg["check_columns"]]
        if extra:
            logger.warning("  unexpected non-V columns kept: %s", extra)
    else:
        df = clean_basico_text(df, tcols, cfg, logger)

    # V columns: blank -> hidden / 0, integer-looking floats -> int, garbage -> no_data
    zmask = zero_mask(cfg, name, refs, keys)
    n_hidden = np.zeros(len(vcols), dtype=np.int64)
    n_zero = np.zeros(len(vcols), dtype=np.int64)
    n_bad = 0
    bad_examples = []
    row_hidden = np.zeros(len(df), dtype=np.int64)
    out = {}
    for j, c in enumerate(vcols):
        s = df[c].str.strip()
        blank = (s.isna() | s.eq("")).to_numpy()
        zb = blank & zmask
        hb = blank & ~zmask
        n_zero[j] = zb.sum()
        n_hidden[j] = hb.sum()
        row_hidden += hb
        s = s.str.replace(INT_FLOAT_RE, r"\1", regex=True)
        bad = ~blank & ~s.str.match(NUM_RE, na=False).to_numpy()
        if bad.any():
            n_bad += int(bad.sum())
            bad_examples += list(zip(keys[bad][:3], [c] * 3, s[bad][:3]))
            s = s.mask(bad, no_data)
        s = s.mask(hb, hidden).mask(zb, "0")
        out[c] = s
    df = pd.concat([df.drop(columns=vcols), pd.DataFrame(out, index=df.index)], axis=1)
    if n_bad:
        logger.warning("  %d unexpected values in V columns -> %s, e.g. %s", n_bad, no_data,
                       bad_examples[:5])

    nv = len(vcols)
    rows = len(df)
    cells = rows * nv
    t = dict(table=name, rows=rows, v_cols=nv, cells=cells,
             hidden_cells=int(n_hidden.sum()), zero_filled_cells=int(n_zero.sum()),
             hidden_pct=round(100 * n_hidden.sum() / cells, 3) if cells else 0,
             rows_any_hidden=int((row_hidden > 0).sum()),
             rows_all_hidden=int((row_hidden == nv).sum()) if nv else 0,
             rows_zero_rule=int(zmask.sum()),
             bad_values=n_bad, **{f"mismatch_{k}": v for k, v in mism.items()})
    v_rows = [dict(table=name, variable=c, hidden=int(n_hidden[j]), zero_filled=int(n_zero[j]),
                   hidden_pct=round(100 * n_hidden[j] / rows, 3) if rows else 0)
              for j, c in enumerate(vcols)]
    return df, t, v_rows


def clean_basico_text(df: pd.DataFrame, tcols: list, cfg: dict, logger) -> pd.DataFrame:
    nd = cfg["text_no_data"]
    for c in tcols:
        df[c] = df[c].str.strip().str.replace(r"\s+", " ", regex=True)
    # placeholder names -> NA (and the matching placeholder code, where configured)
    placeholders = set(cfg["placeholder_names"])
    for c in tcols:
        if not c.startswith("Nome"):
            continue
        ph = df[c].isin(placeholders)
        if ph.any():
            logger.info("  %s: %d placeholder names -> %s", c, int(ph.sum()), nd)
            code_col = cfg["placeholder_code_columns"].get(c)
            if code_col:
                df.loc[ph, code_col] = nd
            df.loc[ph, c] = nd
    df = df.fillna({c: nd for c in tcols}).replace({c: {"": nd} for c in tcols})
    unmapped = [c for c in tcols if c not in cfg["column_map"]]
    if unmapped:
        logger.warning("  text columns not in column_map (kept as is): %s", unmapped)
    df = df.rename(columns=cfg["column_map"])
    st = cfg["situacao_type"]
    if st and "situacao_code" in df.columns:
        stype = df["situacao_code"].map(st).fillna(nd)
        df.insert(df.columns.get_loc("situacao_code"), "situacao_type", stype)
    return df


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
    parser.add_argument("--tables", help="Comma-separated table names to process (default: all)")
    parser.add_argument("--assess-only", action="store_true",
                        help="Only count flags and write reports; do not write T1 tables")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing output tables")
    args = parser.parse_args()

    cfg = load_config(args.config)
    report_folder = Path(cfg["report_folder"])
    logger = setup_logging(report_folder)
    input_folder = Path(cfg["input_folder"])
    output_folder = Path(cfg["output_folder"])
    if not input_folder.is_dir():
        sys.exit(f"Input folder does not exist: {input_folder}")

    names = list(cfg["sources"])
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",")]
        unknown = [t for t in wanted if t not in names]
        if unknown:
            sys.exit(f"Tables not in config sources: {unknown}")
        names = [n for n in names if n in wanted]

    logger.info("=" * 60)
    logger.info("IBGE Censo 2000 — T0 -> T1 table assembly%s",
                " (assess only)" if args.assess_only else "")
    logger.info("Tables: %s", ", ".join(names))
    logger.info("=" * 60)
    if not args.assess_only:
        output_folder.mkdir(parents=True, exist_ok=True)

    # zero-rule references and basico attributes are always read from source (cheap, and the
    # rules must hold also when only some tables are processed)
    refs = {}
    for rule in cfg["zero_rules"]:
        tname, var = rule["reference"].split(".")
        prefix = cfg["sources"][tname]
        df = read_table(find_sources(input_folder, prefix), cfg["workers"])
        refs[rule["reference"]] = pd.to_numeric(
            df.set_index(df[cfg["source_key"]].str.strip())[var])
        logger.info("zero rule %s: %d sectors with value 0 (tables %s)", rule["reference"],
                    int(refs[rule["reference"]].eq(0).sum()), rule["tables"])
    b_files = find_sources(input_folder, cfg["sources"][cfg["basico_table"]])
    b = read_table(b_files, cfg["workers"])
    basico_attr = pd.DataFrame({
        dst: b[src].str.strip().to_numpy() for src, dst in cfg["check_columns"].items()},
        index=b[cfg["source_key"]].str.strip())
    del b

    t_rows, v_rows_all, failed = [], [], []
    for n, name in enumerate(names, 1):
        dst = output_folder / cfg["name_pattern"].format(NAME=name.upper())
        if not args.assess_only and dst.exists() and not args.overwrite:
            logger.info("[%d/%d] %s: SKIP (output exists, use --overwrite)", n, len(names), name)
            continue
        try:
            files = find_sources(input_folder, cfg["sources"][name])
            logger.info("[%d/%d] %s  <-  %d workbooks '%s_UF.xls'", n, len(names), name,
                        len(files), cfg["sources"][name])
            df = read_table(files, cfg["workers"])
            df, t, v = process_table(name, df, cfg, basico_attr, refs, logger)
        except Exception as exc:
            logger.error("  FAILED: %s", exc)
            failed.append(name)
            continue
        if not args.assess_only:
            tmp = dst.with_name(dst.name + ".part")
            df.to_csv(tmp, sep=";", index=False, lineterminator="\n", encoding="utf-8")
            tmp.replace(dst)
        t_rows.append(t)
        v_rows_all.extend(v)
        logger.info("  rows=%d  V=%d  hidden=%d (%.3f%%)  zero-filled=%d  rows_with_hidden=%d",
                    t["rows"], t["v_cols"], t["hidden_cells"], t["hidden_pct"],
                    t["zero_filled_cells"], t["rows_any_hidden"])

    if t_rows:
        fields = []
        for t in t_rows:
            fields += [k for k in t if k not in fields]
        write_csv(report_folder / "flags_by_table.csv", t_rows, fields)
        write_csv(report_folder / "flags_by_variable.csv", v_rows_all,
                  ["table", "variable", "hidden", "zero_filled", "hidden_pct"])
        logger.info("Reports written to %s", report_folder)
    logger.info("=" * 60)
    logger.info("DONE — %d processed, %d failed", len(t_rows), len(failed))
    if failed:
        logger.info("Failed: %s", ", ".join(failed))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

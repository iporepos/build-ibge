"""
Harmonize the IBGE Census 2010 T1 sector tables: flags, sector keys and malformed rows.

The 2010 T1 CSVs (one table per file, `setor_code;V001;V002;...`) came out of the first
harmonization with several defects. This script reads them and writes clean copies to a different
folder (the input is never touched). Each table is read whole with pyarrow (all columns as text)
and processed column-wise with pyarrow.compute / numpy / pandas. What it does, per table:

FLAGS (V columns; same convention as the 2022 T1 and the FIELDS catalog)
  * empty cell                      -> -9999  no data
  * "X"                             -> -1     hidden (statistical confidentiality)
  * "."                             -> -2     uncertain
  * anything that is not a number   -> -9999  unreadable (e.g. 4 kB runs of garbage characters);
                                              each one is logged with line, sector and variable
  * numbers are kept as written ("12.0", "1.67"); a decimal comma becomes a point and a missing
    leading zero is added (",5" -> "0.5", as written in the 2026-06-15 IBGE CSVs)

SECTOR KEYS (setor_code must be 15 digits). A key is classified as:
  * clean       15 digits
  * embedded    the whole row was written, comma separated and quoted, inside the key cell with
                every V cell empty (found for SP in RESPONSAVEL01/02). The row is re-parsed from
                that cell; it must have as many fields as the header (after dropping one extra
                single-digit field that follows the key, the situation code) and a clean key
  * prefix      the key was damaged: scientific notation with a decimal comma and few digits
                ("2,3001E+14", a spreadsheet rounding of the real key) or a short code ("350950",
                the municipality, the start of the real key)
  * bad         anything else; the row is rejected
  Prefix keys are repaired from a reference table whose keys are all clean and complete
  (reference_table, domicilio01): the rows keep the reference order, so between two rows with
  trusted keys the damaged rows take the reference keys at the same positions. It is accepted only
  when the number of damaged rows equals the number of reference rows in that gap, and each
  reference key agrees with the damaged one (rounded to the shown digits, or starting with the
  code). Otherwise the rows are rejected, never guessed. Repaired and rejected counts are reported.

DUPLICATES
  * the same clean key more than once with identical values: later copies dropped
  * the same clean key with different values: later copies dropped and logged as conflicts
  (damaged keys are repaired first-come by position, so identical damaged rows are not duplicates.)

OUTPUT
  * output_folder/<same file name>: columns unchanged, row order kept, keys repaired, flags applied,
    rejected/dropped rows removed
  * every other file of input_folder (GeoPackages, the LAYERS/FIELDS/VARIABLES catalogs, anything
    that is not a sector table) is copied unchanged, so the output folder is standalone. Existing
    copies are skipped unless --overwrite; set copy_other_files to false to turn it off. Only files
    directly in input_folder are copied (subfolders are reported, not copied).
  * report_folder/rejects/<table>_rejects_<timestamp>.csv: every row left out, with reason
  * report_folder/harmonize_summary_<timestamp>.csv and the log: per table rows in/out, keys
    repaired, rows dropped/rejected, flag cells, and the sectors of the reference table that are
    missing in the output (a table with fewer sectors is a finding, not an error)
  * the unique key of each output table is checked
  * a table with a row of a different width than the header stops with an error (not written)

CONFIG (JSON, --config) — required: input_folder, output_folder, report_folder
  file_pattern      T1 table files [IBGE_CENSO-2010_*_BRASIL_2010.csv]
  exclude           substrings of file names to skip [["_X_X"]] (catalog files)
  key_name          sector key column ["setor_code"]
  reference_table   table (file name part, case-insensitive) with complete clean keys ["domicilio01"]
  flags             {"no_data": -9999, "hidden": -1, "uncertain": -2,
                     "hidden_token": "X", "uncertain_token": "."}
  max_unreadable_logged   unreadable cells to list per table in the log [20]
  copy_other_files  copy the files that are not sector tables to output_folder [true]
  other_files_folder  folder those files are copied from [input_folder]; sector tables in it (files
                    matching file_pattern) are never copied
  workers           threads for the column-wise flag conversion [number of CPUs]

Requires pyarrow, numpy and pandas (requirements.txt at the repo root).

Usage:
    .venv/bin/python harmonize_2010.py --config harmonize_2010.local.json [--tables basico,pessoa04] [--overwrite]

output_folder must differ from input_folder. Existing output files are skipped unless --overwrite.
"""

import argparse
import csv
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

NUMBER_PATTERN = r"^-?\d+(\.\d+)?$"
COMMA_NUMBER_PATTERN = r"^-?\d*,\d+$"
SCI_PATTERN = r"^(\d)[,.](\d+)[eE]\+?(\d+)$"

REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    if Path(cfg["input_folder"]).resolve() == Path(cfg["output_folder"]).resolve():
        sys.exit("output_folder must differ from input_folder (the input is never modified)")
    cfg.setdefault("file_pattern", "IBGE_CENSO-2010_*_BRASIL_2010.csv")
    cfg.setdefault("exclude", ["_X_X"])
    cfg.setdefault("key_name", "setor_code")
    cfg.setdefault("reference_table", "domicilio01")
    cfg.setdefault("max_unreadable_logged", 20)
    cfg.setdefault("copy_other_files", True)
    cfg.setdefault("other_files_folder", cfg["input_folder"])
    cfg.setdefault("workers", os.cpu_count() or 1)
    flags = {"no_data": -9999, "hidden": -1, "uncertain": -2,
             "hidden_token": "X", "uncertain_token": "."}
    flags.update(cfg.get("flags", {}))
    cfg["flags"] = flags
    return cfg


def setup_logging(report_folder: Path, stamp: str) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"harmonize_log_{stamp}.txt"
    logger = logging.getLogger("census2010_harmonize")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_path)
    return logger


def table_name(path: Path) -> str:
    return path.name.split("_")[2].lower()


# ---------------------------------------------------------------------------
# Reading / writing
# ---------------------------------------------------------------------------

def read_header(path: Path) -> list:
    with open(path, encoding="utf-8", newline="") as f:
        return next(csv.reader(f, delimiter=";"))


def read_text_table(path: Path, header: list, columns: list = None) -> pa.Table:
    """Read a CSV with every column as text; empty cells stay empty strings."""
    use = columns or header
    conv = pacsv.ConvertOptions(column_types={c: pa.string() for c in use},
                                include_columns=use, strings_can_be_null=False)
    parse = pacsv.ParseOptions(delimiter=";")
    read = pacsv.ReadOptions(block_size=32 << 20)
    tbl = pacsv.read_csv(path, read_options=read, parse_options=parse, convert_options=conv)
    return tbl.combine_chunks()


def mask_count(mask) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def mask_array(mask) -> np.ndarray:
    return np.asarray(mask.to_pandas() if hasattr(mask, "to_pandas") else mask, dtype=bool)


def write_text_table(tbl: pa.Table, path: Path):
    """Values are 15-digit keys and numbers, so nothing needs quoting; the writer refuses (rather
    than corrupt the file) if a value ever contains a delimiter, quote or line break."""
    names = tbl.column_names
    if any(re.search(r'[;"\n\r]', c) for c in names):
        raise ValueError("column names need quoting")
    with open(path, "wb") as f:                       # the writer always quotes a header
        f.write((";".join(names) + "\n").encode("utf-8"))
        pacsv.write_csv(tbl, f, pacsv.WriteOptions(include_header=False, delimiter=";",
                                                   quoting_style="none"))


# ---------------------------------------------------------------------------
# Key classification
# ---------------------------------------------------------------------------

def trim_extra_field(parts: list, width: int) -> list:
    """Embedded SP rows carry one more field than the header: a single digit right after the
    sector key (1-8, the sector situation code of the raw SP files, not a variable). It is dropped."""
    if len(parts) == width + 1 and len(parts) > 1 and re.fullmatch(r"\d", parts[1]):
        return [parts[0]] + parts[2:]
    return parts


def reparse_embedded(tbl: pa.Table, keys: pd.Series, width: int):
    """Find rows whose key cell holds the whole row. Returns (valid row indices, parsed rows,
    {index: reason} for candidates that do not parse)."""
    is_sci = keys.str.match(SCI_PATTERN).fillna(False).to_numpy(dtype=bool)
    cand = keys.str.contains(",", regex=False).fillna(False).to_numpy(dtype=bool) & ~is_sci
    idx = np.flatnonzero(cand)
    if not len(idx):
        return [], [], {}
    sub = tbl.take(pa.array(idx))
    other_filled = np.zeros(len(idx), dtype=bool)
    for j in range(1, tbl.num_columns):
        other_filled |= mask_array(pc.not_equal(sub.column(j), ""))
    valid, parsed, bad = [], [], {}
    clean = re.compile(r"^\d{15}$")
    for i, filled in zip(idx, other_filled):
        if filled:
            continue  # not an embedded row: classified as malformed key later
        try:
            parts = next(csv.reader([keys.iat[i]], delimiter=","))
        except (csv.Error, StopIteration):
            parts = []
        parts = trim_extra_field(parts, width)
        if len(parts) == width and clean.match(parts[0]):
            valid.append(int(i))
            parsed.append(parts)
        else:
            bad[int(i)] = "row embedded in the key cell does not parse"
    return valid, parsed, bad


def classify_keys(keys: pd.Series, embedded_idx: list, embedded_bad: dict):
    """Per row: kind (clean, embedded, prefix_sci, prefix_short, bad), digits (int, prefix rows),
    ndigits (prefix rows), reason (bad rows, else None)."""
    n = len(keys)
    kind = np.full(n, "bad", dtype=object)
    digits = np.zeros(n, dtype=np.int64)
    ndig = np.zeros(n, dtype=np.int64)
    reason = np.full(n, "malformed key", dtype=object)

    clean = keys.str.fullmatch(r"\d{15}").fillna(False).to_numpy(dtype=bool)
    kind[clean] = "clean"
    reason[clean] = None

    short = keys.str.fullmatch(r"\d{1,14}").fillna(False).to_numpy(dtype=bool)
    if short.any():
        kind[short] = "prefix_short"
        reason[short] = None
        digits[short] = pd.to_numeric(keys[short]).to_numpy(dtype=np.int64)
        ndig[short] = keys[short].str.len().to_numpy(dtype=np.int64)

    sci = keys.str.extract(SCI_PATTERN)
    sci_mask = sci[0].notna().to_numpy(dtype=bool)
    if sci_mask.any():
        mant = (sci[0] + sci[1])[sci_mask]
        expo = pd.to_numeric(sci[2][sci_mask]).to_numpy(dtype=np.int64)
        ok = (expo == 14) & (mant.str.len().to_numpy() <= 15)
        pos = np.flatnonzero(sci_mask)
        good, badp = pos[ok], pos[~ok]
        kind[good] = "prefix_sci"
        reason[good] = None
        digits[good] = pd.to_numeric(mant[ok]).to_numpy(dtype=np.int64)
        ndig[good] = mant[ok].str.len().to_numpy(dtype=np.int64)
        reason[badp] = "scientific notation key not of 15 digits"

    for i in embedded_idx:
        kind[i] = "embedded"
        reason[i] = None
    for i, why in embedded_bad.items():
        reason[i] = why
    return kind, digits, ndig, reason


# ---------------------------------------------------------------------------
# Duplicates and key repair
# ---------------------------------------------------------------------------

def find_duplicates(tbl: pa.Table, keys: pd.Series, trusted: np.ndarray) -> dict:
    """{row index: reason} for later copies of a clean/embedded key."""
    tk = keys[trusted]
    dup = tk.duplicated(keep=False)
    if not dup.any():
        return {}
    idx = tk.index[dup.to_numpy()].to_numpy()
    sub = tbl.take(pa.array(idx))
    joined = pc.binary_join_element_wise(*sub.columns, ";").to_pandas()
    df = pd.DataFrame({"i": idx, "key": keys.iloc[idx].to_numpy(), "row": joined.to_numpy()})
    first = df.groupby("key", sort=False)["row"].transform("first")
    later = df.duplicated("key", keep="first")
    out = {}
    for i, same in zip(df["i"][later], (df["row"] == first)[later]):
        out[int(i)] = "duplicate key, identical values" if same else "duplicate key, conflicting values"
    return out


def repair_prefix_keys(keys: pd.Series, kind: np.ndarray, digits: np.ndarray, ndig: np.ndarray,
                       reason: np.ndarray, ref_keys: list, ref_num: np.ndarray,
                       ref_pos: pd.Series, logger, table: str) -> dict:
    """Return {row index: repaired key}; rows that cannot be repaired get a reason in `reason`."""
    kept = np.flatnonzero(np.array([r is None for r in reason]))
    k_kind = kind[kept]
    prefix = np.isin(k_kind, ("prefix_sci", "prefix_short"))
    fixed = {}
    if not prefix.any():
        return fixed
    trusted = np.isin(k_kind, ("clean", "embedded"))
    rp = keys.iloc[kept].map(ref_pos).to_numpy(dtype=float)  # NaN when not in the reference
    anchor = trusted & ~np.isnan(rp)
    pa_pos = np.flatnonzero(anchor)
    ra = rp[anchor].astype(np.int64)
    if len(ra) > 1 and not np.all(np.diff(ra) > 0):
        logger.warning("%-18s row order differs from the reference — damaged keys cannot be repaired", table)
        for i in kept[prefix]:
            reason[i] = "damaged key, row order differs from the reference"
        return fixed
    a_pos = np.concatenate(([-1], pa_pos, [len(kept)]))
    a_ref = np.concatenate(([-1], ra, [len(ref_keys)]))
    cs = np.concatenate(([0], np.cumsum(prefix)))  # cs[x] = prefix rows at positions < x
    cnt = cs[a_pos[1:]] - cs[a_pos[:-1] + 1]
    for g in np.flatnonzero(cnt > 0):
        lo, hi = a_pos[g] + 1, a_pos[g + 1]
        rows = np.flatnonzero(prefix[lo:hi]) + lo          # positions in `kept`
        ref_gap = int(a_ref[g + 1] - a_ref[g] - 1)
        idx = kept[rows]
        if len(rows) != ref_gap:
            for i in idx:
                reason[i] = f"damaged key, {len(rows)} damaged rows vs {ref_gap} reference sectors in the gap"
            continue
        cands = ref_num[a_ref[g] + 1:a_ref[g + 1]]
        is_sci = kind[idx] == "prefix_sci"
        d, nd = digits[idx], ndig[idx]
        unit = 10 ** (15 - nd)
        sci_ok = (cands * 2 + unit) // (2 * unit) == d      # rounded to the shown digits
        short_ok = cands // unit == d                       # starts with the code
        if np.all(np.where(is_sci, sci_ok, short_ok)):
            for i, c in zip(idx, ref_keys[a_ref[g] + 1:a_ref[g + 1]]):
                fixed[int(i)] = c
        else:
            for i in idx:
                reason[i] = "damaged key, prefix does not match the reference keys of the gap"
    return fixed


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def convert_column(col, flags: dict):
    """Flag convention for one V column (a pyarrow text column).
    Returns (new column, counts (no_data, hidden, uncertain, unreadable), row mask or None,
    unreadable row positions or None)."""
    ndt, hid, unc = str(flags["no_data"]), str(flags["hidden"]), str(flags["uncertain"])
    empty = pc.equal(col, "")
    is_h = pc.equal(col, flags["hidden_token"])
    is_u = pc.equal(col, flags["uncertain_token"])
    num = pc.match_substring_regex(col, NUMBER_PATTERN)
    com = pc.match_substring_regex(col, COMMA_NUMBER_PATTERN)
    bad = pc.invert(pc.or_(pc.or_(empty, is_h), pc.or_(pc.or_(is_u, num), com)))
    counts = tuple(mask_count(m) for m in (empty, is_h, is_u, bad))
    if not any(counts):
        return col, counts, None, None
    fixed = pc.replace_substring(pc.replace_substring_regex(col, r"^(-?),", r"\10,"), ",", ".")
    out = pc.if_else(com, fixed, col)
    out = pc.if_else(pc.or_(empty, bad), ndt, out)
    out = pc.if_else(is_h, hid, out)
    out = pc.if_else(is_u, unc, out)
    rows = mask_array(pc.or_(pc.or_(empty, is_h), pc.or_(is_u, bad)))
    bad_idx = np.flatnonzero(mask_array(bad)) if counts[3] else None
    return out, counts, rows, bad_idx


def convert_flags(tbl: pa.Table, flags: dict, stats: dict, workers: int):
    """Apply the flag convention to every V column (columns are processed in parallel threads;
    the pyarrow kernels release the GIL). Returns (new columns, row_flagged mask,
    {column index: unreadable row positions})."""
    row_flag = np.zeros(tbl.num_rows, dtype=bool)
    unreadable = {}
    cols = [tbl.column(0)]
    todo = [tbl.column(j) for j in range(1, tbl.num_columns)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda c: convert_column(c, flags), todo))
    for j, (out, counts, rows, bad_idx) in enumerate(results, 1):
        stats["no_data"] += counts[0]
        stats["hidden"] += counts[1]
        stats["uncertain"] += counts[2]
        stats["unreadable"] += counts[3]
        if rows is not None:
            row_flag |= rows
        if bad_idx is not None:
            unreadable[j] = bad_idx
        cols.append(out)
    return cols, row_flag, unreadable


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def process_table(src: Path, dst: Path, cfg: dict, ref_keys: list, ref_num: np.ndarray,
                  ref_pos: pd.Series, stamp: str, logger):
    table = table_name(src)
    header = read_header(src)
    width = len(header)
    if header[0] != cfg["key_name"]:
        logger.error("%-18s first column is %r, expected %r — skipped", table, header[0], cfg["key_name"])
        return None
    try:
        tbl = read_text_table(src, header)
    except pa.ArrowInvalid as e:
        logger.error("%-18s cannot read the CSV (rows of different width?): %s", table, str(e)[:300])
        return None
    n_in = tbl.num_rows
    orig_keys = tbl.column(0).to_pandas().astype(object)
    keys = pd.Series(orig_keys.to_numpy(), dtype=object)

    # 1. rows embedded in the key cell
    emb_idx, emb_rows, emb_bad = reparse_embedded(tbl, keys, width)
    if emb_idx:
        mask = pa.array(np.isin(np.arange(n_in), emb_idx))
        new_cols = []
        for j in range(width):
            repl = pa.array([r[j] for r in emb_rows], pa.string())
            new_cols.append(pc.replace_with_mask(tbl.column(j), mask, repl))
        tbl = pa.table(new_cols, names=header)
        keys = pd.Series(tbl.column(0).to_pandas().to_numpy(), dtype=object)

    # 2. classify keys
    kind, digits, ndig, reason = classify_keys(keys, emb_idx, emb_bad)

    # 3. duplicates, then repair of damaged keys
    trusted = np.isin(kind, ("clean", "embedded"))
    for i, why in find_duplicates(tbl, keys, trusted).items():
        reason[i] = why
    fixed = repair_prefix_keys(keys, kind, digits, ndig, reason, ref_keys, ref_num, ref_pos,
                               logger, table)
    for i in np.flatnonzero(np.array([r is None for r in reason])):
        if kind[i].startswith("prefix") and i not in fixed:
            reason[i] = "damaged key not repaired"
    keep = np.array([r is None for r in reason])
    keep_idx = np.flatnonzero(keep)

    # 4. rejects file
    rejects_dir = Path(cfg["report_folder"]) / "rejects"
    rejects_dir.mkdir(parents=True, exist_ok=True)
    rejects_path = rejects_dir / f"{table}_rejects_{stamp}.csv"
    reasons = {}
    with open(rejects_path, "w", encoding="utf-8", newline="") as fr:
        wr = csv.writer(fr, delimiter=";", lineterminator="\n")
        wr.writerow(["line", "reason", "key_cell"])
        for i in np.flatnonzero(~keep):
            wr.writerow([i + 2, reason[i], orig_keys.iat[i][:80]])
            label = re.sub(r"\d+", "N", reason[i])
            reasons[label] = reasons.get(label, 0) + 1

    # 5. output table: repaired keys, flags
    out = tbl.take(pa.array(keep_idx))
    new_keys = keys.iloc[keep_idx].to_numpy(dtype=object).copy()
    for pos, i in enumerate(keep_idx):
        if i in fixed:
            new_keys[pos] = fixed[i]
    out = out.set_column(0, header[0], pa.array(new_keys, pa.string()))
    stats = dict(no_data=0, hidden=0, uncertain=0, unreadable=0)
    cols, row_flag, unreadable = convert_flags(out, cfg["flags"], stats, cfg["workers"])
    final = pa.table(cols, names=header)

    # 6. write
    part = dst.with_name(dst.name + ".part")
    write_text_table(final, part)

    n_out = final.num_rows
    out_keys = set(new_keys)
    ref_set = set(ref_keys)
    result = dict(table=table, rows_in=n_in, rows_out=n_out, keys_repaired=len(fixed),
                  rows_reparsed=len(emb_idx), rows_left_out=n_in - n_out,
                  no_data_cells=stats["no_data"], hidden_cells=stats["hidden"],
                  uncertain_cells=stats["uncertain"], unreadable_cells=stats["unreadable"],
                  rows_with_flags=int(row_flag.sum()),
                  sectors_missing_vs_reference=len(ref_set - out_keys),
                  sectors_not_in_reference=len(out_keys - ref_set),
                  unique_keys=(len(out_keys) == n_out))
    logger.info("%-18s in=%7d out=%7d  keys repaired=%d re-parsed=%d  left out=%d",
                table, n_in, n_out, len(fixed), len(emb_idx), n_in - n_out)
    for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
        logger.info("    left out: %d × %s", c, r)
    logger.info("    flag cells: no_data=%d hidden=%d uncertain=%d unreadable=%d (rows with flags: %d)",
                stats["no_data"], stats["hidden"], stats["uncertain"], stats["unreadable"],
                result["rows_with_flags"])
    entries = sorted((int(r), j) for j, rows in unreadable.items() for r in rows)
    for r, j in entries[:cfg["max_unreadable_logged"]]:
        v = tbl.take(pa.array([keep_idx[r]])).column(j)[0].as_py()
        shown = v if len(v) <= 20 else v[:20] + f"... ({len(v)} characters)"
        logger.warning("    unreadable -> no data: line %d, sector %s, %s = %r",
                       int(keep_idx[r]) + 2, new_keys[r], header[j], shown)
    if not result["unique_keys"]:
        logger.error("    output keys are not unique")
    if result["sectors_missing_vs_reference"] or result["sectors_not_in_reference"]:
        logger.info("    vs reference: %d reference sectors missing, %d sectors not in reference",
                    result["sectors_missing_vs_reference"], result["sectors_not_in_reference"])
    if dst.exists():
        dst.unlink()
    part.rename(dst)
    if n_in == n_out:
        rejects_path.unlink()
    return result


def copy_other_files(in_dir: Path, out_dir: Path, cfg: dict, overwrite: bool, logger):
    """Copy every file of in_dir that is not a sector table (GeoPackages, catalogs, ...)."""
    def is_table(p: Path) -> bool:
        return fnmatch.fnmatch(p.name, cfg["file_pattern"]) and not any(x in p.name for x in cfg["exclude"])
    others = sorted(p for p in in_dir.iterdir() if p.is_file() and not is_table(p))
    for d in sorted(p for p in in_dir.iterdir() if p.is_dir()):
        logger.warning("subfolder %s not copied", d.name)
    logger.info("Copying %d other files from %s to the output folder", len(others), in_dir)
    for src in others:
        dst = out_dir / src.name
        if dst.exists() and not overwrite:
            logger.info("  %-50s exists — skipped", src.name)
            continue
        part = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, part)
        shutil.copystat(src, part)
        if dst.exists():
            dst.unlink()
        part.rename(dst)
        logger.info("  %-50s %8.1f MB", src.name, src.stat().st_size / 1e6)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--tables", help="comma-separated subset of tables (e.g. basico,pessoa04)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing output files")
    args = ap.parse_args()
    cfg = load_config(args.config)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = setup_logging(Path(cfg["report_folder"]), stamp)

    in_dir, out_dir = Path(cfg["input_folder"]), Path(cfg["output_folder"])
    files = sorted(p for p in in_dir.glob(cfg["file_pattern"])
                   if not any(x in p.name for x in cfg["exclude"]))
    by_name = {table_name(p): p for p in files}
    ref_name = cfg["reference_table"].lower()
    if ref_name not in by_name:
        sys.exit(f"reference table {ref_name} not found in {in_dir}")
    only = {t.lower() for t in args.tables.split(",")} if args.tables else None
    if only and (only - set(by_name)):
        sys.exit(f"unknown tables: {sorted(only - set(by_name))}")

    ref_path = by_name[ref_name]
    ref_tbl = read_text_table(ref_path, read_header(ref_path), [cfg["key_name"]])
    ref_keys = ref_tbl.column(0).to_pylist()
    ref_series = pd.Series(ref_keys, dtype=object)
    bad_ref = int((~ref_series.str.fullmatch(r"\d{15}")).sum())
    n_distinct = int(ref_series.nunique())
    logger.info("=" * 60)
    logger.info("Census 2010 — T1 harmonization (%d tables)", len(only) if only else len(by_name))
    logger.info("Reference %s: %d sectors, %d distinct, %d not clean",
                ref_name, len(ref_keys), n_distinct, bad_ref)
    logger.info("Input %s", in_dir)
    logger.info("Output %s", out_dir)
    logger.info("=" * 60)
    if bad_ref or n_distinct != len(ref_keys):
        sys.exit("reference table keys are not clean and unique — choose another reference_table")
    ref_pos = pd.Series(np.arange(len(ref_keys)), index=ref_series)
    ref_num = pd.to_numeric(ref_series).to_numpy(dtype=np.int64)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for name, src in by_name.items():
        if only and name not in only:
            continue
        dst = out_dir / src.name
        if dst.exists() and not args.overwrite:
            logger.warning("%-18s output exists — skipped (use --overwrite)", name)
            continue
        res = process_table(src, dst, cfg, ref_keys, ref_num, ref_pos, stamp, logger)
        if res:
            results.append(res)

    if cfg["copy_other_files"]:
        copy_other_files(Path(cfg["other_files_folder"]), out_dir, cfg, args.overwrite, logger)

    if results:
        summary = Path(cfg["report_folder"]) / f"harmonize_summary_{stamp}.csv"
        with open(summary, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0]), delimiter=";", lineterminator="\n")
            w.writeheader()
            w.writerows(results)
        logger.info("=" * 60)
        logger.info("Summary: %s", summary)
        left = [(r["table"], r["rows_left_out"]) for r in results if r["rows_left_out"]]
        if left:
            logger.warning("Tables with rows left out (see rejects/): %s", left)
        logger.info("=" * 60)


if __name__ == "__main__":
    main()

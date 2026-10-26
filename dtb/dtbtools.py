"""
Shared helpers of the DTB T1 scripts (assemble_tables.py, assemble_geography.py, build_lineage.py,
plot_lineage.py): config loading, logging, name folding, IBGE check digit, spreadsheet reading straight
from the source zips, CONSISTENCY merge and CSV writing.
"""

import csv
import io
import json
import logging
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

NA = "NA"
CONSISTENCY_FIELDS = ["layer", "code", "issue", "value", "action", "note"]


def load_config(path: str, required: list) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in required if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(report_folder: Path, name: str) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"{name}_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger(f"dtb_{name}")
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


def fold(text) -> str:
    """Upper case, no accents, hyphens, apostrophes and spaces collapsed — for name comparison only."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()
    text = re.sub(r"[\s\-'`´.]+", " ", text)
    return text.strip()


def clean_name(text) -> str:
    """Published name with surrounding / repeated whitespace (incl. NBSP) removed; NA when empty."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return NA
    text = re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()
    return text if text else NA


def check_digit(code6: str) -> str:
    """IBGE municipality check digit (weights 1,2,1,2,1,2; two-digit products are summed digit by digit)."""
    total = 0
    for i, ch in enumerate(code6):
        p = int(ch) * (1 if i % 2 == 0 else 2)
        total += p // 10 + p % 10
    return str((10 - total % 10) % 10)


def norm_code(value) -> str:
    """Spreadsheet cell -> digit string ('11.0' -> '11'); '' when empty."""
    if value is None:
        return ""
    s = str(value).strip()
    s = re.sub(r"\.0+$", "", s)
    return s if re.fullmatch(r"\d+", s) else ""


def zip_bytes(zip_path: Path, member: str) -> bytes:
    with zipfile.ZipFile(zip_path) as z:
        names = {n: n for n in z.namelist()}
        if member not in names:
            # member names of old zips may be stored in cp850
            for n in z.namelist():
                try:
                    if n.encode("cp437").decode("cp850") == member:
                        member = n
                        break
                except UnicodeError:
                    continue
        return z.read(member)


def read_sheet(data: bytes, sheet=None) -> list:
    """All rows of a spreadsheet sheet (xls / xlsx / ods) as lists of python values."""
    try:
        from python_calamine import CalamineWorkbook
        wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
        name = sheet if sheet is not None else wb.sheet_names[0]
        return wb.get_sheet_by_name(name).to_python()
    except Exception:  # very old BIFF files: fall back to xlrd
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        s = wb.sheet_by_name(sheet) if sheet is not None else wb.sheet_by_index(0)
        return [s.row_values(i) for i in range(s.nrows)]


def merge_consistency(path: Path, rows: list, layers) -> int:
    """Rewrite CONSISTENCY keeping the rows of other layers; returns the number of rows kept."""
    kept = []
    layers = set(layers)
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter=";") if r["layer"] not in layers]
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONSISTENCY_FIELDS, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(kept + rows)
    tmp.replace(path)
    return len(kept)


def write_csv(df: pd.DataFrame, path: Path, overwrite: bool, logger) -> None:
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} exists (use --overwrite)")
    tmp = path.with_name(path.name + ".part")
    df.to_csv(tmp, sep=";", index=False, encoding="utf-8", lineterminator="\n", na_rep=NA)
    tmp.replace(path)
    logger.info("Wrote %s (%d rows)", path, len(df))

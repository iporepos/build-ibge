"""
Assemble the IBGE Census 2010 sector tables (T0) from the IBGE source zips in SRC.

One table per output file (`setor_code;V001;V002;...`), all UFs concatenated, values kept exactly as
IBGE wrote them (text: no float conversion, so no rounding; decimal commas, "X" and empty cells are
left for harmonize_2010.py, which turns this T0 into T1). Sources (see download_2010.py):

  * universo: <UF>_<release>.zip, one CSV per table and UF (".../CSV/<Table>_<UF>.csv", `;`, latin-1,
    quoted header). The leading id/name columns (Cod_setor, Situacao_setor, names in Basico...) are
    dropped except the key.
  * sinopse: Base_informacoes_setores2010_sinopse_<UF>.zip, one XLS per UF (cells stored as text;
    a numeric cell is written as an integer when whole, and counted in the log). Empty sheets skipped.

Per table:
  * the V columns of every member are renamed by the config (rename_columns; e.g. pessoa02 is
    numbered V001-V170 in the CSVs but V086-V255 in the IBGE documentation and the VARIABLES catalog)
    and must then equal, in order, the variables of the table in the VARIABLES catalog (plus the
    columns listed in uncatalogued_columns, kept where they stand) — else the table stops (nothing written)
  * every key must be 15 digits and every member must hold one UF (2-digit key prefix); duplicate
    keys inside the table stop it; UFs of expected_ufs missing from the table are reported
  * rows are sorted by setor_code; written to <name>.part, renamed when complete

Report (report_folder): log, assemble_summary_<stamp>.csv (table, uf, rows, members) and, when
reference_folder is set (a previous T0/T1 folder), the rows per table and UF of the reference next to
the new ones (assemble_compare_<stamp>.csv) — a review aid, not a check.

CONFIG (JSON, --config) — required: src_folder, output_folder, report_folder, variables_catalog, products
  output_name        ["IBGE_CENSO-2010_{TABLE}_BRASIL_2010.csv"]
  key_source / key_name   ["Cod_setor"] / ["setor_code"]
  expected_ufs       2-digit UF codes every table must cover [[]]
  rename_columns     {table: {"offset": n, "first": "V001", "last": "V170"}}: a member whose V columns
                     run exactly first..last gets each renamed V<number + offset>; a member already
                     numbered like the catalog is left as is; anything else stops the table
  uncatalogued_columns  {table: [V columns]} present in the source but not in the catalog, kept
                     (e.g. domicilio01 V030-V032: in the data, skipped by the IBGE documentation)
  reference_folder   optional folder with tables of the same names, for the comparison report
  products           [{name, folder, zip_pattern, format: csv|xls, member_pattern (regex on the member
                     path; group 'table' gives the table, or a fixed "table" key), encoding [latin-1],
                     delimiter [;]}]

Requires pandas (project venv) and, for the sinopse XLS, xlrd.

Usage:
    .venv/bin/python assemble_2010.py --config assemble_2010.local.json [--tables basico,sinopse] [--overwrite]
"""

import argparse
import csv
import io
import json
import logging
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

REQUIRED_KEYS = ["src_folder", "output_folder", "report_folder", "variables_catalog", "products"]
V_PATTERN = re.compile(r"^V(\d+)$")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("output_name", "IBGE_CENSO-2010_{TABLE}_BRASIL_2010.csv")
    cfg.setdefault("key_source", "Cod_setor")
    cfg.setdefault("key_name", "setor_code")
    cfg.setdefault("expected_ufs", [])
    cfg.setdefault("rename_columns", {})
    cfg.setdefault("uncatalogued_columns", {})
    cfg.setdefault("reference_folder", None)
    for p in cfg["products"]:
        miss = [k for k in ("name", "folder", "zip_pattern", "format", "member_pattern") if k not in p]
        if miss:
            sys.exit(f"Product {p.get('name', '?')} is missing keys: {miss}")
        if p["format"] not in ("csv", "xls"):
            sys.exit(f"Product {p['name']}: format must be csv or xls")
        p.setdefault("encoding", "latin-1")
        p.setdefault("delimiter", ";")
    return cfg


def setup_logging(report_folder: Path, stamp: str) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"assemble_log_{stamp}.txt"
    logger = logging.getLogger("census2010_assemble")
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


def catalog_columns(path: Path) -> dict:
    """{table: [V columns in catalog order]} from the VARIABLES catalog (table;...;variable_name;...)."""
    out = defaultdict(list)
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            out[row["table"].lower()].append(row["variable_name"])
    return dict(out)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def find_members(cfg: dict, logger) -> dict:
    """{table: [(product, zip path, member name)]}; a member listed twice in one zip is taken once."""
    found = defaultdict(list)
    for p in cfg["products"]:
        folder = Path(cfg["src_folder"]) / p["folder"]
        zips = sorted(folder.glob(p["zip_pattern"]))
        if not zips:
            logger.error("product %s: no zip matches %s in %s", p["name"], p["zip_pattern"], folder)
        pat = re.compile(p["member_pattern"])
        for z in zips:
            with zipfile.ZipFile(z) as zf:
                names = list(dict.fromkeys(n for n in zf.namelist() if not n.endswith("/")))
            hits = 0
            for n in names:
                m = pat.search(n)
                if not m:
                    continue
                table = (m.groupdict().get("table") or p.get("table") or "").lower()
                if not table:
                    sys.exit(f"product {p['name']}: member_pattern has no 'table' group and no fixed table")
                found[table].append((p, z, n))
                hits += 1
            if not hits:
                logger.warning("product %s: no member of %s matches the member pattern", p["name"], z.name)
    return found


def read_csv_member(p: dict, data: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(data), sep=p["delimiter"], encoding=p["encoding"], dtype=str,
                       keep_default_na=False, na_filter=False, quotechar='"')


def xls_text(cell, stats: dict) -> str:
    import xlrd
    if cell.ctype in (xlrd.XL_CELL_TEXT, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return str(cell.value)
    stats["numeric_cells"] += 1
    v = cell.value
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v)


def read_xls_member(data: bytes, stats: dict) -> pd.DataFrame:
    import xlrd
    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    frames = []
    for sh in book.sheets():
        if sh.nrows == 0:
            continue
        header = [str(h).strip() for h in sh.row_values(0)]
        rows = [[xls_text(c, stats) for c in sh.row(r)] for r in range(1, sh.nrows)]
        frames.append(pd.DataFrame(rows, columns=header))
    if not frames:
        return pd.DataFrame()
    if len(frames) > 1 and any(list(f.columns) != list(frames[0].columns) for f in frames):
        raise ValueError("sheets with different headers")
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def rename_v(columns: list, rule: dict) -> dict:
    """{old: new} for a rename rule; raises if the source V columns are not exactly first..last."""
    lo, hi = int(V_PATTERN.match(rule["first"]).group(1)), int(V_PATTERN.match(rule["last"]).group(1))
    width = len(rule["first"]) - 1
    expected = [f"V{i:0{width}d}" for i in range(lo, hi + 1)]
    if columns != expected:
        raise ValueError(f"rename rule expects {rule['first']}..{rule['last']}, source has "
                         f"{columns[:2]}..{columns[-1:]} ({len(columns)} columns)")
    return {c: f"V{int(c[1:]) + rule['offset']:03d}" for c in columns}


def assemble_table(table: str, members: list, cfg: dict, catalog_cols: list, logger):
    """Returns (DataFrame, [(uf, rows, member)]) or raises ValueError."""
    key_src, key = cfg["key_source"], cfg["key_name"]
    rule = cfg["rename_columns"].get(table)
    allowed = set(cfg["uncatalogued_columns"].get(table, []))
    frames, per_uf, renamed = [], [], []
    for p, zpath, name in members:
        stats = {"numeric_cells": 0}
        with zipfile.ZipFile(zpath) as zf:
            data = zf.read(name)
        df = read_csv_member(p, data) if p["format"] == "csv" else read_xls_member(data, stats)
        label = f"{zpath.name}:{name.rsplit('/', 1)[-1]}"
        df.columns = [str(c).strip() for c in df.columns]
        if key_src not in df.columns:
            raise ValueError(f"{label}: no key column {key_src!r} (columns {list(df.columns)[:4]}...)")
        vcols = [c for c in df.columns if V_PATTERN.match(c)]
        df = df[[key_src] + vcols]
        if rule and [c for c in vcols if c not in allowed] != catalog_cols:
            df = df.rename(columns=rename_v(vcols, rule))
            vcols = [c for c in df.columns if c != key_src]
            renamed.append(label)
        if [c for c in vcols if c not in allowed] != catalog_cols:
            missing = [c for c in catalog_cols if c not in vcols]
            extra = [c for c in vcols if c not in catalog_cols and c not in allowed]
            raise ValueError(f"{label}: V columns differ from the VARIABLES catalog: missing {missing[:5]} "
                             f"({len(missing)}), extra {extra[:5]} ({len(extra)})"
                             + ("" if missing or extra else ", same set in another order"))
        df = df.rename(columns={key_src: key})
        df = df.apply(lambda s: s.str.strip())
        keys = df[key]
        bad = keys[~keys.str.fullmatch(r"\d{15}")]
        if len(bad):
            raise ValueError(f"{label}: {len(bad)} keys are not 15 digits, e.g. {bad.head(3).tolist()}")
        ufs = keys.str[:2].unique()
        if len(ufs) != 1:
            raise ValueError(f"{label}: keys of several UFs {sorted(ufs)}")
        if stats["numeric_cells"]:
            logger.info("    %s: %d numeric XLS cells written as text", label, stats["numeric_cells"])
        if frames and list(df.columns) != list(frames[0].columns):
            raise ValueError(f"{label}: columns differ from the first member of the table")
        frames.append(df)
        per_uf.append((ufs[0], len(df), label))
    if renamed:
        logger.info("    %d members renumbered by rename_columns, %d already numbered like the catalog",
                    len(renamed), len(members) - len(renamed))
    kept = [c for c in frames[0].columns if c in allowed]
    if kept:
        logger.info("    uncatalogued columns kept: %s", kept)
    out = pd.concat(frames, ignore_index=True)
    dup = out[key].duplicated(keep=False)
    if dup.any():
        raise ValueError(f"{int(dup.sum())} rows share a key, e.g. {out[key][dup].head(3).tolist()}")
    return out.sort_values(key, kind="stable", ignore_index=True), per_uf


def write_table(df: pd.DataFrame, path: Path):
    """Keys and numbers only, so nothing needs quoting; refuse rather than corrupt otherwise."""
    for c in df.columns:
        if df[c].str.contains(r'[;"\n\r]', regex=True).any():
            raise ValueError(f"column {c} has a value that would need quoting")
    part = path.with_name(path.name + ".part")
    df.to_csv(part, sep=";", index=False, lineterminator="\n", quoting=csv.QUOTE_NONE, encoding="utf-8")
    part.replace(path)


def reference_counts(path: Path, key: str) -> dict:
    """{uf: rows} of a reference table (first column = key)."""
    keys = pd.read_csv(path, sep=";", usecols=[0], dtype=str, keep_default_na=False)[key]
    return keys.str[:2].value_counts().to_dict()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--tables", help="comma-separated subset of tables (e.g. basico,sinopse)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing output files")
    args = ap.parse_args()
    cfg = load_config(args.config)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = Path(cfg["report_folder"])
    logger = setup_logging(report, stamp)

    out_dir = Path(cfg["output_folder"])
    catalog = catalog_columns(Path(cfg["variables_catalog"]))
    members = find_members(cfg, logger)
    only = {t.lower() for t in args.tables.split(",")} if args.tables else None
    if only and (only - set(members)):
        sys.exit(f"unknown tables: {sorted(only - set(members))}; found {sorted(members)}")
    without = sorted(set(catalog) - set(members))
    logger.info("=" * 60)
    logger.info("Census 2010 — T0 assembly: %d tables found in %s", len(members), cfg["src_folder"])
    logger.info("Output %s", out_dir)
    if without:
        logger.warning("tables of the VARIABLES catalog with no source member: %s", without)
    logger.info("=" * 60)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary, compare, failed = [], [], []
    expected_ufs = set(cfg["expected_ufs"])
    for table in sorted(members):
        if only and table not in only:
            continue
        dst = out_dir / cfg["output_name"].format(TABLE=table.upper(), table=table)
        if dst.exists() and not args.overwrite:
            logger.warning("%-18s output exists — skipped (use --overwrite)", table)
            continue
        if table not in catalog:
            logger.error("%-18s not in the VARIABLES catalog — skipped", table)
            failed.append(table)
            continue
        try:
            df, per_uf = assemble_table(table, members[table], cfg, catalog[table], logger)
            write_table(df, dst)
        except ValueError as exc:
            logger.error("%-18s STOPPED, nothing written: %s", table, exc)
            failed.append(table)
            continue
        rows_uf = defaultdict(int)
        for uf, n, label in per_uf:
            rows_uf[uf] += n
            summary.append({"table": table, "uf": uf, "rows": n, "member": label})
        missing_ufs = sorted(expected_ufs - set(rows_uf))
        logger.info("%-18s %7d rows, %3d columns, %d members, %d UFs%s", table, len(df), df.shape[1],
                    len(per_uf), len(rows_uf), f"  MISSING UFs {missing_ufs}" if missing_ufs else "")
        if missing_ufs:
            failed.append(table)
        if cfg["reference_folder"]:
            ref = Path(cfg["reference_folder"]) / dst.name
            if ref.exists():
                old = reference_counts(ref, cfg["key_name"])
                for uf in sorted(set(old) | set(rows_uf)):
                    compare.append({"table": table, "uf": uf, "rows_reference": old.get(uf, 0),
                                    "rows_new": rows_uf.get(uf, 0),
                                    "difference": rows_uf.get(uf, 0) - old.get(uf, 0)})
                diff = [(c["uf"], c["difference"]) for c in compare if c["table"] == table and c["difference"]]
                if diff:
                    logger.info("    rows vs reference by UF: %s", ", ".join(f"{u} {d:+d}" for u, d in diff))
            else:
                logger.info("    no reference table %s", ref.name)

    for name, rows in (("summary", summary), ("compare", compare)):
        if rows:
            path = report / f"assemble_{name}_{stamp}.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";", lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
            logger.info("%s: %s", name.capitalize(), path)
    logger.info("=" * 60)
    if failed:
        logger.error("Tables with problems: %s", failed)
    else:
        logger.info("All tables assembled")
    logger.info("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

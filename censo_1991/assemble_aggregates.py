"""
Assemble the IBGE Census 1991 municipal aggregates into the T1 table `agregados-municipios`.

The table is a near-copy of the values IBGE ships per municipality (no estimation from the sample;
computed aggregates belong to T2). One row per DTB 1991 municipality, key municipio_code, which
links to the `municipios` geography layer. Variables are listed in the spec (aggregates_1991.json):
  * fracamo   FRACAMO.TXT (auxiliary file of the sample microdata): resident population, permanent
              private households and effective sampling fraction per municipality (universe)
  * xls       <src_folder>/<folder>/*.zip, one zipped spreadsheet per UF (IBGE, 1995), rows
              "<name> - <UF>" with one column per variable (header label = spec "column"); a sheet
              may also stack the columns as blocks (a row holding only the label starts a block,
              e.g. POPU91MS.XLS)
Spreadsheet rows carry names only: a name is matched to the DTB 1991 (NOME_EM_MI) inside its UF,
case/accent/hyphen-insensitive, or through name_aliases (later names, other spellings).
Rows marked with missing_markers ("...", "-") are municipalities created after 1991 and are skipped.

Checks (the run stops, nothing is written, on failure):
  * every DTB municipality is in FRACAMO; every spreadsheet municipality row with values resolves
    to exactly one DTB municipality, once
  * sum_checks of the spec: the parts cover every DTB municipality and add up to the total exactly
    (urban + rural = male + female = V001 of FRACAMO), which also proves the aliases
Other findings go to CONSISTENCY (layer = table name): names matched by alias, municipalities absent
from a spreadsheet (value -9999), stacked layouts, rows that are neither municipality, UF nor Brasil,
UF totals that differ from the sum of their municipalities.

Usage:
    python assemble_aggregates.py --config assemble_aggregates.local.json [--overwrite]
"""

import argparse
import csv
import json
import logging
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import xlrd

from assemble_geography import CONSISTENCY_FIELDS, fold, merge_consistency, read_dtb

REQUIRED_KEYS = ["spec", "src_folder", "fracamo_txt", "dtb_xls", "output_folder", "report_folder"]
NO_DATA = -9999
MUNI_RE = re.compile(r"^(.*\S)\s+-\s+([A-Z]{2})$")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("table_pattern", "IBGE_CENSO-1991_{NAME}_BRASIL_1991.csv")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    cfg.setdefault("fracamo_encoding", "cp1252")
    cfg.setdefault("missing_markers", ["...", "-"])
    cfg.setdefault("name_aliases", {})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"aggregates_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_aggregates")
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


def label(v) -> str:
    """Header text as written in the spreadsheet (1991.0 -> '1991')."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def read_fracamo(path: str, encoding: str, code6: dict) -> dict:
    """{municipio_code: {"fraction", "households", "persons"}} from the municipality rows
    (region UF meso micro MMMM fraction households persons; MMMM = 0000 on the upper levels)."""
    out = {}
    with open(path, encoding=encoding) as f:
        for line in f:
            p = line.split()
            if len(p) != 8 or p[4] == "0000":
                continue
            c6 = p[1] + p[4]
            if c6 not in code6:
                sys.exit(f"FRACAMO: municipality {c6} not in the DTB 1991")
            out[code6[c6]] = {"fraction": float(p[5]), "households": int(p[6]), "persons": int(p[7])}
    return out


def read_sheet(content: bytes, wanted: list, markers: list) -> tuple:
    """Rows of the first sheet as (name, {label: value}) plus the layout ("wide" or "stacked").
    Values are floats, a missing marker, or other text (reported by the caller)."""
    sh = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    header = [label(v) for v in sh.row_values(0)]
    stacked = not set(wanted) <= set(header[1:])
    rows, block = [], None
    for r in range(1, sh.nrows):
        cells = sh.row_values(r)
        name = str(cells[0]).strip()
        vals = [v.strip() if isinstance(v, str) else v for v in cells[1:]]
        if name in wanted and not any(isinstance(v, float) for v in vals):
            block = name
            continue
        if not name and all(v in markers or v == "" for v in vals):
            continue
        if stacked:
            if block is None:
                continue
            rows.append((name, {block: vals[0]}))
        else:
            rows.append((name, dict(zip(header[1:], vals))))
    return rows, ("stacked" if stacked else "wide")


def read_xls_folder(folder: Path, columns: dict, ctx: dict, cons: list, logger) -> dict:
    """{municipio_code: {V name: value}} for one folder of zipped per-UF spreadsheets.
    columns = {spreadsheet label: V name}."""
    table, markers, sigla_uf = ctx["table"], ctx["markers"], ctx["sigla_uf"]
    out, errors = {}, []
    loose = sorted(p.name for p in folder.iterdir() if p.suffix.lower() != ".zip")
    if loose:
        logger.info("  %s: files outside the zips not read: %s", folder.name, loose)
    for z in sorted(folder.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            members = [m for m in zf.namelist() if m.upper().endswith(".XLS")]
            if len(members) != 1:
                errors.append(f"{z.name}: expected one .XLS, found {members}")
                continue
            member = members[0]
            rows, layout = read_sheet(zf.read(member), list(columns), markers)
        sig = Path(member).stem[-2:].upper()
        uf = sigla_uf.get(sig)
        if uf is None:
            errors.append(f"{member}: unknown UF {sig}")
            continue
        if layout == "stacked":
            cons.append(dict(layer=table, code=uf, issue="stacked_layout", value=member, action="read by blocks",
                             note=f"columns {', '.join(columns)} stacked as blocks of rows instead of side by side"))
        munis, others = defaultdict(dict), []
        for name, vals in rows:
            m = MUNI_RE.match(name)
            if not m:
                others.append((name, vals))
                continue
            if m.group(2) != sig:
                errors.append(f"{member}: row of another UF: {name}")
                continue
            for lab, v in vals.items():
                if lab in columns:
                    munis[m.group(1)][lab] = v
        n = 0
        for mname, vals in munis.items():
            if all(v in markers for v in vals.values()):
                continue  # created after 1991
            code = ctx["dtb_key"].get((uf, fold(mname)))
            alias = ctx["aliases"].get(sig, {}).get(mname)
            if code is None and alias:
                code = alias
                ctx["alias_hits"][code][folder.name] = mname
            if code is None:
                errors.append(f"{member}: '{mname}' has values but no DTB 1991 municipality")
                continue
            for lab, v in vals.items():
                vname = columns[lab]
                if v in markers:
                    cons.append(dict(layer=table, code=code, issue="missing_value", value=f"{member}: {v}",
                                     action=str(NO_DATA), note=f"{vname} ({lab}) marked as missing"))
                    v = NO_DATA
                elif not isinstance(v, float):
                    errors.append(f"{member}: '{mname}' {lab} = {v!r} is not a number")
                    continue
                if vname in out.get(code, {}):
                    errors.append(f"{member}: municipality {code} ({mname}) appears twice")
                out.setdefault(code, {})[vname] = v
            n += 1
        ctx["others"][(folder.name, uf)] = (member, others)
        logger.debug("  %s: %d municipalities (%s)", member, n, layout)
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(f"{folder.name}: {len(errors)} errors — nothing written")
    logger.info("  %-18s %d municipalities from %d spreadsheets", folder.name, len(out),
                len(list(folder.glob("*.zip"))))
    return out


def check_totals(folder: str, columns: dict, data: dict, ctx: dict, cons: list, logger):
    """Additive variables: the UF row must equal the sum of its municipalities. Rows of the other
    variables (e.g. Gini) are only checked for being UF or Brasil rows."""
    table, additive = ctx["table"], ctx["additive"]
    for (fname, uf), (member, others) in sorted(ctx["others"].items()):
        if fname != folder:
            continue
        codes = [c for c in data if c.startswith(uf)]
        for name, vals in others:
            if fold(name) == "BRASIL" or not any(isinstance(v, float) for v in vals.values()):
                continue
            if fold(name) != ctx["uf_names"].get(uf, fold(name)):
                cons.append(dict(layer=table, code=uf, issue="unexpected_row", value=f"{member}: {name}",
                                 action="ignored", note="row that is neither a municipality of the UF, the UF nor Brasil "
                                 f"({', '.join(f'{k} = {v}' for k, v in vals.items())})"))
                continue
            for lab, v in vals.items():
                vname = columns.get(lab)
                if vname not in additive or not isinstance(v, float):
                    continue
                s = sum(data[c][vname] for c in codes if data[c].get(vname, NO_DATA) != NO_DATA)
                if s != v:
                    cons.append(dict(layer=table, code=uf, issue="uf_total_differs", value=f"{member}: {lab} = {v:.0f}",
                                     action="kept municipal values", note=f"sum of the municipalities = {s:.0f}"))
                    logger.warning("%s: UF total %s %s = %.0f, municipalities add up to %.0f", member, name, lab, v, s)


def fmt(v: float) -> str:
    if v == NO_DATA:
        return str(NO_DATA)
    return str(int(v)) if float(v).is_integer() else repr(round(v, 6))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    with open(cfg["spec"], encoding="utf-8") as f:
        spec = json.load(f)
    table, variables = spec["table"], spec["variables"]
    out_dir = Path(cfg["output_folder"])
    out_path = out_dir / cfg["table_pattern"].format(NAME=table.upper())
    if out_path.exists() and not args.overwrite:
        sys.exit(f"{out_path} exists (use --overwrite)")

    dtb = read_dtb(cfg["dtb_xls"])
    codes = sorted(dtb["municipio_code"])
    sigla_uf = {s: u for u, s in cfg["unidade_sigla"].items()}
    cons = []
    ctx = dict(table=table, markers=cfg["missing_markers"], sigla_uf=sigla_uf, aliases=cfg["name_aliases"],
               dtb_key={(r.unidade_code, fold(r.municipio_name)): r.municipio_code for r in dtb.itertuples()},
               alias_hits=defaultdict(dict), others={},
               additive={v["name"] for v in variables if v.get("additive", True)},
               uf_names={})
    for alias_uf, names in cfg["name_aliases"].items():
        for name, code in names.items():
            if code not in set(codes) or code[:2] != sigla_uf.get(alias_uf):
                sys.exit(f"name_aliases: {alias_uf} '{name}' -> {code} is not a DTB 1991 municipality of the UF")

    # --- FRACAMO -----------------------------------------------------------------
    fr = read_fracamo(cfg["fracamo_txt"], cfg["fracamo_encoding"], {c[:6]: c for c in codes})
    lacking = sorted(set(codes) - set(fr))
    if lacking:
        sys.exit(f"FRACAMO lacks {len(lacking)} DTB municipalities: {lacking[:10]}")
    values = {c: {} for c in codes}
    for v in variables:
        if v["source"] == "fracamo":
            for c in codes:
                values[c][v["name"]] = fr[c][v["column"]]
    logger.info("  FRACAMO.TXT        %d municipalities, population %d", len(fr), sum(x["persons"] for x in fr.values()))

    # --- spreadsheets ------------------------------------------------------------------
    folders = defaultdict(dict)
    for v in variables:
        if v["source"] == "xls":
            folders[v["folder"]][v["column"]] = v["name"]
    # UF names as written on the UF total rows: the first non-Brasil row with values of each file
    for folder, columns in folders.items():
        data = read_xls_folder(Path(cfg["src_folder"]) / folder, columns, ctx, cons, logger)
        for (fname, uf), (_, others) in ctx["others"].items():
            if fname == folder and uf not in ctx["uf_names"]:
                first = [n for n, vals in others if fold(n) not in ("BRASIL", "") and
                         any(isinstance(x, float) for x in vals.values())]
                if first:
                    ctx["uf_names"][uf] = fold(first[0])
        check_totals(folder, columns, data, ctx, cons, logger)
        for c in codes:
            for vname in columns.values():
                v = data.get(c, {}).get(vname)
                if v is None:
                    cons.append(dict(layer=table, code=c, issue="missing_in_source", value=folder, action=str(NO_DATA),
                                     note=f"{vname}: municipality absent from the spreadsheet of its UF"))
                    v = NO_DATA
                values[c][vname] = v

    # --- checks ----------------------------------------------------------------------
    errors = []
    for total, groups in spec.get("sum_checks", {}).items():
        for c in codes:
            for parts in groups:
                got = [values[c][p] for p in parts]
                if NO_DATA in got:
                    errors.append(f"{c}: {parts} missing")
                elif sum(got) != values[c][total]:
                    errors.append(f"{c}: {' + '.join(parts)} = {sum(got):.0f}, {total} = {values[c][total]}")
        logger.info("Sum check: %s = %s in all %d municipalities", total,
                    " = ".join(" + ".join(g) for g in groups), len(codes))
    if errors:
        for e in errors[:50]:
            logger.error(e)
        sys.exit(f"{len(errors)} sum check errors — nothing written")
    names = dict(zip(dtb["municipio_code"], dtb["municipio_name"]))
    for code, by_folder in sorted(ctx["alias_hits"].items()):
        spell = sorted(set(by_folder.values()))
        cons.append(dict(layer=table, code=code, issue="name_differs_from_dtb", value=" / ".join(spell),
                         action="matched by name_aliases, population equal to FRACAMO",
                         note=f"DTB 1991 name: {names[code]} ({', '.join(sorted(by_folder))})"))

    # --- write -----------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    vnames = [v["name"] for v in variables]
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n")
        w.writerow(["municipio_code"] + vnames)
        for c in codes:
            w.writerow([c] + [fmt(values[c][n]) for n in vnames])
    tmp.replace(out_path)
    logger.info("Wrote %s (%d rows, %d variables)", out_path, len(codes), len(vnames))
    for n in vnames:
        miss = sum(values[c][n] == NO_DATA for c in codes)
        if miss:
            logger.info("  %s: %d municipalities at %d", n, miss, NO_DATA)

    cons_path = out_dir / cfg["consistency_name"]
    kept = merge_consistency(cons_path, cons, (table,))
    issues = defaultdict(int)
    for r in cons:
        issues[r["issue"]] += 1
    logger.info("Consistency: %d rows (+%d kept from other layers) -> %s %s", len(cons), kept, cons_path, dict(issues))


if __name__ == "__main__":
    main()

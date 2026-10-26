"""
Build the VARIABLES and CATEGORIES catalogs of the IBGE Census 1991 sample T1 tables.

VARIABLES   one row per V column of `microdados-domicilios` and `microdados-pessoas`:
              table;variable_id;variable_code;variable_name;source_name;dictionary_number;class;
              description_pt;description_en
            descriptions from variables_1991.json (curated from the IBGE dictionary); source_name
            is the IBGE mnemonic (e.g. IDADEANO), class comes from recode_1991.json.
            Optional aggregates_spec (aggregates_1991.json) adds the V columns of its table
            (agregados-municipios) with the descriptions and source names of the spec.
            Optional tracts_spec (tracts_1991.json) adds the V columns of every tract table (basico,
            domicilio, ... instrucao02); source_name = "<DVD folder>: <mnemonic>", class quantity.
CATEGORIES  one row per code of the coded columns:
              table;variable_name;variable_code;code;label_pt;label_en
            * categorical variables: the dictionary categories (labels -> labels_1991.json, which
              fixes accents / typos and gives the English); "NSA" entries are left out, since
              "not applicable" is -9999 in T1 (documented in FIELDS)
            * OCUPACAO and ATIVIDAD: the auxiliary code lists (cp1252), English in codes_1991.json
            * boolean variables: 1 / 0
            * microdados-domicilios.situacao_code: the dictionary categories of SITSET (also given to
              the tables of situacao_tables, e.g. basico)
            * tipo_labels: {table: {code: [pt, en]}} (tract type of basico)
            * extra labels of the recode table (e.g. MIUFPAIS 48)
Checks: every label must have a translation (the run stops otherwise); the V columns must match
the T1 headers when the tables exist; codes found in the data (profile_values.csv) without a
label are reported (codes_without_label.csv).

Usage:
    python build_variables.py --config build_variables.local.json
"""

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

REQUIRED_KEYS = ["recode", "dictionary_xls", "aux_folder", "variables_text", "labels", "codes",
                 "output_folder", "report_folder"]
V_RE = re.compile(r"^V\d+$")
AUX_RE = re.compile(r"^(\d{3})-\s*(.+?)\s*$")
TABLES = ["microdados-domicilios", "microdados-pessoas"]  # = recode table "layer"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("aux_files", {"OCUPACAO": "OCUPACAO PRINCIPAL.TXT", "ATIVIDAD": "CODIGO ATIVIDADE.txt"})
    cfg.setdefault("aux_encoding", "cp1252")
    cfg.setdefault("profile_values", None)
    cfg.setdefault("tables_folder", None)
    cfg.setdefault("aggregates_spec", None)
    cfg.setdefault("tracts_spec", None)
    cfg.setdefault("situacao_tables", [])
    cfg.setdefault("tipo_labels", {})
    cfg.setdefault("table_pattern", "IBGE_CENSO-1991_{NAME}_BRASIL_1991.csv")
    cfg.setdefault("variables_name", "IBGE_CENSO-1991_VARIABLES_X_X.csv")
    cfg.setdefault("categories_name", "IBGE_CENSO-1991_CATEGORIES_X_X.csv")
    cfg.setdefault("boolean_labels", {"1": ["Sim", "Yes"], "0": ["Não", "No"]})
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"variables_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_variables")
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


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("_comment", None)
    return d


def read_dictionary_categories(path: str) -> dict:
    """{variable: [(code, label), ...]} from the 'Layout' sheet (category rows follow their variable)."""
    d = pd.read_excel(path, header=None, dtype=str, engine="calamine").fillna("")
    out, cur = {}, None
    for _, r in d.iloc[2:].iterrows():
        if r[1].strip():
            cur = r[1].strip()
            out[cur] = []
        elif cur and r[2].strip() and r[2].strip() != "Categorías":
            out[cur].append((r[2].strip(), r[3].strip()))
    return out


def read_aux(path: Path, encoding: str) -> dict:
    out = {}
    with open(path, encoding=encoding) as f:
        for line in f:
            m = AUX_RE.match(line.strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out


def norm_code(code: str) -> str:
    """Codes as they appear in T1 (integers): '001' -> '1'."""
    return str(int(code)) if re.match(r"^\d+$", code) else code


def write_csv(path: Path, rows: list, fields: list):
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))

    rec = read_json(cfg["recode"])["variables"]
    texts = read_json(cfg["variables_text"])
    labels = read_json(cfg["labels"])
    codes = read_json(cfg["codes"])
    dcat = read_dictionary_categories(cfg["dictionary_xls"])
    errors = []

    # --- VARIABLES ----------------------------------------------------------------
    by_name = {e["name"]: (var, e) for var, e in rec.items()}
    vrows, vid = [], 0
    for table in TABLES:
        names = [e["name"] for e in rec.values() if e["layer"] == table and e["class"] != "code"]
        path = Path(cfg["tables_folder"] or ".") / cfg["table_pattern"].format(NAME=table.upper())
        if cfg["tables_folder"] and path.exists():
            with open(path, encoding="utf-8", newline="") as f:
                header = [c for c in next(csv.reader(f, delimiter=";")) if V_RE.match(c)]
            if header != names:
                errors.append(f"{table}: T1 header V columns differ from the recode table "
                              f"({sorted(set(header) ^ set(names))[:10]})")
            names = header
        else:
            logger.warning("%s: T1 table not found (%s); variable order taken from the recode table",
                           table, path)
        for name in names:
            var, e = by_name[name]
            if name not in texts:
                errors.append(f"no description for {name} ({var})")
                continue
            vid += 1
            pt, en = texts[name]
            vrows.append(dict(table=table, variable_id=vid, variable_code=f"{table}_{name}",
                              variable_name=name, source_name=var, dictionary_number=e["dictionary_number"],
                              **{"class": e["class"]}, description_pt=pt, description_en=en))
        logger.info("  %-21s %3d variables", table, len(names))
    if cfg["aggregates_spec"]:
        with open(cfg["aggregates_spec"], encoding="utf-8") as f:
            agg = json.load(f)
        table, names = agg["table"], [v["name"] for v in agg["variables"]]
        path = Path(cfg["tables_folder"] or ".") / cfg["table_pattern"].format(NAME=table.upper())
        if cfg["tables_folder"] and path.exists():
            with open(path, encoding="utf-8", newline="") as f:
                header = [c for c in next(csv.reader(f, delimiter=";")) if V_RE.match(c)]
            if header != names:
                errors.append(f"{table}: T1 header V columns differ from the spec ({header} vs {names})")
        else:
            logger.warning("%s: T1 table not found (%s); variable order taken from the spec", table, path)
        for v in agg["variables"]:
            vid += 1
            vrows.append(dict(table=table, variable_id=vid, variable_code=f"{table}_{v['name']}",
                              variable_name=v["name"], source_name=v["source_name"], dictionary_number="NA",
                              **{"class": v["class"]}, description_pt=v["description_pt"],
                              description_en=v["description_en"]))
        logger.info("  %-21s %3d variables", table, len(names))
    if cfg["tracts_spec"]:
        with open(cfg["tracts_spec"], encoding="utf-8") as f:
            tsp = json.load(f)
        folders = {k: v["folder"] for k, v in tsp["sets"].items()}
        for t in tsp["tables"]:
            table, names = t["table"], [v["name"] for v in t["variables"]]
            path = Path(cfg["tables_folder"] or ".") / cfg["table_pattern"].format(NAME=table.upper())
            if cfg["tables_folder"] and path.exists():
                with open(path, encoding="utf-8", newline="") as f:
                    header = [c for c in next(csv.reader(f, delimiter=";")) if V_RE.match(c)]
                if header != names:
                    errors.append(f"{table}: T1 header V columns differ from the tracts spec")
            else:
                logger.warning("%s: T1 table not found (%s); variable order taken from the spec", table, path)
            for v in t["variables"]:
                vid += 1
                vrows.append(dict(table=table, variable_id=vid, variable_code=f"{table}_{v['name']}",
                                  variable_name=v["name"], source_name=f"{folders[v['set']]}: {v['source_name']}",
                                  dictionary_number="NA", **{"class": "quantity"},
                                  description_pt=v["description_pt"], description_en=v["description_en"]))
            logger.info("  %-21s %3d variables", table, len(names))
    extra = sorted(set(texts) - {r["variable_name"] for r in vrows if r["table"] in TABLES})
    if extra:
        logger.warning("Descriptions without a T1 variable: %s", extra)

    # --- CATEGORIES -------------------------------------------------------------------
    crows = []

    def add(table, name, code, pt, en):
        crows.append(dict(table=table, variable_name=name, variable_code=f"{table}_{name}",
                          code=code, label_pt=pt, label_en=en))

    for var, e in rec.items():
        table, name, cls = e["layer"], e["name"], e["class"]
        if var in cfg["aux_files"]:
            aux = read_aux(Path(cfg["aux_folder"]) / cfg["aux_files"][var], cfg["aux_encoding"])
            fixes, en = codes[var]["pt_fixes"], codes[var]["en"]
            for c, pt in aux.items():
                if c not in en:
                    errors.append(f"{var} code {c}: no English label")
                add(table, name, norm_code(c), fixes.get(c, pt), en.get(c, ""))
            logger.info("  %-9s %3d codes from %s", var, len(aux), cfg["aux_files"][var])
            continue
        if cls == "boolean":
            for c, (pt, en) in cfg["boolean_labels"].items():
                add(table, name, c, pt, en)
            continue
        if var == "SITSET":
            for t in cfg["situacao_tables"]:
                for c, lab in dcat.get(var, []):
                    if lab != "NSA" and lab in labels:
                        add(t, "situacao_code", norm_code(c), *labels[lab])
            table, name = TABLES[0], "situacao_code"
        elif cls != "categorical":
            continue
        seen = set()
        for c, lab in dcat.get(var, []):
            if lab == "NSA":
                continue
            if lab not in labels:
                errors.append(f"{var}: label without translation: {lab!r}")
                continue
            c = norm_code(c)
            if c in seen:
                logger.warning("%s: code %s listed twice in the dictionary; first kept", var, c)
                continue
            seen.add(c)
            add(table, name, c, *labels[lab])
        for c, (pt, en) in (e.get("extra_labels") or {}).items():
            add(table, name, norm_code(c), pt, en)

    for t, codes_ in cfg["tipo_labels"].items():
        for c, (pt, en) in codes_.items():
            add(t, "tipo_code", c, pt, en)

    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(f"{len(errors)} errors — catalogs NOT written")

    # --- codes in the data without a label ----------------------------------------------------
    if cfg["profile_values"] and Path(cfg["profile_values"]).exists():
        have = {(r["variable_name"], r["code"]) for r in crows}
        name_of = {v: e["name"] for v, e in rec.items()}
        miss = Counter()
        with open(cfg["profile_values"], encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                v = r["variable"]
                e = rec.get(v)
                if not e or e["class"] != "categorical" or r["value"] == "<blank>":
                    continue
                if not any(n == name_of[v] for n, _ in have):
                    continue
                code = norm_code(r["value"]) if re.match(r"^\d+$", r["value"]) else r["value"]
                if (name_of[v], code) not in have:
                    miss[(v, name_of[v], code)] += int(r["count"])
        rows = [dict(variable=k[0], name=k[1], code=k[2], records=n) for k, n in sorted(miss.items())]
        write_csv(Path(cfg["report_folder"]) / "codes_without_label.csv", rows,
                  ["variable", "name", "code", "records"])
        logger.info("Codes in the data without a label: %d (%s)", len(rows),
                    dict(Counter(r["variable"] for r in rows)))

    out = Path(cfg["output_folder"])
    write_csv(out / cfg["variables_name"], vrows, list(vrows[0]))
    write_csv(out / cfg["categories_name"], crows, list(crows[0]))
    logger.info("Wrote %s (%d rows)", out / cfg["variables_name"], len(vrows))
    logger.info("Wrote %s (%d rows, %d variables)", out / cfg["categories_name"], len(crows),
                len({r["variable_code"] for r in crows}))


if __name__ == "__main__":
    main()

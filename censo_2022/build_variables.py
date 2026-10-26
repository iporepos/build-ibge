"""
Build the IBGE Census 2022 VARIABLES table (T2-style naming) for the sector tables.

One row per V column of every non-geography table of the layers catalog:

    table;variable_id;variable_code;variable_name;description_pt;description_en

  * variable_id    global sequence, in layers-catalog order, then T0 column order
  * variable_code  <table>_<variable_name>, e.g. demografia_V01006
  * variable_name  the V column as it appears in the T1 table, e.g. V01006
  * description_pt from the IBGE dictionaries (bracketed entorno descriptions are
                   harmonized to "a, b, c" with sentence case)
  * description_en translated with the glossary file (patterns + terms)

The V columns are read from the T0 CSV headers (authoritative); the dictionaries are
matched by code. Missing/extra codes and untranslated fragments are reported in the log.

Usage:
    python build_variables.py --config build_variables.local.json
"""

import argparse
import csv
import glob
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
V_RE = re.compile(r"^[Vv]\d+$")
MONTHS = {"janeiro": "January", "fevereiro": "February", "março": "March", "abril": "April",
          "maio": "May", "junho": "June", "julho": "July", "agosto": "August",
          "setembro": "September", "outubro": "October", "novembro": "November",
          "dezembro": "December"}

REQUIRED_KEYS = ["assemble_config", "dictionary_folder", "output_folder", "report_folder",
                 "glossary", "dictionaries"]


# ---------------------------------------------------------------------------
# Configuration / logging
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2022_VARIABLES_X_X.csv")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"variables_log_{timestamp}.txt"
    logger = logging.getLogger("censo2022_variables")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_path)
    return logger


# ---------------------------------------------------------------------------
# xlsx reading (stdlib only)
# ---------------------------------------------------------------------------

def read_xlsx(path: Path) -> dict:
    """Return {sheet name: [row dict keyed by header text]} using the first row as header."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.find(NS + "sheets")]
    out = {}
    for i, name in enumerate(names, 1):
        rows = []
        for r in ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml")).iter(NS + "row"):
            row = {}
            for c in r:
                col = re.match(r"[A-Z]+", c.get("r")).group()
                v = c.find(NS + "v")
                if v is None:
                    t = c.find(NS + "is")
                    val = "".join(x.text or "" for x in t.iter(NS + "t")) if t is not None else ""
                else:
                    val = shared[int(v.text)] if c.get("t") == "s" else v.text
                row[col] = val
            rows.append(row)
        if not rows:
            out[name] = []
            continue
        header = rows[0]
        out[name] = [{header[k].strip(): v for k, v in r.items() if k in header} for r in rows[1:]]
    return out


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def match_case(src: str, res: str) -> str:
    """Give the translation the same initial case as the Portuguese fragment."""
    if src[:1].isupper():
        return res[:1].upper() + res[1:]
    if src[:1].islower() and res[:1].isupper() and not res[:2].isupper():
        return res[:1].lower() + res[1:]
    return res


class Translator:
    def __init__(self, glossary: dict):
        self.patterns = [(re.compile(p, re.I), t) for p, t in glossary["patterns"]]
        self.terms = {k.lower(): v for k, v in glossary["terms"].items()}

    def fragment(self, text: str):
        """Translate one fragment; None if it cannot be translated completely."""
        key = " ".join(text.split()).lower()
        if key in self.terms:
            return self.terms[key]
        for rx, tpl in self.patterns:
            m = rx.match(key)
            if not m:
                continue
            ok = True

            def sub(mm):
                nonlocal ok
                tag = mm.group(1)
                if tag.startswith("mo"):
                    return MONTHS.get(m.group(int(tag[2:])), "?")
                g = m.group(int(tag))
                if re.fullmatch(r"\d+", g):
                    return g
                r = self.fragment(g)
                if r is None:
                    ok = False
                    return ""
                return r

            res = re.sub(r"\{(mo\d+|\d+)\}", sub, tpl)
            if ok:
                return res
        return None

    def sequence(self, tokens: list):
        """Greedy longest-match translation of a ', '-split description."""
        out, missing, i = [], [], 0
        while i < len(tokens):
            for j in range(min(len(tokens), i + 10), i, -1):
                res = self.fragment(", ".join(tokens[i:j]))
                if res is not None:
                    out.append(match_case(tokens[i], res))
                    i = j
                    break
            else:
                missing.append(tokens[i])
                i += 1
        return out, missing


def normalize_pt(text: str, style: str):
    """Return (pt description, list of translation units, joiner info)."""
    text = " ".join((text or "").replace("\xa0", " ").split()).rstrip(" ,")
    if style == "bracket":
        segs = [s.strip() for s in re.findall(r"\[([^\]]*)\]", text)] or [text]
        segs = [(s.lower() if s.isupper() else s) for s in segs]
        segs = [s[:1].upper() + s[1:] for s in segs]
        return ", ".join(segs), segs
    return text, [t.strip() for t in text.split(", ")]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    cfg = load_config(parser.parse_args().config)
    logger = setup_logging(Path(cfg["report_folder"]))

    with open(cfg["assemble_config"], encoding="utf-8") as f:
        acfg = json.load(f)
    with open(cfg["glossary"], encoding="utf-8") as f:
        tr = Translator(json.load(f))
    with open(acfg["layers_csv"], encoding="utf-8", newline="") as f:
        layers = [r["name"] for r in csv.DictReader(f, delimiter=";")
                  if r["theme"] not in acfg.get("skip_themes", ["geography", "auxiliary"])]
    layers.sort(key=lambda n: n != acfg.get("population_table", "basico"))

    dict_folder = Path(cfg["dictionary_folder"])
    input_folder = Path(acfg["input_folder"])
    xlsx_cache = {}

    out_rows, vid = [], 0
    n_missing_dict = n_untranslated = 0
    logger.info("=" * 60)
    logger.info("Census 2022 — VARIABLES table (%d tables)", len(layers))
    logger.info("=" * 60)

    for table in layers:
        spec = cfg["dictionaries"].get(table)
        src_pat = acfg["sources"].get(table)
        if not spec or not src_pat:
            logger.error("%s: no dictionary or source configured — skipped", table)
            continue
        matches = sorted(glob.glob(str(input_folder / src_pat)))
        if len(matches) != 1:
            logger.error("%s: source pattern matched %d files — skipped", table, len(matches))
            continue
        enc = acfg.get("encodings", {}).get(table, "utf-8")
        with open(matches[0], encoding=enc, newline="") as f:
            header = next(csv.reader(f, delimiter=";"))
        vars_data = ["V" + h[1:] for h in header[1:] if V_RE.match(h)]

        xpath = dict_folder / spec["file"]
        if xpath not in xlsx_cache:
            xlsx_cache[xpath] = read_xlsx(xpath)
        book = xlsx_cache[xpath]
        sheet = spec.get("sheet") or next(iter(book))
        flt = spec.get("filter", {})
        entries = {}
        for r in book[sheet]:
            if all(r.get(k) == v for k, v in flt.items()):
                code = (r.get(spec["code"]) or "").strip().upper()
                if code:
                    entries[code] = r.get(spec["description"], "")
        style = spec.get("style", "plain")

        only_data = [v for v in vars_data if v not in entries]
        only_dict = [c for c in entries if c not in set(vars_data)]
        logger.info("%-20s data V=%d  dictionary=%d  missing in dict=%d  extra in dict=%d",
                    table, len(vars_data), len(entries), len(only_data), len(only_dict))
        if only_data:
            logger.warning("  in data but not in dictionary: %s", only_data[:10])
        if only_dict:
            logger.warning("  in dictionary but not in data: %s", only_dict[:10])

        t_missing = 0
        for v in vars_data:
            vid += 1
            desc = entries.get(v)
            if desc is None:
                n_missing_dict += 1
                pt = en = ""
            else:
                pt, units = normalize_pt(desc, style)
                if style == "bracket":
                    parts, miss = [], []
                    for u in units:
                        r = tr.fragment(u)
                        if r is None:
                            miss.append(u)
                        else:
                            parts.append(match_case(u, r))
                else:
                    parts, miss = tr.sequence(units)
                if miss:
                    t_missing += 1
                    n_untranslated += 1
                    logger.warning("  untranslated %s_%s: %s", table, v, miss)
                    en = ""
                else:
                    en = ", ".join(parts)
            out_rows.append([table, str(vid), f"{table}_{v}", v, pt, en])
        if t_missing:
            logger.warning("  %d variable(s) of %s without complete translation", t_missing, table)

    out_dir = Path(cfg["output_folder"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / cfg["name_pattern"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n")
        w.writerow(["table", "variable_id", "variable_code", "variable_name",
                    "description_pt", "description_en"])
        w.writerows(out_rows)

    logger.info("=" * 60)
    logger.info("Wrote %d variables to %s", len(out_rows), out_path)
    logger.info("Without dictionary entry: %d   Without full translation: %d",
                n_missing_dict, n_untranslated)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

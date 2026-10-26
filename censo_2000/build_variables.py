"""
Build the IBGE Census 2000 VARIABLES catalog for the T1 sector tables.

One row per V column of every sector table:

    table;variable_id;variable_code;variable_name;description_pt;description_en

  * variable_id    global sequence in assemble_tables.json "sources" order, then column order
  * variable_code  <table>_<variable_name>, e.g. domicilio_V0001
  * variable_name  the V column of the T1 table (basico Var01..Var14 are V01..V14 in T1)
  * description_pt from the IBGE documentation (section 6, "Relação das variáveis das planilhas"),
                   extracted with `pdftotext -layout`; IBGE notes ("Nota: ...") are appended in
                   parentheses. The basico descriptions (whose layout does not parse) live in the
                   config (basico_descriptions).
  * description_en translated with the glossary file (ordered phrase substitutions), when given;
                   a description with leftover Portuguese is left empty and listed in the report
                   (untranslated.csv). Basico descriptions carry their own EN text in the config.

The V columns are read from the T1 CSV headers (authoritative); missing/extra descriptions are
reported and stop the build unless --allow-gaps.

Usage:
    python build_variables.py --config build_variables.local.json
    python build_variables.py --config build_variables.local.json --dump-pt descriptions_pt.csv
"""

import argparse
import csv
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["assemble_config", "documentation_pdf", "tables_folder", "output_folder",
                 "report_folder", "basico_descriptions"]
V_RE = re.compile(r"^V\d+$")
SECTION_RE = re.compile(r"^6\.\d+\s+Arquivo .*\(planilha\s+([A-Za-z]+\d?)_UF\.xls\)")
VAR_RE = re.compile(r"^\s{1,10}(V\d{4})\s+(\S.*)$")
CONT_RE = re.compile(r"^\s{10,}(\S.*)$")
NOISE_RE = re.compile(r"^\s*(\d+|IBGE\s+Censo Demográfico 2000|Agregado por Setores.*|Nome da|"
                      r"variável|Descrição da variável|Nome da variável\s+Descrição da variável)\s*$")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("name_pattern", "IBGE_CENSO-2000_VARIABLES_X_X.csv")
    cfg.setdefault("table_pattern", "IBGE_CENSO-2000_{NAME}_BRASIL_2000.csv")
    cfg.setdefault("glossary", None)
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = report_folder / f"variables_log_{timestamp}.txt"
    logger = logging.getLogger("censo2000_variables")
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
# Documentation parsing
# ---------------------------------------------------------------------------

def pdf_text(pdf: str) -> list:
    res = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, check=True)
    return res.stdout.decode("utf-8").splitlines()


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("não- ", "não-")  # line-break hyphenation in the PDF
    return re.sub(r"\s+([,.;)])", r"\1", text)


def parse_documentation(lines: list, prefix_to_table: dict, logger) -> dict:
    """{(table, V####): description_pt} from section 6 of the documentation."""
    out = {}
    table = None
    cur = None  # [key, description parts, note parts]

    def flush():
        if cur is None:
            return
        key, desc, note = cur
        d = clean(" ".join(desc))
        if note:
            d += f" ({clean(' '.join(note))})"
        if key in out and out[key] != d:
            logger.warning("  %s %s described twice; first kept", *key)
        out.setdefault(key, d)

    for line in lines:
        s = line.strip()
        m = SECTION_RE.match(s)
        if m:
            flush()
            cur = None
            table = prefix_to_table.get(m.group(1))
            continue
        if table is None or not s or NOISE_RE.match(line):
            continue
        m = VAR_RE.match(line)
        if m:
            flush()
            cur = [(table, m.group(1)), [m.group(2)], []]
            continue
        if cur is None:
            continue
        m = CONT_RE.match(line)
        if m:
            t = m.group(1)
            if t.startswith("Nota:") or cur[2]:
                cur[2].append(t)
            else:
                cur[1].append(t)
        else:
            # a non-indented line ends the current variable (e.g. Cod_setor rows)
            flush()
            cur = None
    flush()
    return out


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

class Translator:
    """Glossary translation by ordered phrase substitution.

    The glossary holds "phrases": [[regex, replacement], ...] applied in order (case-insensitive,
    Python re.sub replacement syntax), so longer / reordering phrases come first and single
    words last. The result is then checked for leftover Portuguese (glossary "residual_pt"
    regex); a description that still has Portuguese is reported and left untranslated."""

    def __init__(self, glossary: dict):
        self.phrases = [(re.compile(p, re.I), t) for p, t in glossary["phrases"]]
        self.residual = re.compile(glossary["residual_pt"], re.I)
        self.missing = {}

    def translate(self, text: str) -> str:
        res = text
        for rx, rep in self.phrases:
            res = rx.sub(rep, res)
        res = re.sub(r"\s+", " ", res).strip()
        return res[:1].upper() + res[1:]

    def __call__(self, text: str) -> str:
        res = self.translate(text)
        left = self.residual.findall(res)
        if left:
            self.missing[text] = res
            return ""
        return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def t1_columns(path: Path) -> list:
    with open(path, encoding="utf-8", newline="") as f:
        return [c for c in next(csv.reader(f, delimiter=";")) if V_RE.match(c)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--allow-gaps", action="store_true",
                        help="Write the catalog even if some variables have no description")
    parser.add_argument("--dump-pt", help="Also write the distinct PT descriptions to this CSV "
                                          "(helper for building the glossary)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    with open(cfg["assemble_config"], encoding="utf-8") as f:
        acfg = json.load(f)
    sources = acfg["sources"]
    basico = acfg.get("basico_table", "basico")
    prefix_to_table = {v: k for k, v in sources.items()}

    logger.info("Parsing %s", cfg["documentation_pdf"])
    docs = parse_documentation(pdf_text(cfg["documentation_pdf"]), prefix_to_table, logger)
    fixed_en = {}
    for var, desc in cfg["basico_descriptions"].items():
        docs[(basico, var)] = desc["pt"]
        fixed_en[(basico, var)] = desc["en"]
    logger.info("  %d descriptions found", len(docs))

    tr = None
    if cfg["glossary"]:
        with open(cfg["glossary"], encoding="utf-8") as f:
            tr = Translator(json.load(f))

    rows, gaps, vid = [], [], 0
    seen = set()
    for table in sources:
        path = Path(cfg["tables_folder"]) / cfg["table_pattern"].format(NAME=table.upper())
        if not path.exists():
            logger.error("T1 table not found: %s", path)
            gaps.append((table, "*"))
            continue
        cols = t1_columns(path)
        for v in cols:
            vid += 1
            seen.add((table, v))
            d = docs.get((table, v))
            if d is None:
                gaps.append((table, v))
                d = ""
            rows.append(dict(table=table, variable_id=vid, variable_code=f"{table}_{v}",
                             variable_name=v, description_pt=d,
                             description_en=fixed_en.get((table, v)) or (tr(d) if (tr and d)
                                                                         else "")))
        logger.info("  %-14s %4d variables", table, len(cols))
    extra = sorted(set(docs) - seen)
    if extra:
        logger.warning("%d documented variables not in the T1 tables: %s", len(extra), extra[:10])
    if gaps:
        logger.error("%d variables without description: %s", len(gaps), gaps[:20])

    if args.dump_pt:
        with open(args.dump_pt, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\n")
            w.writerow(["description_pt", "n"])
            counts = {}
            for r in rows:
                counts[r["description_pt"]] = counts.get(r["description_pt"], 0) + 1
            for d, n in sorted(counts.items()):
                w.writerow([d, n])
        logger.info("Distinct PT descriptions written to %s", args.dump_pt)

    if tr is not None:
        n_en = sum(1 for r in rows if r["description_en"])
        logger.info("EN translated: %d / %d (%d distinct descriptions untranslated)",
                    n_en, len(rows), len(tr.missing))
        rep = Path(cfg["report_folder"]) / "untranslated.csv"
        with open(rep, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\n")
            w.writerow(["description_pt", "partial_en"])
            for d, partial in sorted(tr.missing.items()):
                w.writerow([d, partial])

    if gaps and not args.allow_gaps:
        sys.exit("Catalog NOT written (use --allow-gaps to write anyway)")
    out = Path(cfg["output_folder"]) / cfg["name_pattern"]
    tmp = out.with_name(out.name + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(out)
    logger.info("Wrote %s (%d rows)", out, len(rows))


if __name__ == "__main__":
    main()

"""
Assemble the T1 of one PNSB edition (PNSB-<year>) from the SIDRA downloads (download_pnsb.py): one wide CSV
per SIDRA table, one row per municipality of the DTB edition of the year, one column per variable and
combination of categories. One config per year, like the censuses: every year has its own list of tables and
columns.

Column names: v<variable> followed by _<category> for each classification of the table, in SIDRA order
(e.g. v10676_48278 = variable 10676, category 48278 of classification 964). The VARIABLES catalog describes
every column (variable, unit, classifications and categories by name); the TABLES catalog every file.

Values: SIDRA symbols '-' (zero) -> 0, 'X' (hidden) -> -1, '..' (not applicable) and '...' (not available)
-> -9999; numbers as published (integers stay integers). In the municipal PNSB tables the "Municípios com ..."
variables count the municipality itself: 1 or 0.

Rows: SIDRA lists today's municipalities for every year; rows are the municipalities of the DTB edition of the
year (config edition). SIDRA municipalities outside the edition are dropped (logged in CONSISTENCY when they
carry a value); municipalities of the edition missing from SIDRA (tables of a sub-universe, e.g. municipalities
with the service) get -9999 (one CONSISTENCY row per file).

Checks: every downloaded file is in the manifest with the same sha256 and row count; every (municipality,
column) value exactly once; values numeric or a known symbol; every table has a row in the hand-kept LAYERS
catalog (layers_catalog); the count variables ("Municípios ...", unit Unidades) are 0/1 or a symbol at
municipal level (else logged). A failed check stops with exit 1.

Outputs (output_folder): IBGE_PNSB-<year>_T<table>_BRASIL_<year>.csv, IBGE_PNSB-<year>_VARIABLES_X_X.csv,
IBGE_PNSB-<year>_TABLES_X_X.csv, IBGE_PNSB-<year>_CONSISTENCY_X_X.csv (rows of the tables assembled replace
their previous rows; other rows kept). Log and reports in report_folder.

CONFIG (JSON, --config)
  required:
    sidra_folder    the SIDRA SRC folder of the edition (meta/, values/, manifest)
    output_folder   T1 ASSETS folder
    report_folder   log folder
    dtb_quadro      DTB T1 municipal list (IBGE_DTB_QUADRO-MUNICIPIOS_BRASIL_1940-2022.csv)
    year            survey year (2000, 2008, 2017)
    edition         DTB edition of the rows
  optional:
    layers_catalog  hand-kept LAYERS of the edition, every table must be in it (default: no check)
    survey          default "SB"
    level           default "N6"
    manifest_name   default "SIDRA_SB_MANIFEST.csv"
    name_pattern    default "IBGE_PNSB-{YEAR}_T{TABLE}_BRASIL_{YEAR}.csv"
    catalog_pattern default "IBGE_PNSB-{YEAR}_{NAME}_X_X.csv" (NAME = VARIABLES, TABLES, CONSISTENCY)
    symbols         default {"-": 0, "X": -1, "..": -9999, "...": -9999}

Usage:
    python assemble_pnsb.py --config assemble_pnsb_2017.local.json [--overwrite] [--tables 7460]
"""

import argparse
import csv
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REQUIRED_KEYS = ["sidra_folder", "output_folder", "report_folder", "dtb_quadro", "year", "edition"]
CONSISTENCY_COLUMNS = ["layer", "code", "issue", "value", "action", "note"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("layers_catalog", None)
    cfg.setdefault("survey", "SB")
    cfg.setdefault("level", "N6")
    cfg.setdefault("manifest_name", f"SIDRA_{cfg['survey']}_MANIFEST.csv")
    cfg.setdefault("name_pattern", "IBGE_PNSB-{YEAR}_T{TABLE}_BRASIL_{YEAR}.csv")
    cfg.setdefault("catalog_pattern", "IBGE_PNSB-{YEAR}_{NAME}_X_X.csv")
    cfg.setdefault("symbols", {"-": 0, "X": -1, "..": -9999, "...": -9999})
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"assemble_pnsb_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("assemble_pnsb")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def write_csv(df: pd.DataFrame, path: Path) -> None:
    part = path.with_name(path.name + ".part")
    df.to_csv(part, sep=";", index=False, encoding="utf-8", lineterminator="\n")
    part.replace(path)


def columns_of(meta: dict) -> list:
    """[(column, variable, [(classification, category)])] in SIDRA order."""
    combos = [[]]
    for c in meta["classificacoes"]:
        combos = [k + [(c, cat)] for k in combos for cat in c["categorias"]]
    return [("v" + str(v["id"]) + "".join(f"_{cat['id']}" for _, cat in k), v, k) for v in meta["variaveis"] for k in combos]


def to_number(series: pd.Series, symbols: dict) -> tuple:
    """SIDRA text values -> numbers; returns (values, mask of unknown text)."""
    sym = series.map(symbols)
    num = pd.to_numeric(series.where(sym.isna()), errors="coerce")
    bad = sym.isna() & num.isna()
    return sym.fillna(num), bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="replace existing T1 files")
    ap.add_argument("--tables", nargs="*", type=int, help="only these tables")
    args = ap.parse_args()

    cfg = load_config(args.config)
    src, out, rep = Path(cfg["sidra_folder"]), Path(cfg["output_folder"]), Path(cfg["report_folder"])
    logger = setup_logging(rep)
    out.mkdir(parents=True, exist_ok=True)
    year, edition = int(cfg["year"]), int(cfg["edition"])
    catalog = {n: out / cfg["catalog_pattern"].format(YEAR=year, NAME=n) for n in ("VARIABLES", "TABLES", "CONSISTENCY")}

    quadro = pd.read_csv(cfg["dtb_quadro"], sep=";", dtype=str, keep_default_na=False,
                         usecols=["edition", "municipio_code", "municipio_name"])
    ed = quadro[quadro["edition"] == str(edition)].set_index("municipio_code")["municipio_name"].sort_index()
    if ed.empty:
        sys.exit(f"DTB edition {edition} not in {cfg['dtb_quadro']}")
    with open(src / cfg["manifest_name"], encoding="utf-8", newline="") as f:
        manifest = {int(r["table"]): r for r in csv.DictReader(f, delimiter=";")
                    if r["level"] == cfg["level"] and int(r["year"]) == year}
    todo = sorted(t for t in manifest if not args.tables or t in args.tables)
    logger.info("PNSB %d: %d tables in the manifest, rows = DTB %d (%d municipalities)", year, len(todo), edition, len(ed))
    known_layers = None
    if cfg["layers_catalog"]:
        known_layers = set(pd.read_csv(cfg["layers_catalog"], sep=";", dtype=str, keep_default_na=False)["name"])

    var_rows, tab_rows, cons, errors, skipped = [], [], [], [], 0
    for table in todo:
        m = manifest[table]
        layer = f"t{table}"
        if known_layers is not None and layer not in known_layers:
            errors.append(f"T{table}: layer {layer} not in {cfg['layers_catalog']}")
            continue
        path = src / m["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != m["sha256"]:
            errors.append(f"T{table}: sha256 differs from the manifest")
            continue
        meta = json.loads((src / "meta" / f"T{table}.json").read_text(encoding="utf-8"))
        name = cfg["name_pattern"].format(TABLE=table, YEAR=year)
        cols = columns_of(meta)
        dest = out / name
        if dest.is_file() and not args.overwrite:
            skipped += 1
            logger.debug("%s exists, kept (use --overwrite)", name)
        raw = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, compression="gzip")
        if len(raw) != int(m["rows"]):
            errors.append(f"T{table}: {len(raw)} rows, manifest {m['rows']}")
            continue
        raw["column"] = "v" + raw["variable"]
        for c in meta["classificacoes"]:
            raw["column"] += "_" + raw[f"c{c['id']}"]
        unknown = set(raw["column"]) - {c for c, _, _ in cols}
        if unknown:
            errors.append(f"T{table}: columns not in the metadata: {sorted(unknown)[:5]}")
            continue
        if raw.duplicated(["municipio_code", "column"]).any():
            errors.append(f"T{table}: duplicate municipality x column")
            continue
        raw["num"], bad = to_number(raw["value"], cfg["symbols"])
        if bad.any():
            errors.append(f"T{table}: unknown values {sorted(set(raw.loc[bad, 'value']))[:5]}")
            continue
        wide = raw.pivot(index="municipio_code", columns="column", values="num")
        wide = wide[[c for c, _, _ in cols if c in wide.columns]]
        extra = wide.index.difference(ed.index)
        out_ed = raw[raw["municipio_code"].isin(extra)]
        has_value = sorted(set(out_ed.loc[out_ed["value"].map(cfg["symbols"]) != -9999, "municipio_code"]))
        for c in has_value:
            cons.append(dict(layer=layer, code=c, issue="not_in_dtb_edition", value=str(edition),
                             action="dropped", note=f"Município do SIDRA fora da DTB {edition} com valores"))
        missing = ed.index.difference(wide.index)
        if len(missing):  # tables of a sub-universe (e.g. municipalities with the service): one row per file
            cons.append(dict(layer=layer, code="NA", issue="not_in_sidra", value=str(len(missing)),
                             action="-9999", note=f"{len(missing)} municípios da DTB {edition} ausentes do SIDRA: "
                             + " ".join(missing)))
        wide = wide.reindex(ed.index).fillna(-9999)
        vtypes = {}
        for c in wide.columns:  # integers stay integers, column by column
            vtypes[c] = "integer" if (wide[c] % 1 == 0).all() else "real"
            if vtypes[c] == "integer":
                wide[c] = wide[c].astype("int64")
        for col, v, k in cols:
            if col not in wide.columns:
                continue
            var_rows.append(dict(
                layer=layer, column=col, table_code=table, variable_code=v["id"], variable_name=v["nome"],
                unit=v["unidade"], classifications=" | ".join(c["nome"] for c, _ in k) or "NA",
                categories=" | ".join(cat["nome"] for _, cat in k) or "NA",
                description_pt=v["nome"] + "".join(f"; {c['nome']}: {cat['nome']}" for c, cat in k),
                value_type=vtypes[col]))
            if v["unidade"] == "Unidades" and v["nome"].startswith("Municípios"):  # count variables: 0/1
                vals = set(wide[col].unique()) - {0, 1, -1, -9999}
                if vals:
                    logger.warning("%s %s: count variable with values %s", name, col, sorted(vals)[:5])
        wide.index.name = "municipio_code"
        wide = pd.concat([ed.rename("municipio_name"), wide], axis=1).rename_axis("municipio_code").reset_index()
        if not dest.is_file() or args.overwrite:
            write_csv(wide, dest)
        per = meta["periodicidade"]
        tab_rows.append(dict(layer=layer, file=name, table_code=table, table_name=meta["nome"],
                             subject=meta.get("assunto", "NA"),
                             period=str(per["inicio"]) if per["inicio"] == per["fim"] else f"{per['inicio']}-{per['fim']}",
                             edition=edition, rows=len(wide), columns=len(wide.columns) - 2,
                             sidra_municipalities=int(m["municipalities"]), dropped=len(extra),
                             dropped_with_values=len(has_value), missing=len(missing), url=meta.get("URL", "NA")))
        logger.info("%s: %d rows, %d columns (dropped %d not in DTB %d, %d with values; %d missing)", name,
                    len(wide), len(wide.columns) - 2, len(extra), edition, len(has_value), len(missing))

    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    # catalogs: rows of the assembled tables replace their previous rows
    done = {f"t{t}" for t in todo}

    def merge(path: Path, rows: list, columns=None) -> pd.DataFrame:
        new = pd.DataFrame(rows, columns=columns).astype(str)
        if path.is_file():
            old = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
            new = pd.concat([old[~old["layer"].isin(done)], new], ignore_index=True).fillna("NA")
        order = new["layer"].str[1:].astype(int)
        return new.iloc[order.argsort(kind="stable")]

    if todo:
        write_csv(merge(catalog["VARIABLES"], var_rows), catalog["VARIABLES"])
        write_csv(merge(catalog["TABLES"], tab_rows), catalog["TABLES"])
        write_csv(merge(catalog["CONSISTENCY"], cons, CONSISTENCY_COLUMNS), catalog["CONSISTENCY"])
        write_csv(pd.DataFrame(tab_rows), rep / f"assemble_pnsb_{year}_tables.csv")
    logger.info("Done: %d files (%d existing kept), %d variables rows, %d consistency rows",
                len(tab_rows), skipped, len(var_rows), len(cons))


if __name__ == "__main__":
    main()

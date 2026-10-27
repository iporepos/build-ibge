"""
Assemble the T1 of ATLASSAN from the GeoServer harvest (download_atlas.py): the values of the maps
of the Atlas de Saneamento 2021, split first by chapter of the Atlas (the first two digits of the map code)
and then by territorial level: one wide CSV per chapter and level, one column per map value (as<code>, the
IBGE column name), keyed by the codes of the DTB (municipio_code, unidade_code, regiaointermediaria_code) or by
ISO3 (pais_code). Layers of no level (REGIC flows and cities, hydrographic regions, intake points, layers
without a key) are written as they are, one file each, under their chapter.

Level of a layer: the first level of the config with one of its source keys among the layer's columns and
every key value of the expected number of digits. Same column in several layers of one level (the maps cut
by population size class) -> union, values must agree where layers overlap. "combine": complementary maps
merged into one column (e.g. municipalities with / without water network), they must not overlap.
"duplicates": columns identical to another one (checked), dropped. Codes in "drop_codes" (the lagoons) are
dropped. Rows: the municipalities of the DTB edition of the level (municipal levels), else the codes found.
A municipality absent from a layer has no value in its column.

Values as published: a column whose values are all numbers is numeric (empty -> -9999); otherwise text (the
category as written by IBGE, empty -> NA). Scales are not changed (some layers give shares 0-1, others 0-100:
see VARIABLES scale). Labels of the categories and the class breaks of the maps are in CATEGORIES (from the
legends).

Checks: every layer of the manifest has its attribute file with the sha256 and row count of the manifest;
keys unique per layer; no conflict between overlapping layers; duplicates really identical; every municipal
code in the DTB edition (else stop). Failed checks stop with exit 1.

Outputs (output_folder): IBGE_ATLASSAN_CAP<chapter>-<LEVEL>_BRASIL_<year>.csv, IBGE_ATLASSAN_CAP<chapter>-<layer
code>_BRASIL_<year>.csv (layers of no level); layer names cap<chapter>-<level|code>; IBGE_ATLASSAN_VARIABLES_X_X.csv, IBGE_ATLASSAN_CONSISTENCY_X_X.csv,
IBGE_ATLASSAN_CATEGORIES_X_X.csv. Log in report_folder.

CONFIG (JSON, --config)
  required:
    geoserver_folder  the harvest SRC folder (attributes/, schema/, legend/, iso/, manifest)
    output_folder     T1 ASSETS folder
    report_folder     log folder
    dtb_quadro        DTB T1 municipal list (edition;municipio_code;municipio_name;...)
    year              reference year of the atlas (2017), in the file names and the year column
    levels            [{name, source_keys, key, digits (null = any), optional edition (DTB, municipal levels),
                        year (default: year), names ({source column: T1 column}, kept columns)}]
  optional:
    prefix            layer prefix (default "AtlasSan2021_")
    combine           {column: [columns merged into it]}
    duplicates        {column: column it repeats}
    drop_codes        codes dropped from the municipal levels (default [])
    title_fixes       {column: title} replacing a wrong map title in VARIABLES
    skip_layers       {layer code: reason} layers not assembled (repeats of another layer or dataset), logged
    manifest_name     default "GEOSERVER_MANIFEST.csv"
    name_pattern      default "IBGE_ATLASSAN_{NAME}_BRASIL_{YEAR}.csv"
    variables_name, categories_name, consistency_name  (defaults IBGE_ATLASSAN_<X>_X_X.csv)

Usage:
    python assemble_atlas.py --config assemble_atlas.local.json [--overwrite]
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

REQUIRED_KEYS = ["geoserver_folder", "output_folder", "report_folder", "dtb_quadro", "year", "levels"]
CONSISTENCY_COLUMNS = ["layer", "code", "issue", "value", "action", "note"]
VALUE = re.compile(r"^as\d")
SIZE_CLASS = re.compile(r"\s*(em|nos) [Mm]unicípios (com|de) .*habitantes")
NUMBER = re.compile(r"-?\d+(\.\d+)?([eE]-?\d+)?")
ISO = {"gmd": "http://www.isotc211.org/2005/gmd", "gco": "http://www.isotc211.org/2005/gco"}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("prefix", "AtlasSan2021_")
    cfg.setdefault("combine", {})
    cfg.setdefault("duplicates", {})
    cfg.setdefault("drop_codes", [])
    cfg.setdefault("title_fixes", {})
    cfg.setdefault("skip_layers", {})
    cfg.setdefault("manifest_name", "GEOSERVER_MANIFEST.csv")
    cfg.setdefault("name_pattern", "IBGE_ATLASSAN_{NAME}_BRASIL_{YEAR}.csv")
    cfg.setdefault("variables_name", "IBGE_ATLASSAN_VARIABLES_X_X.csv")
    cfg.setdefault("categories_name", "IBGE_ATLASSAN_CATEGORIES_X_X.csv")
    cfg.setdefault("consistency_name", "IBGE_ATLASSAN_CONSISTENCY_X_X.csv")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"assemble_atlas_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("assemble_atlas")
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


def norm_code(v: str) -> str:
    """Key as text: '1100015.0' -> '1100015'; ISO3 and other text unchanged."""
    v = v.strip()
    return str(int(float(v))) if NUMBER.fullmatch(v) else v


def level_codes(df: pd.DataFrame, lv: dict, k: str) -> tuple:
    """(frame, codes) of a layer at a level: exact duplicate rows dropped when the level says so (countries cut
    into several polygons), codes still repeated made unique with the "disambiguate" column (code|name)."""
    if lv.get("dedupe_rows"):
        df = df.drop_duplicates()
    codes = df[k].map(norm_code)
    d = lv.get("disambiguate")
    if d and d in df.columns:
        rep = codes.duplicated(keep=False)
        codes = codes.where(~rep, codes + "|" + df[d])
    return df, codes


def level_of(df: pd.DataFrame, levels: list):
    """First level with a source key whose codes have the expected digits and are unique in the layer."""
    for lv in levels:
        for k in lv["source_keys"]:
            if k in df.columns:
                _, codes = level_codes(df, lv, k)
                digits = lv["digits"] is None or codes.str.fullmatch(rf"\d{{{lv['digits']}}}").all()
                if digits and not codes.duplicated().any():
                    return lv, k
    return None, None


def iso_texts(path: Path) -> dict:
    if not path.is_file():
        return {}
    root = ET.parse(path).getroot()

    def first(xp):
        el = root.find(xp, ISO)
        return re.sub(r"\s+", " ", el.text).strip() if el is not None and el.text else "NA"
    return dict(iso_title=first(".//gmd:citation/gmd:CI_Citation/gmd:title/gco:CharacterString"),
                iso_date=first(".//gmd:citation/gmd:CI_Citation/gmd:date/gmd:CI_Date/gmd:date/gco:Date"),
                iso_abstract=first(".//gmd:abstract/gco:CharacterString"))


def legend_rows(path: Path, layer: str) -> list:
    """CATEGORIES rows from a GetLegendGraphic JSON: one row per category code or per class break."""
    if not path.is_file():
        return []
    out = []
    for leg in json.loads(path.read_text(encoding="utf-8")).get("Legend", []):
        for i, r in enumerate(leg.get("rules", []), 1):
            label = (r.get("title") or r.get("name") or "").strip()
            flt = r.get("filter", "")
            base = dict(source_layer=layer, rule=i, label=label, filter=flt or "NA")
            m = re.fullmatch(r"\[(\w+) BETWEEN '([^']*)' AND '([^']*)'\]", flt)
            if m:
                out.append(dict(base, column=m[1], kind="class", code="NA", lower=m[2], upper=m[3]))
                continue
            m = re.fullmatch(r"\[(\w+) = '([^']*)'\]", flt)
            if m:
                out.append(dict(base, column=m[1], kind="category", code=m[2], lower="NA", upper="NA"))
                continue
            m = re.fullmatch(r"\[(\w+) IN \((.*)\)\]", flt)
            if m:
                for code in dict.fromkeys(re.findall(r"'([^']*)'", m[2])):
                    out.append(dict(base, column=m[1], kind="category", code=code, lower="NA", upper="NA"))
                continue
            m = re.fullmatch(r"\[(\w+) ([<>]=?) '([^']*)'\]", flt)
            col = m[1] if m else "NA"
            out.append(dict(base, column=col, kind="other" if flt else "symbol", code="NA", lower="NA", upper="NA"))
    return out


def typed(s: pd.Series) -> tuple:
    """(series, value_type): numeric when every non-empty value is a number."""
    ne = s[s.notna() & (s != "")]
    if len(ne) and ne.map(lambda v: bool(NUMBER.fullmatch(v))).all():
        num = pd.to_numeric(s.replace("", None), errors="coerce").fillna(-9999)
        if (num % 1 == 0).all():
            return num.astype("int64"), "integer"
        return num, "real"
    return s.replace("", None).fillna("NA"), "text"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="replace existing T1 files")
    args = ap.parse_args()

    cfg = load_config(args.config)
    src, out, rep = Path(cfg["geoserver_folder"]), Path(cfg["output_folder"]), Path(cfg["report_folder"])
    logger = setup_logging(rep)
    out.mkdir(parents=True, exist_ok=True)
    year = int(cfg["year"])
    with open(src / cfg["manifest_name"], encoding="utf-8", newline="") as f:
        manifest = {r["layer"]: r for r in csv.DictReader(f, delimiter=";")}
    quadro = pd.read_csv(cfg["dtb_quadro"], sep=";", dtype=str, keep_default_na=False,
                         usecols=["edition", "municipio_code", "municipio_name"])
    errors, cons, var_rows, cat_rows = [], [], [], []
    per_level = {}   # level name+year -> {column: [(layer, frame indexed by key)]}
    loose = []       # (layer, frame)
    titles = {}

    for layer in sorted(manifest):
        m = manifest[layer]
        path = src / "attributes" / f"{layer}.csv"
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != m["sha256"]:
            errors.append(f"{layer}: sha256 differs from the manifest")
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False).drop(columns=["FID"], errors="ignore")
        if len(df) != int(m["rows"]):
            errors.append(f"{layer}: {len(df)} rows, manifest {m['rows']}")
            continue
        code = layer[len(cfg["prefix"]):]
        if code in cfg["skip_layers"]:
            cons.append(dict(layer=f"cap{code[:2]}-{code.lower()}", code="NA", issue="skipped_layer", value=layer, action="dropped",
                             note=cfg["skip_layers"][code]))
            continue
        meta = dict(title=m["title"], **iso_texts(src / "iso" / f"{layer}.xml"))
        cat_rows += legend_rows(src / "legend" / f"{layer}.json", layer)
        lv, key = level_of(df, cfg["levels"])
        vcols = [c for c in df.columns if VALUE.match(c)]
        for c in vcols:
            titles.setdefault(c, []).append((layer, meta))
        if lv is None or not vcols:
            loose.append((layer, code, df, meta))
            continue
        lname = (lv["name"], int(lv.get("year", year)))
        n0 = len(df)
        df, codes = level_codes(df, lv, key)
        if len(df) < n0:
            cons.append(dict(layer=f"cap{code[:2]}-{lv['name'].lower()}", code="NA", issue="repeated_rows", value=layer,
                             action="dropped", note=f"{n0 - len(df)} linhas idênticas (polígonos do mesmo país)"))
        if codes.duplicated().any():
            errors.append(f"{layer}: duplicate keys {sorted(set(codes[codes.duplicated()]))[:5]}")
            continue
        drop = codes.isin(cfg["drop_codes"]) if "edition" in lv else pd.Series(False, index=df.index)
        for c in codes[drop]:
            cons.append(dict(layer=f"cap{code[:2]}-{lv['name'].lower()}", code=c, issue="dropped_code", value=layer,
                             action="dropped", note="Código fora da DTB (lagoa)"))
        df = df[~drop].copy()
        df.index = codes[~drop].values
        keep = {s: t for s, t in lv.get("names", {}).items() if s in df.columns}
        slot = per_level.setdefault(lname, {"level": lv, "columns": {}, "names": []})
        if keep:
            slot["names"].append(df[list(keep)].rename(columns=keep))
        for c in vcols:
            slot["columns"].setdefault(c, []).append((layer, df[c]))

    written = []
    for (lname, lyear), slot in sorted(per_level.items()):
        lv, layer_name = slot["level"], lname.lower()
        cols = {}
        for c, parts in slot["columns"].items():
            s = parts[0][1]
            for other_layer, o in parts[1:]:
                both = s.index.intersection(o.index)
                diff = [k for k in both if s[k] != o[k]]
                if diff:
                    errors.append(f"{c}: {parts[0][0]} and {other_layer} disagree on {diff[:5]}")
                s = pd.concat([s, o[o.index.difference(s.index)]])
                if len(both):
                    logger.debug("%s: %d codes in more than one layer (%s), values agree", c, len(both), other_layer)
            cols[c] = s
        for c, parts in cfg["combine"].items():
            if c not in cols:
                continue
            for p in parts:
                if p not in cols:
                    errors.append(f"combine {c}: {p} not in level {lname}")
                    continue
                over = cols[c].index.intersection(cols[p].index)
                if len(over):
                    errors.append(f"combine {c} + {p}: {len(over)} codes in both")
                cols[c] = pd.concat([cols[c], cols.pop(p)])
                cons.append(dict(layer=f"cap{c[2:4]}-{layer_name}", code="NA", issue="combined_maps", value=f"{p} -> {c}",
                                 action="merged", note="Mapas complementares (com/sem serviço) unidos numa coluna"))
        for c, orig in cfg["duplicates"].items():
            if c in cols and orig in cols:
                a, b = cols[c].sort_index(), cols[orig].sort_index()
                if not (a.index.equals(b.index) and (a.values == b.values).all()):
                    errors.append(f"duplicate {c} differs from {orig}")
                    continue
                del cols[c]
                cons.append(dict(layer=f"cap{c[2:4]}-{layer_name}", code="NA", issue="duplicate_map", value=f"{c} = {orig}",
                                 action="dropped", note="Mesmo mapa repetido em outro capítulo"))
        if "edition" in lv:
            ed = quadro[quadro["edition"] == str(lv["edition"])].set_index("municipio_code")["municipio_name"]
            found = set().union(*[set(s.index) for s in cols.values()])
            unknown = sorted(found - set(ed.index))
            if unknown:
                errors.append(f"{lname} {lyear}: codes not in DTB {lv['edition']}: {unknown[:10]}")
                continue
            index = ed.index.sort_values()
        else:
            index = pd.Index(sorted(set().union(*[set(s.index) for s in cols.values()])))
        frame = {}
        for c in sorted(cols, key=lambda c: (c[2:4], c)):
            vals, vtype = typed(cols[c].reindex(index))
            frame[c] = vals.values
            num = vals[vals != -9999] if vtype != "text" else None
            scale = "NA"  # maps of shares ("Percentual", "Proporção", "Distribuição"): 0-1 or 0-100 as found
            share = re.search(r"percent|propor|distribui", titles[c][0][1]["title"].lower())
            if share and num is not None and len(num) and num.min() >= 0:
                scale = "0-1" if num.max() <= 1 else "0-100" if num.max() <= 100 else "NA"
            src_layers = [l for l, _ in slot["columns"].get(c, [])] + [
                l for p in cfg["combine"].get(c, []) for l, _ in slot["columns"].get(p, [])]
            meta = titles[c][0][1]
            title = meta["title"]
            if len(set(src_layers)) > 1:  # pieces by population size: the class goes out of the title
                title = SIZE_CLASS.sub("", title).strip()
            title = cfg["title_fixes"].get(c, title)
            if c in cfg["title_fixes"]:
                cons.append(dict(layer=f"cap{c[2:4]}-{layer_name}", code="NA", issue="title_fixed", value=c, action="replaced",
                                 note=f"Título do mapa: {meta['title']}; novo: {title}"))
            period = ",".join(sorted(set(re.findall(r"\b(?:19|20)\d\d\b", title)))) or str(year)
            var_rows.append(dict(
                layer=f"cap{c[2:4]}-{layer_name}", year=lyear, column=c, variable_name=title, period=period.replace(",", "-"), value_type=vtype, scale=scale,
                source_layers=" ".join(dict.fromkeys(src_layers)), metadata_title=meta.get("iso_title", "NA"),
                metadata_date=meta.get("iso_date", "NA")))
        frame = pd.DataFrame(frame, index=index)
        if slot["names"]:
            names = pd.concat(slot["names"])
            names = names[~names.index.duplicated()].reindex(index).fillna("NA")
            for n in reversed(list(names.columns)):
                frame.insert(0, n, names[n])
        if "edition" in lv:
            frame.insert(0, "municipio_name", ed.reindex(index).values)
        frame.index.name = lv["key"]
        frame = frame.reset_index()
        frame.insert(0, "year", lyear)
        ids = [c for c in frame.columns if c not in cols]
        for ch in sorted({c[2:4] for c in cols}):  # one file per chapter, same rows
            vc = [c for c in frame.columns if c in cols and c[2:4] == ch]
            name = cfg["name_pattern"].format(NAME=f"CAP{ch}-{lname}", YEAR=lyear)
            dest = out / name
            if dest.is_file() and not args.overwrite:
                errors.append(f"{name} exists (use --overwrite)")
                continue
            written.append((dest, frame[ids + vc]))
            logger.info("%s: %d rows, %d value columns from %d layers", name, len(frame), len(vc),
                        len({l for c in vc for l, _ in slot["columns"].get(c, [])}))

    for layer, code, df, meta in loose:
        name = cfg["name_pattern"].format(NAME=f"CAP{code[:2]}-{code}", YEAR=year)
        dest = out / name
        if dest.is_file() and not args.overwrite:
            errors.append(f"{name} exists (use --overwrite)")
            continue
        df = df.copy()
        df.insert(0, "year", year)
        written.append((dest, df))
        for c in [c for c in df.columns if VALUE.match(c)]:
            _, vtype = typed(df[c])
            var_rows.append(dict(layer=f"cap{code[:2]}-{code.lower()}", year=year, column=c, variable_name=meta["title"], period="NA",
                                 value_type=vtype,
                                 scale="NA", source_layers=layer, metadata_title=meta.get("iso_title", "NA"),
                                 metadata_date=meta.get("iso_date", "NA")))
        logger.info("%s: %d rows as published (no level)", name, len(df))

    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)
    for dest, frame in written:
        write_csv(frame, dest)

    write_csv(pd.DataFrame(var_rows), out / cfg["variables_name"])
    write_csv(pd.DataFrame(cons, columns=CONSISTENCY_COLUMNS), out / cfg["consistency_name"])
    cats = pd.DataFrame(cat_rows, columns=["source_layer", "column", "rule", "kind", "code", "lower", "upper", "label", "filter"])
    write_csv(cats, out / cfg["categories_name"])
    logger.info("Done: %d files, %d variables, %d legend rows, %d consistency rows", len(written), len(var_rows),
                len(cats), len(cons))


if __name__ == "__main__":
    main()

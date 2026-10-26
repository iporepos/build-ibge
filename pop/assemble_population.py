"""
Assemble the IBGE population series (POP) for T1.

Outputs (output_folder, CSV ';' UTF-8; text NA, numbers -9999 = not available):
  censos-municipios      census population by municipality 1872-2022. 1872-2010 from the table of "Evolução da
                         divisão territorial do Brasil 1872-2010" (dtb_evolucao_pop_1872-2010.csv): year,
                         municipio_code of that year (the DTB T1 snapshot codes), population, urban / rural
                         (1940+), free / enslaved (1872). 1960 = Sinopse preliminar (IBGE note). 2022 (config
                         census_tracts): sums of the census T1 tract table (basico V0001), urban / rural by
                         the tract situation.
  estimativas-municipios IBGE estimates of the resident population on 1 July (DOU / TCU tables), 1992-2026:
                         year, municipio_code (7 digits), municipio_name as published, population,
                         note_mark (footnote mark of the cell, e.g. (1) judicial population, see the
                         notes table). Years without estimates: 1996 and 2007 (population counts), 2010 and
                         2022 (censuses), 2023.
  estimativas-unidades   the published totals of Brasil, regions and UFs (sheets "BR e UFs"), when present
  estimativas-notas      footnotes of every estimate file (year, mark, text)
  CONSISTENCY            findings (rows of layers estimativas-* / censos-* are replaced)

Parsing rules: '-' is zero (IBGE convention), '...' / '…' / empty is not available (-9999). Estimates: footnote
marks are cut from the value / name cells ('59.881(*)' -> 59881, note_mark '(*)'); dots between thousand groups
are dropped. Municipality codes without check digit (1992-2006 files) get the 7-digit code of the DTB with the
same 6 leading digits (dtb_codes: T1 quadro-municipios + genealogia), else the IBGE rule. Census 2010 values of
the source are numbers written with thousand dots and read as decimals (24.392 = 24,392; 13.97 = 13,970): each
value is read as x1000 when it has a dot; a value without dot is taken literally or x1000, choosing the
combination where total = urban + rural (then total checked against census_totals).

Checks: every estimate code must exist in the DTB editions around the year (last not after it, first after it);
names compared with the DTB (folded); municipal sums vs published UF / Brasil totals; census national
totals vs census_totals.

Usage:
    python assemble_population.py --config assemble_population.local.json [--overwrite]
"""

import argparse
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

REQUIRED_KEYS = ["src_folder", "output_folder", "report_folder", "name_pattern", "consistency_name",
                 "census_csv", "census_years", "census_totals", "estimates", "dtb_quadro_csv", "dtb_genealogy_csv",
                 "unidades", "regioes"]
NA = "NA"
NODATA = -9999
CONSISTENCY_FIELDS = ["layer", "code", "issue", "value", "action", "note"]
LAYERS = ("censos-municipios", "estimativas-municipios", "estimativas-unidades", "estimativas-notas")
MARK = r"\(\s*\*+\s*\)|\(\s*\d+\s*\)|\*+"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"population_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("pop_population")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def fold(text) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[\s\-'`´.]+", " ", text).strip()


def clean(text) -> str:
    t = re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()
    return t if t else NA


def check_digit(code6: str) -> str:
    total = 0
    for i, ch in enumerate(code6):
        p = int(ch) * (1 if i % 2 == 0 else 2)
        total += p // 10 + p % 10
    return str((10 - total % 10) % 10)


def read_sheets(data: bytes) -> dict:
    try:
        from python_calamine import CalamineWorkbook
        wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
        return {n: wb.get_sheet_by_name(n).to_python() for n in wb.sheet_names if not n.startswith("'file")}
    except Exception:
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        return {s.name: [s.row_values(i) for i in range(s.nrows)] for s in wb.sheets()}


def write_csv(df: pd.DataFrame, path: Path, overwrite: bool, logger):
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} exists (use --overwrite)")
    tmp = path.with_name(path.name + ".part")
    df.to_csv(tmp, sep=";", index=False, encoding="utf-8", lineterminator="\n", na_rep=NA)
    tmp.replace(path)
    logger.info("Wrote %s (%d rows)", path, len(df))


def merge_consistency(path: Path, rows: list) -> int:
    kept = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter=";") if r["layer"] not in LAYERS]
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONSISTENCY_FIELDS, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(kept + rows)
    tmp.replace(path)
    return len(kept)


# ---------------------------------------------------------------------------
# Census 1872-2010
# ---------------------------------------------------------------------------

def census_value(v: str):
    v = v.strip()
    if v == "-":
        return 0
    if v in ("", "...", "…"):
        return NODATA
    return int(v.replace(".", ""))


def census_2010(tot: str, urb: str, rur: str):
    """Values written with thousand dots read as decimals: returns (tot, urb, rur, method)."""
    def cands(v):
        v = v.strip()
        if v == "-":
            return [0]
        if v == "":
            return [NODATA]
        if v.count(".") > 1:
            return [int(v.replace(".", ""))]
        if "." in v:
            return [round(float(v) * 1000)]
        return [int(v), int(v) * 1000] if len(v) >= 3 else [int(v) * 1000]
    for t in cands(tot):
        for u in cands(urb):
            for r in cands(rur):
                if t == u + r:
                    return t, u, r, "identity"
    t, u, r = (cands(x)[0] if len(cands(x)) == 1 or len(x.strip()) >= 3 else cands(x)[-1] for x in (tot, urb, rur))
    return t, u, r, "fallback"


def build_censos(cfg: dict, cons: list, logger) -> pd.DataFrame:
    src = pd.read_csv(cfg["census_csv"], sep=";", dtype=str, encoding="utf-8").fillna("")
    rows = []
    for y in cfg["census_years"]:
        cols = {"total": "pop.total" if y == 1872 else f"pop.tot.{y}"}
        if y == 1872:
            cols.update(free="pop.livre", enslaved="pop.escrava")
        if f"pop.urb.{y}" in src:
            cols.update(urban=f"pop.urb.{y}", rural=f"pop.rur.{y}")
        fallback = 0
        for _, r in src.iterrows():
            code = r[f"cod.{y}"].strip()
            if not code:
                continue
            name = re.split(r",", r[f"Município {y}"])[0].strip()
            rec = dict(year=y, municipio_code=code, municipio_name=clean(name), population=NODATA, population_urban=NODATA,
                       population_rural=NODATA, population_free=NODATA, population_enslaved=NODATA)
            if y == 2010:
                t, u, rr, method = census_2010(r[cols["total"]], r[cols["urban"]], r[cols["rural"]])
                rec.update(population=t, population_urban=u, population_rural=rr)
                if method != "identity":
                    fallback += 1
                    cons.append(dict(layer="censos-municipios", code=code, issue="census2010_value_ambiguous",
                                     value=f"{r[cols['total']]} / {r[cols['urban']]} / {r[cols['rural']]}", action=f"read as {t} / {u} / {rr}",
                                     note="Valor de 2010 com separador de milhar lido como decimal; total ≠ urbana + rural em todas as leituras"))
            else:
                rec["population"] = census_value(r[cols["total"]])
                for k in ("urban", "rural", "free", "enslaved"):
                    if k in cols:
                        rec[f"population_{k}"] = census_value(r[cols[k]])
            if rec["population"] == NODATA:
                note = r[f"Município {y}"]
                cons.append(dict(layer="censos-municipios", code=code, issue="population_not_available",
                                 value=f"{y}: {clean(note)}", action="-9999", note="População não informada na fonte"))
            rows.append(rec)
        d = pd.DataFrame([x for x in rows if x["year"] == y])
        tot = int(d.loc[d["population"] != NODATA, "population"].sum())
        exp = cfg["census_totals"].get(str(y))
        msg = f"{y}: {len(d)} municipalities, total {tot:,}" + (f" (published {exp:,}, diff {tot - exp:+,})" if exp else "")
        if "urban" in cols:
            ok = d[(d["population"] != NODATA) & (d["population_urban"] != NODATA) & (d["population_rural"] != NODATA)]
            bad = ok[ok["population"] != ok["population_urban"] + ok["population_rural"]]
            msg += f", total != urban+rural in {len(bad)}"
            for _, b in bad.iterrows():
                cons.append(dict(layer="censos-municipios", code=b["municipio_code"], issue="total_not_urban_plus_rural",
                                 value=f"{y}: {b['population']} vs {b['population_urban']} + {b['population_rural']}",
                                 action="kept as published", note="Total diferente da soma urbana + rural na fonte"))
        if y == 1872:
            ok = d[(d["population_free"] != NODATA) & (d["population_enslaved"] != NODATA) & (d["population"] != NODATA)]
            msg += f", total != free+enslaved in {int((ok['population'] != ok['population_free'] + ok['population_enslaved']).sum())}"
        if y == 2010:
            msg += f", 2010 ambiguous values {fallback}"
        if exp and tot != exp:
            cons.append(dict(layer="censos-municipios", code="BR", issue="national_total_differs", value=f"{y}: {tot} vs {exp}",
                             action="none", note="Soma dos municípios difere do total publicado do Brasil"))
        logger.info("  census %s", msg)
    for spec in cfg.get("census_tracts", []):
        rows += census_from_tracts(cfg, spec, cons, logger)
    return pd.DataFrame(rows)


def census_from_tracts(cfg: dict, spec: dict, cons: list, logger) -> list:
    """Census years after the genealogy table (2022): municipal sums of the census T1 tract table (copy in SRC);
    urban / rural by the tract situation."""
    y = spec["year"]
    cols = ["municipio_code", "municipio_name", spec["situacao"], spec["total"]]
    d = pd.read_csv(Path(cfg["src_folder"]) / spec["path"], sep=";", dtype=str, keep_default_na=False, usecols=cols)
    v = pd.to_numeric(d[spec["total"]], errors="coerce")
    bad = d[v.isna() | (v < 0)]
    for _, b in bad.iterrows():
        cons.append(dict(layer="censos-municipios", code=b["municipio_code"], issue="tract_value_not_available",
                         value=f"{y}: {b[spec['total']]}", action="tract excluded from the sum", note="Setor sem valor de população"))
    d = d.assign(pop=v.where(v >= 0, 0).astype(int))
    nocode = d[d["municipio_code"] == NA]
    if len(nocode):
        cons.append(dict(layer="censos-municipios", code=NA, issue="tract_without_municipio", value=f"{y}: {len(nocode)} tracts, {nocode['pop'].sum()} persons",
                         action="excluded" if nocode["pop"].sum() == 0 else "excluded (population lost)", note="Setor sem código de município"))
        d = d[d["municipio_code"] != NA]
    d["urban"] = d["pop"].where(d[spec["situacao"]] == spec["urban"], 0)
    d["rural"] = d["pop"].where(d[spec["situacao"]] == spec["rural"], 0)
    other = d.loc[(d["urban"] + d["rural"]) != d["pop"], "pop"].sum()
    g = d.groupby("municipio_code").agg(municipio_name=("municipio_name", "first"), population=("pop", "sum"),
                                        population_urban=("urban", "sum"), population_rural=("rural", "sum")).reset_index()
    exp = cfg["census_totals"].get(str(y))
    tot = int(g["population"].sum())
    if exp and tot != exp:
        cons.append(dict(layer="censos-municipios", code="BR", issue="national_total_differs", value=f"{y}: {tot} vs {exp}",
                         action="none", note="Soma dos municípios difere do total publicado do Brasil"))
    logger.info("  census %d (tracts %s): %d municipalities, total %s%s, urban %s, rural %s, without situation %s, tracts without value %d",
                y, spec["path"], len(g), f"{tot:,}", f" (published {exp:,}, diff {tot - exp:+,})" if exp else "",
                f"{g['population_urban'].sum():,}", f"{g['population_rural'].sum():,}", f"{other:,}", len(bad))
    return [dict(year=y, municipio_code=r.municipio_code, municipio_name=clean(r.municipio_name), population=int(r.population),
                 population_urban=int(r.population_urban), population_rural=int(r.population_rural),
                 population_free=NODATA, population_enslaved=NODATA) for r in g.itertuples()]


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------

def parse_estimate(v):
    """Cell -> (int or NODATA, mark)."""
    if isinstance(v, (int, float)):
        return int(round(v)), NA
    s = str(v).replace("\xa0", " ").strip()
    marks = re.findall(MARK, s)
    s = re.sub(MARK, "", s).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    mark = " ".join(m.replace(" ", "") for m in marks) if marks else NA
    return (int(s) if s.isdigit() else NODATA), mark


def norm_int(v) -> str:
    s = re.sub(r"\.0+$", "", str(v).strip())
    return s if s.isdigit() else ""


def dtb_prefix_map(cfg: dict) -> dict:
    """6-digit prefix -> 7-digit codes of the DTB since dtb_min_year (older codes, e.g. Mato Grosso before the
    1977 split, share prefixes with current ones)."""
    y0 = cfg.get("dtb_min_year", 1991)
    q = pd.read_csv(cfg["dtb_quadro_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["edition", "municipio_code"])
    g = pd.read_csv(cfg["dtb_genealogy_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["year", "municipio_code"])
    codes = set(q.loc[q["edition"].astype(int) >= y0, "municipio_code"]) | set(g.loc[g["year"].astype(int) >= y0, "municipio_code"])
    codes -= {NA}
    out = {}
    for c in codes:
        out.setdefault(c[:6], set()).add(c)
    return out


def read_estimate(cfg: dict, spec: dict, prefix: dict, cons: list, logger):
    src = Path(cfg["src_folder"])
    path = src / spec["path"]
    data = zipfile.ZipFile(path).read(spec["member"]) if "member" in spec else path.read_bytes()
    sheets = read_sheets(data)
    y = spec["year"]
    is_data = lambda r: len(r) >= 5 and re.fullmatch(r"[A-Z]{2}", str(r[0]).strip() or "") and norm_int(r[1])  # noqa: E731
    msheet = max(sheets, key=lambda n: sum(1 for r in sheets[n] if is_data(r)))
    rows = sheets[msheet]
    data_idx = [i for i, r in enumerate(rows) if is_data(r)]
    # check digit present? Brasília is 0010 without, 00108 with
    bsb = next(norm_int(rows[i][2]) for i in data_idx if norm_int(rows[i][1]) == "53")
    with_dv = spec.get("with_check_digit", int(bsb) == 108)
    recs = []
    for i in data_idx:
        r = rows[i]
        uf, mun = norm_int(r[1]).zfill(2), norm_int(r[2])
        name_raw = clean(r[3])
        nmarks = re.findall(MARK, name_raw)
        name = clean(re.sub(MARK, "", name_raw))
        pop, mark = parse_estimate(r[4])
        extra = [clean(c) for c in r[5:] if clean(c) != NA]
        marks = [m for m in [mark] + nmarks + [e for e in extra if re.fullmatch(MARK, e)] if m != NA]
        if with_dv:
            code = uf + mun.zfill(5)
            method = "published"
        else:
            p6 = uf + mun.zfill(4)[-4:]
            hit = sorted(prefix.get(p6, []))
            code, method = (hit[0], "dtb") if len(hit) == 1 else (p6 + check_digit(p6), "check_digit_rule")
            if method != "dtb":
                cons.append(dict(layer="estimativas-municipios", code=code, issue="code_from_check_digit_rule",
                                 value=f"{y}: {name}", action="7-digit code by the IBGE rule", note="Código de 6 dígitos sem correspondente no DTB"))
        if pop == NODATA:
            cons.append(dict(layer="estimativas-municipios", code=code, issue="population_not_parsed", value=f"{y}: {r[4]}",
                             action="-9999", note="Valor de população não numérico"))
        recs.append(dict(year=y, municipio_code=code, municipio_name=name, unidade_code=uf, population=pop,
                         note_mark=" ".join(marks) if marks else NA, code_source=method))
    d = pd.DataFrame(recs)
    notes = []
    for i, r in enumerate(rows[data_idx[-1] + 1:]):
        t = clean(" ".join(str(c) for c in r if clean(c) != NA))
        if t != NA:
            m = re.match(rf"^\s*(NOTA\d*:?\s*)?({MARK})", t)
            notes.append(dict(year=y, line=len(notes) + 1, mark=m.group(2).replace(" ", "") if m else NA, text=t))
    # published totals
    units = []
    for n, srows in sheets.items():
        if n == msheet:
            continue
        for r in srows:
            cells = [c for c in r if clean(c) != NA]
            if len(cells) < 2:
                continue
            label = clean(re.sub(MARK, "", str(cells[0])))
            pop, mark = parse_estimate(cells[1])
            if len(cells) > 2 and mark == NA and re.fullmatch(MARK, clean(cells[2])):
                mark = clean(cells[2])
            key = fold(re.sub(r"^(REGIAO|REGIÃO)\s+", "", label, flags=re.I))
            level = code = None
            if key == "BRASIL":
                level, code = "brasil", "BR"
            elif key in cfg["regioes"]:
                level, code = "regiao", cfg["regioes"][key]
            elif key in cfg["unidades"]:
                level, code = "unidade", cfg["unidades"][key]
            if level and pop != NODATA:
                units.append(dict(year=y, level=level, code=code, name=label, population=pop, note_mark=mark))
    u = pd.DataFrame(units).drop_duplicates(["year", "level", "code"]) if units else pd.DataFrame()
    logger.info("  estimate %d: sheet %s, %d municipalities, check digit %s, total %s, %d marks, %d note lines, %d unit totals",
                y, msheet, len(d), "published" if with_dv else "derived", f"{d.loc[d['population'] != NODATA, 'population'].sum():,}",
                int((d["note_mark"] != NA).sum()), len(notes), len(u))
    return d, pd.DataFrame(notes), u


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))
    out = Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    cons = []

    censos = build_censos(cfg, cons, logger)

    prefix = dtb_prefix_map(cfg)
    est, notes, units = [], [], []
    for spec in cfg["estimates"]:
        d, n, u = read_estimate(cfg, spec, prefix, cons, logger)
        est.append(d)
        notes.append(n)
        units.append(u)
    est = pd.concat(est, ignore_index=True)
    notes = pd.concat(notes, ignore_index=True)
    units = pd.concat([u for u in units if len(u)], ignore_index=True)

    # checks against the DTB and published totals
    q = pd.read_csv(cfg["dtb_quadro_csv"], sep=";", dtype=str, keep_default_na=False, usecols=["edition", "municipio_code", "municipio_name"])
    q["edition"] = q["edition"].astype(int)
    eds = sorted(q["edition"].unique())
    summary = []
    for y, d in est.groupby("year"):
        dup = d[d["municipio_code"].duplicated(keep=False)]
        for c in sorted(set(dup["municipio_code"])):
            cons.append(dict(layer="estimativas-municipios", code=c, issue="duplicated_code", value=str(y), action="kept", note="Código repetido no arquivo"))
        # DTB editions around the year: the last one not after it and the first one after it
        ed = max([e for e in eds if e <= y] or [eds[0]])
        ed_next = min([e for e in eds if e > y] or [ed])
        ref = q[q["edition"] == ed].set_index("municipio_code")["municipio_name"]
        ref_next = q[q["edition"] == ed_next].set_index("municipio_code")["municipio_name"]
        ref = pd.concat([ref, ref_next[~ref_next.index.isin(ref.index)]])
        ed = f"{ed}/{ed_next}" if ed_next != ed else str(ed)
        notin = d[~d["municipio_code"].isin(ref.index)]
        for _, r in notin.iterrows():
            cons.append(dict(layer="estimativas-municipios", code=r["municipio_code"], issue="code_not_in_dtb_edition",
                             value=f"{y}: {r['municipio_name']} (DTB {ed})", action="kept", note="Código ausente da edição do DTB vigente"))
        both = d[d["municipio_code"].isin(ref.index)]
        named = both[both["municipio_name"].map(fold) != both["municipio_code"].map(ref).map(fold)]
        for _, r in named.iterrows():
            cons.append(dict(layer="estimativas-municipios", code=r["municipio_code"], issue="name_differs_from_dtb",
                             value=f"{y}: {r['municipio_name']} / DTB {ed}: {ref[r['municipio_code']]}", action="published name kept",
                             note="Nome da estimativa difere do DTB"))
        tot = int(d.loc[d["population"] != NODATA, "population"].sum())
        pub = units[(units["year"] == y) & (units["level"] == "brasil")]["population"]
        uf_bad = 0
        uu = units[(units["year"] == y) & (units["level"] == "unidade")].set_index("code")["population"]
        sums = d[d["population"] != NODATA].groupby("unidade_code")["population"].sum()
        for c, v in uu.items():
            if sums.get(c, 0) != v:
                uf_bad += 1
                cons.append(dict(layer="estimativas-unidades", code=c, issue="uf_total_differs_from_municipal_sum",
                                 value=f"{y}: published {v} vs sum {sums.get(c, 0)}", action="both kept",
                                 note="Total da UF publicado difere da soma dos municípios (ver notas do arquivo)"))
        summary.append(dict(year=y, municipios=len(d), dtb_edition=ed, not_in_dtb=len(notin), name_differs=len(named),
                            sum_municipios=tot, brasil_published=int(pub.iloc[0]) if len(pub) else NODATA,
                            uf_totals=len(uu), uf_totals_differ=uf_bad, marks=int((d["note_mark"] != NA).sum())))
    s = pd.DataFrame(summary)
    s.to_csv(Path(cfg["report_folder"]) / "estimates_summary.csv", sep=";", index=False)
    logger.info("Estimates summary:\n%s", s.to_string(index=False))

    pat = cfg["name_pattern"]
    cy = sorted(censos["year"].unique())
    ey = sorted(est["year"].unique())
    # census years against the DTB quadro of the same edition (genealogy years are checked in the DTB lineage)
    for y in sorted({s["year"] for s in cfg.get("census_tracts", [])} & set(q["edition"])):
        ref = set(q.loc[q["edition"] == y, "municipio_code"])
        got = set(censos.loc[censos["year"] == y, "municipio_code"])
        for c in sorted(got ^ ref):
            cons.append(dict(layer="censos-municipios", code=c, issue="code_not_in_dtb_edition" if c in got else "dtb_code_without_census",
                             value=str(y), action="kept", note="Código do censo e do DTB da mesma edição não coincidem"))
        logger.info("  census %d vs DTB %d: %d / %d codes, %d differ", y, y, len(got), len(ref), len(got ^ ref))
    write_csv(censos, out / pat.format(NAME="CENSOS-MUNICIPIOS", YEAR=f"{cy[0]}-{cy[-1]}"), args.overwrite, logger)
    write_csv(est.drop(columns=["code_source"]), out / pat.format(NAME="ESTIMATIVAS-MUNICIPIOS", YEAR=f"{ey[0]}-{ey[-1]}"), args.overwrite, logger)
    uy = sorted(units["year"].unique())
    write_csv(units, out / pat.format(NAME="ESTIMATIVAS-UNIDADES", YEAR=f"{uy[0]}-{uy[-1]}"), args.overwrite, logger)
    write_csv(notes, out / pat.format(NAME="ESTIMATIVAS-NOTAS", YEAR=f"{ey[0]}-{ey[-1]}"), args.overwrite, logger)
    kept = merge_consistency(out / cfg["consistency_name"], cons)
    logger.info("CONSISTENCY: %d rows (%d of other layers kept)", len(cons), kept)


if __name__ == "__main__":
    main()

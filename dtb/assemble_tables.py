"""
Assemble the tabular IBGE DTB (Divisão Territorial Brasileira) sources for T1.

Outputs (output_folder, CSV ';' UTF-8, NA = not available):
  quadro-municipios    one row per edition x municipio_code: names and codes of the municipality and of the
                       hierarchy published in that edition (UF, meso/microregion, intermediate/immediate
                       region from 2017). Editions 1940-1980 (lists of the census years), 1994, 2000, 2003-2022
  quadro-distritos     edition x distrito_code (9 digits), distrito_name, municipio_code
  quadro-subdistritos  edition x subdistrito_code (11 digits), subdistrito_name, distrito_code
  genealogia           the IBGE genealogy table of "Evolução da divisão territorial do Brasil 1872-2010"
                       (dtb_evolucao_pop_1872-2010.csv) in long form: one row per source row x census year
                       with the municipality code/name of that year, or, when it did not exist, the relation
                       given in the note (desmembrado de / anexado a / sede em ...) with the referenced
                       municipality resolved to its code of that year or of the next census (reference_code,
                       reference_year, reference_method; lists "A, B e C" kept aligned with " | ")
  CONSISTENCY          rows of layers quadro-* and genealogia are replaced (merged by layer)

Source formats (config "editions"; each edition lists one or more files, read straight from the zips):
  flat        spreadsheet with one column per role (config "column_sets"); codes may be partial (meso 2,
              micro 3, municipio 5 digits) and are completed with the parent codes
  dat_1994    fixed-width hierarchical DAT (UF2 MESO2 MICRO3 MUN4 DV1 DIST2 SUBD2 NAME)
  nivel_2000  hierarchical sheet with a "Nível" column (2 UF, 8 meso, 9 micro, 5 mun, 6 dist, 7 subd)
  nivel_2004  hierarchical sheet NÍVEL/UF/MUNICÍPIO/DISTRITO/SUBDISTRITO/NOME; meso/micro membership is
              taken from the row order (rows are sorted by the hierarchy)

Checks (CONSISTENCY / log): duplicated codes inside an edition, municipio codes not 7 digits, codes whose
check digit does not follow the IBGE rule (kept: a few official codes are exceptions), districts whose
municipality is not listed, genealogy references not resolved.

Usage:
    python assemble_tables.py --config assemble_tables.local.json [--overwrite]
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dtbtools import (NA, check_digit, clean_name, fold, load_config, merge_consistency,  # noqa: E402
                      norm_code, read_sheet, setup_logging, write_csv, zip_bytes)

REQUIRED_KEYS = ["src_folder", "output_folder", "report_folder", "name_pattern", "consistency_name",
                 "column_sets", "editions", "genealogy_csv", "genealogy_years", "unidades"]
ROLES = ["unidade_code", "unidade_name", "regiaomeso_code", "regiaomeso_name", "regiaomicro_code",
         "regiaomicro_name", "regiaointermediaria_code", "regiaointermediaria_name", "regiaoimediata_code",
         "regiaoimediata_name", "municipio_code", "municipio_name", "distrito_code", "distrito_name",
         "subdistrito_code", "subdistrito_name"]
MUN_COLS = ["edition", "municipio_code", "municipio_name", "unidade_code", "unidade_name", "regiaomeso_code",
            "regiaomeso_name", "regiaomicro_code", "regiaomicro_name", "regiaointermediaria_code",
            "regiaointermediaria_name", "regiaoimediata_code", "regiaoimediata_name"]
DIST_COLS = ["edition", "distrito_code", "distrito_name", "municipio_code"]
SUB_COLS = ["edition", "subdistrito_code", "subdistrito_name", "distrito_code"]
GEN_COLS = ["source_row", "trajectory_code", "trajectory_name", "year", "municipio_code", "municipio_name",
            "unidade_code", "relation", "reference_name", "reference_unidade_code", "reference_code",
            "reference_year", "reference_method", "divergence", "note"]
LAYERS = ("quadro-municipios", "quadro-distritos", "quadro-subdistritos", "genealogia")


# ---------------------------------------------------------------------------
# Edition readers -> DataFrame with the ROLES columns (codes completed, names cleaned)
# ---------------------------------------------------------------------------

def complete_codes(d: pd.DataFrame) -> pd.DataFrame:
    """Prefix partial codes with their parents: meso UF+2, micro UF+3, municipio UF+5, distrito +2, subd +2."""
    uf = d["unidade_code"].str.zfill(2)
    d["unidade_code"] = uf

    def full(col, width, prefix, part):
        v = d[col]
        ok = v != ""
        d.loc[ok, col] = [x if len(x) == width else p + x.zfill(part)[-part:]
                          for x, p in zip(v[ok], prefix[ok])]

    full("regiaomeso_code", 4, uf, 2)
    full("regiaomicro_code", 5, uf, 3)
    full("regiaointermediaria_code", 4, uf, 2)
    full("regiaoimediata_code", 6, uf, 4)
    full("municipio_code", 7, uf, 5)
    d.loc[d["distrito_code"].isin(["0", "00"]), "distrito_code"] = ""
    d.loc[d["subdistrito_code"].isin(["0", "00"]), "subdistrito_code"] = ""
    full("distrito_code", 9, d["municipio_code"], 2)
    full("subdistrito_code", 11, d["distrito_code"], 2)
    # full-width codes of "no subdistrict" rows end in 00; total / blank rows have municipality part 00000
    d.loc[d["subdistrito_code"].str.fullmatch(r"\d{9}00"), "subdistrito_code"] = ""
    return d[~d["municipio_code"].str.fullmatch(r"\d{2}00000")].reset_index(drop=True)


def empty_roles(n: int) -> dict:
    return {r: [""] * n for r in ROLES}


def read_flat(src: Path, f: dict, column_sets: dict) -> pd.DataFrame:
    rows = read_sheet(zip_bytes(src / f["path"], f["member"]) if "member" in f else (src / f["path"]).read_bytes(),
                      f.get("sheet"))
    cols = column_sets[f["column_set"]]
    hdr = f.get("header_row")
    if hdr is None:
        hdr = next(i for i, r in enumerate(rows[:15]) if str(r[0]).strip() in ("UF", "Uf"))
    header = [str(c).strip() for c in rows[hdr]]
    missing = [c for c in cols.values() if c not in header]
    if missing:
        raise SystemExit(f"{f['path']} {f.get('member', '')}: columns not found {missing}")
    idx = {role: header.index(c) for role, c in cols.items()}
    data = {r: [] for r in ROLES}
    for r in rows[hdr + 1:]:
        if not re.fullmatch(r"\d{2}", norm_code(r[idx["unidade_code"]]).zfill(2)) or not norm_code(r[idx["unidade_code"]]):
            continue
        for role in ROLES:
            if role not in idx:
                data[role].append("")
            elif role.endswith("_code"):
                data[role].append(norm_code(r[idx[role]]))
            else:
                data[role].append(clean_name(r[idx[role]]))
    return complete_codes(pd.DataFrame(data))


def read_dat_1994(src: Path, f: dict) -> pd.DataFrame:
    text = zip_bytes(src / f["path"], f["member"]).decode(f.get("encoding", "cp850"))
    names = {}
    recs = []
    for line in text.splitlines():
        if len(line) < 17 or not line[:2].isdigit():
            continue
        uf, me, mi, mu, dv, di, sd, name = (line[0:2], line[2:4], line[4:7], line[7:11], line[11], line[12:14],
                                            line[14:16], clean_name(line[16:80]))
        if mu == "0000":
            level = "micro" if mi != "000" else "meso" if me != "00" else "uf"
            names[(level, uf + (me if level == "meso" else mi if level == "micro" else ""))] = name
            continue
        recs.append((uf, me, mi, uf + mu + dv, di if di != "00" else "", sd if sd != "00" else "", name))
    d = empty_roles(len(recs))
    for i, (uf, me, mi, mun, di, sd, name) in enumerate(recs):
        d["unidade_code"][i], d["regiaomeso_code"][i], d["regiaomicro_code"][i] = uf, uf + me, uf + mi
        d["unidade_name"][i] = names.get(("uf", uf), NA)
        d["regiaomeso_name"][i] = names.get(("meso", uf + me), NA)
        d["regiaomicro_name"][i] = names.get(("micro", uf + mi), NA)
        d["municipio_code"][i] = mun
        d["distrito_code"][i] = di
        d["subdistrito_code"][i] = sd
        target = "subdistrito_name" if sd else "distrito_name" if di else "municipio_name"
        d[target][i] = name
    d = pd.DataFrame(d)
    return fill_parent_names(complete_codes(d))


def fill_parent_names(d: pd.DataFrame) -> pd.DataFrame:
    """Hierarchical sources give each name on its own row: copy municipio / distrito names to their children."""
    mun = d[(d["distrito_code"] == "") & (d["municipio_name"] != "")].drop_duplicates("municipio_code")
    d["municipio_name"] = d["municipio_code"].map(dict(zip(mun["municipio_code"], mun["municipio_name"]))).fillna(NA)
    dist = d[(d["distrito_code"] != "") & (d["subdistrito_code"] == "") & (d["distrito_name"] != "")]
    dmap = dict(zip(dist["distrito_code"], dist["distrito_name"]))
    has = d["distrito_code"] != ""
    d.loc[has, "distrito_name"] = d.loc[has, "distrito_code"].map(dmap).fillna(NA)
    return d


def read_nivel_2000(src: Path, f: dict) -> pd.DataFrame:
    rows = read_sheet(zip_bytes(src / f["path"], f["member"]), f.get("sheet"))
    names = {}
    recs = []
    for r in rows[1:]:
        lv = norm_code(r[0])
        if lv not in ("2", "5", "6", "7", "8", "9"):
            continue
        uf, me, mi = norm_code(r[2]).zfill(2), norm_code(r[3]).zfill(2), norm_code(r[4]).zfill(3)
        mu, di, sd, name = norm_code(r[5]).zfill(5), norm_code(r[6]).zfill(2), norm_code(r[7]).zfill(2), clean_name(r[8])
        if lv == "2":
            names[("uf", uf)] = name
        elif lv == "8":
            names[("meso", uf + me)] = name
        elif lv == "9":
            names[("micro", uf + mi)] = name
        else:
            recs.append((lv, uf, me, mi, uf + mu, di if lv in ("6", "7") else "", sd if lv == "7" else "", name))
    d = empty_roles(len(recs))
    for i, (lv, uf, me, mi, mun, di, sd, name) in enumerate(recs):
        d["unidade_code"][i], d["regiaomeso_code"][i], d["regiaomicro_code"][i] = uf, uf + me, uf + mi
        d["unidade_name"][i] = names.get(("uf", uf), NA)
        d["regiaomeso_name"][i] = names.get(("meso", uf + me), NA)
        d["regiaomicro_name"][i] = names.get(("micro", uf + mi), NA)
        d["municipio_code"][i], d["distrito_code"][i], d["subdistrito_code"][i] = mun, di, sd
        d[{"5": "municipio_name", "6": "distrito_name", "7": "subdistrito_name"}[lv]][i] = name
    return fill_parent_names(complete_codes(pd.DataFrame(d)))


def read_nivel_2004(src: Path, f: dict) -> pd.DataFrame:
    rows = read_sheet(zip_bytes(src / f["path"], f["member"]), f.get("sheet"))
    uf_names, names, recs = {}, {}, []
    meso = micro = ""
    for r in rows[1:]:
        lv = norm_code(r[0]).lstrip("0")
        if lv not in ("2", "5", "6", "7", "8", "9"):
            continue
        uf, mu, di, sd, name = norm_code(r[1]).zfill(2), str(r[2]).strip(), str(r[3]).strip(), str(r[4]).strip(), clean_name(r[5])
        if lv == "2":
            uf_names[uf] = name
        elif lv == "8":
            meso = uf + mu[:2]
            names[("meso", meso)] = name
        elif lv == "9":
            micro = uf + mu[:3]
            names[("micro", micro)] = name
        else:
            recs.append((lv, uf, meso, micro, uf + mu, di if lv in ("6", "7") else "", sd if lv == "7" else "", name))
    d = empty_roles(len(recs))
    for i, (lv, uf, me, mi, mun, di, sd, name) in enumerate(recs):
        d["unidade_code"][i], d["regiaomeso_code"][i], d["regiaomicro_code"][i] = uf, me, mi
        d["unidade_name"][i] = uf_names.get(uf, NA)
        d["regiaomeso_name"][i] = names.get(("meso", me), NA)
        d["regiaomicro_name"][i] = names.get(("micro", mi), NA)
        d["municipio_code"][i], d["distrito_code"][i], d["subdistrito_code"][i] = mun, di, sd
        d[{"5": "municipio_name", "6": "distrito_name", "7": "subdistrito_name"}[lv]][i] = name
    return fill_parent_names(complete_codes(pd.DataFrame(d)))


def read_edition(src: Path, ed: dict, column_sets: dict) -> list:
    out = []
    for f in ed["files"]:
        fmt = ed["format"]
        if fmt == "flat":
            out.append(read_flat(src, f, column_sets))
        elif fmt == "dat_1994":
            out.append(read_dat_1994(src, f))
        elif fmt == "nivel_2000":
            out.append(read_nivel_2000(src, f))
        elif fmt == "nivel_2004":
            out.append(read_nivel_2004(src, f))
        else:
            raise SystemExit(f"Unknown format {fmt}")
    return out


# ---------------------------------------------------------------------------
# Level tables
# ---------------------------------------------------------------------------

def level_tables(edition: int, frames: list, cons: list, logger):
    mun, dist, sub = [], [], []
    for d in frames:
        mun.append(d[d["municipio_code"] != ""])
        dist.append(d[d["distrito_code"] != ""])
        sub.append(d[d["subdistrito_code"] != ""])
    m = pd.concat(mun)
    m = m.replace("", NA)
    m.insert(0, "edition", edition)
    m = dedupe(m, "municipio_code", "municipio_name", "quadro-municipios", edition, cons)[MUN_COLS]
    out = [m]
    for lst, key, name, parent, cols, layer in ((dist, "distrito_code", "distrito_name", "municipio_code", DIST_COLS, "quadro-distritos"),
                                               (sub, "subdistrito_code", "subdistrito_name", "distrito_code", SUB_COLS, "quadro-subdistritos")):
        t = pd.concat(lst) if lst else pd.DataFrame(columns=ROLES)
        if len(t) == 0:
            out.append(pd.DataFrame(columns=cols))
            continue
        t = t.replace("", NA)
        t.insert(0, "edition", edition)
        out.append(dedupe(t, key, name, layer, edition, cons)[cols])

    # integrity
    bad = m[~m["municipio_code"].str.fullmatch(r"\d{7}")]
    if len(bad):
        raise SystemExit(f"Edition {edition}: municipio codes not 7 digits: {bad['municipio_code'].head().tolist()}")
    for t, parent_col, parents, layer in ((out[1], "municipio_code", set(m["municipio_code"]), "quadro-distritos"),
                                          (out[2], "distrito_code", set(out[1]["distrito_code"]), "quadro-subdistritos")):
        orphan = t[~t[parent_col].isin(parents)]
        for _, r in orphan.iterrows():
            cons.append(dict(layer=layer, code=r.iloc[1], issue="parent_not_listed", value=f"{edition}: {r[parent_col]}",
                             action="kept", note="Unidade superior não listada na mesma edição"))
        if len(orphan):
            logger.warning("  %d: %d %s rows whose parent is not listed", edition, len(orphan), layer)
    logger.info("  %d: %5d municipios, %5d distritos, %4d subdistritos", edition, len(out[0]), len(out[1]), len(out[2]))
    return out


def dedupe(t: pd.DataFrame, key: str, name: str, layer: str, edition: int, cons: list) -> pd.DataFrame:
    """One row per code; the first file of the edition wins. Different names for one code are reported."""
    g = t.groupby(key, sort=False)[name].agg(lambda s: sorted(set(s) - {NA}))
    for code, names in g.items():
        if len(names) > 1:
            cons.append(dict(layer=layer, code=code, issue="code_with_several_names", value=f"{edition}: {' | '.join(names)}",
                             action="first kept", note="Mesmo código com nomes diferentes na mesma edição"))
    return t.drop_duplicates(key, keep="first").sort_values(key).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Genealogy
# ---------------------------------------------------------------------------

RELATIONS = [  # (regex on the note, relation)
    (r"^anexado e desmembrado de\s+(?P<ref>.+)$", "anexado_desmembrado"),
    (r"^anexado a\s+(?P<ref>.+?)\s+e desmembrado de\s+.+$", "anexado_desmembrado"),
    (r"^desmembrado\s+(?:de|e)\s+(?P<ref>.+)$", "desmembrado"),
    (r"^anexado a\s+(?P<ref>.+)$", "anexado"),
    (r"^sede em\s+(?P<ref>[^,]+)(?:,.*)?$", "sede"),
    (r"^litígio\s+(?P<ref>.+)$", "litigio"),
]


def parse_note(text: str):
    """Absent-year note -> (relation, reference name, reference UF sigla or '', divergence flag)."""
    t = re.sub(r"\s+", " ", text).strip()
    divergence = "*" in t
    t = t.replace("*", "").strip()
    for pat, rel in RELATIONS:
        m = re.match(pat, t)
        if m:
            ref = m.group("ref").strip().rstrip(".")
            uf = ""
            mm = re.match(r"^(.*?),\s*([A-Z]{2})$", ref)
            if mm and rel != "litigio":
                ref, uf = mm.group(1).strip(), mm.group(2)
            return rel, ref, uf, divergence
    return "outro", NA, "", divergence


def split_name(text: str):
    """Existing-year cell -> (name, unidade sigla or '', annotation or NA)."""
    t = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"^(.*?) - (Território Municipal)$", t)
    if m:
        return m.group(1), "", m.group(2)
    if "," not in t:
        return t, "", NA
    name, rest = t.split(",", 1)
    rest = rest.strip()
    if re.fullmatch(r"[A-Z]{2}", rest):
        return name.strip(), rest, NA
    return name.strip(), "", rest


def build_genealogy(cfg: dict, quadro: pd.DataFrame, cons: list, logger) -> pd.DataFrame:
    src = pd.read_csv(cfg["genealogy_csv"], sep=";", dtype=str, encoding="utf-8").fillna("")
    years = cfg["genealogy_years"]
    sigla = cfg["unidades"]
    rows = []
    for i, r in src.iterrows():
        traj_code, traj_name = r["código"].strip(), clean_name(r["Município"])
        codes = [r[f"cod.{y}"].strip() for y in years]
        for k, y in enumerate(years):
            code, cell = codes[k], r[f"Município {y}"]
            if not code and not cell.strip():
                continue
            rec = dict(source_row=i + 1, trajectory_code=traj_code, trajectory_name=traj_name, year=y,
                       municipio_code=NA, municipio_name=NA, unidade_code=NA, relation=NA, reference_name=NA,
                       reference_unidade_code=NA, reference_code=NA, reference_method=NA, divergence=0, note=NA)
            if code:
                name, uf, note = split_name(cell)
                rec.update(municipio_code=code, municipio_name=name or NA, unidade_code=code[:2], note=note)
                if uf and sigla.get(uf) != code[:2]:
                    rec["note"] = f"{uf}" if note == NA else f"{uf}; {note}"
                m = re.match(r"^popula[çc][ãa]o in[cl]+u[íi]da na de\s+(?P<ref>.+?)\.?$", note or "")
                if m:
                    rec.update(relation="populacao_incluida", reference_name=m.group("ref").strip())
            else:
                rel, ref, uf, div = parse_note(cell)
                near = next((c for c in codes[k + 1:] if c), None) or next((c for c in reversed(codes[:k]) if c), "")
                rec.update(relation=rel, reference_name=ref, divergence=int(div), note=clean_name(cell),
                           unidade_code=near[:2] if near else NA)
                if uf:
                    rec["reference_unidade_code"] = sigla.get(uf, NA)
            rows.append(rec)
    g = pd.DataFrame(rows)
    resolve_references(g, quadro, cfg, cons, logger)
    return g[GEN_COLS]


def split_references(ref: str, known: set) -> list:
    """'A, B e C' -> [A, B, C]; adjacent tokens are joined again when they form a known name (Abreu e Lima)."""
    tokens = [t.strip() for t in re.split(r",\s*|\s+e\s+", ref) if t.strip()]
    out, i = [], 0
    while i < len(tokens):
        if i + 1 < len(tokens) and fold(f"{tokens[i]} e {tokens[i + 1]}") in known:
            out.append(f"{tokens[i]} e {tokens[i + 1]}")
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def resolve_one(ref: str, uf: str, c: pd.DataFrame, cutoff: float):
    """One name -> (method, code) among the municipalities c of one year."""
    f = fold(ref)
    same = c[c["uf"] == uf]
    hit = sorted(set(same.loc[same["f"] == f, "municipio_code"]))
    if len(hit) == 1:
        return "exact", hit[0]
    if len(hit) > 1:
        return "ambiguous", NA
    other = sorted(set(c.loc[c["f"] == f, "municipio_code"]))
    if len(other) == 1:
        return "other_uf", other[0]
    close = difflib.get_close_matches(f, sorted(set(same["f"])), n=2, cutoff=cutoff)
    if close and (len(close) == 1 or difflib.SequenceMatcher(None, f, close[0]).ratio() >
                  difflib.SequenceMatcher(None, f, close[1]).ratio()):
        hit = sorted(set(same.loc[same["f"] == close[0], "municipio_code"]))
        if len(hit) == 1:
            return "fuzzy", hit[0]
    return "unresolved", NA


def resolve_references(g: pd.DataFrame, quadro: pd.DataFrame, cfg: dict, cons: list, logger):
    """reference_name (a ' | ' list after splitting 'A, B e C') -> codes of municipalities existing in the same
    census year (genealogy and quadro names of that year). Per name: same UF exact fold, other UF unique,
    same UF approximate (difflib >= fuzzy_cutoff); failing that, the same tests in the next census year
    (the note describes a change between the two years; reference_year tells which year the code is from)."""
    cutoff = cfg.get("fuzzy_cutoff", 0.85)
    years = cfg["genealogy_years"]
    cand = g[g["municipio_code"] != NA][["year", "municipio_code", "municipio_name"]]
    q = quadro[quadro["edition"].isin(years)][["edition", "municipio_code", "municipio_name"]]
    cand = pd.concat([cand, q.rename(columns={"edition": "year"})]).drop_duplicates()
    cand["uf"] = cand["municipio_code"].str[:2]
    cand["f"] = cand["municipio_name"].map(fold)
    by_year = {y: c for y, c in cand.groupby("year")}
    known = set(cand["f"])
    g["reference_year"] = NA
    stats = {}
    for idx in g.index[g["reference_name"] != NA]:
        y = g.at[idx, "year"]
        uf = g.at[idx, "reference_unidade_code"] if g.at[idx, "reference_unidade_code"] != NA else g.at[idx, "unidade_code"]
        names = split_references(g.at[idx, "reference_name"], known) if g.at[idx, "relation"] != "litigio" else [g.at[idx, "reference_name"]]
        methods, codes, ryears = [], [], []
        for name in names:
            method, code, ry = "unresolved", NA, NA
            if g.at[idx, "relation"] != "litigio":
                for k, yy in enumerate((y, years[years.index(y) + 1] if y != years[-1] else None)):
                    if yy is None or yy not in by_year:
                        continue
                    method, code = resolve_one(name, uf, by_year[yy], cutoff)
                    if code != NA:
                        ry = yy
                        if k == 1:
                            method = f"next_year_{method}"
                        break
            methods.append(method)
            codes.append(code)
            ryears.append(str(ry))
            stats[method] = stats.get(method, 0) + 1
            if method.endswith(("unresolved", "ambiguous", "fuzzy")) and g.at[idx, "relation"] != "litigio":
                cons.append(dict(layer="genealogia", code=g.at[idx, "trajectory_code"], issue=f"reference_{method}",
                                 value=f"{y}: {name}" + (f" -> {code} ({ry})" if code != NA else ""),
                                 action="code set by approximate name" if code != NA else "reference_code NA",
                                 note="Município de referência da nota da genealogia"))
        g.at[idx, "reference_name"] = " | ".join(names)
        g.at[idx, "reference_code"] = " | ".join(codes)
        g.at[idx, "reference_method"] = " | ".join(methods)
        g.at[idx, "reference_year"] = " | ".join(ryears)
    logger.info("Genealogy references (per name): %s", dict(sorted(stats.items())))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config, REQUIRED_KEYS)
    logger = setup_logging(Path(cfg["report_folder"]), "tables")
    src, out = Path(cfg["src_folder"]), Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    cons = []

    mun, dist, sub = [], [], []
    for ed in cfg["editions"]:
        m, d, s = level_tables(ed["edition"], read_edition(src, ed, cfg["column_sets"]), cons, logger)
        mun.append(m)
        dist.append(d)
        sub.append(s)
    quadro = pd.concat(mun, ignore_index=True)
    dv = quadro[[check_digit(c[:6]) != c[6] for c in quadro["municipio_code"]]]
    for c, grp in dv.groupby("municipio_code"):
        eds_c = sorted(grp["edition"])
        cons.append(dict(layer="quadro-municipios", code=c, issue="check_digit_exception",
                         value=f"{grp['municipio_name'].iloc[0]}; editions {eds_c[0]}-{eds_c[-1]} ({len(eds_c)})",
                         action="kept", note=f"Dígito verificador fora da regra do IBGE (a regra daria {check_digit(c[:6])}); "
                         "códigos XX999NN são de municípios extintos"))
    logger.info("Codes outside the check-digit rule: %d", dv["municipio_code"].nunique())
    distritos = pd.concat(dist, ignore_index=True)
    subdistritos = pd.concat(sub, ignore_index=True)

    # edition-to-edition changes (report only)
    changes = []
    eds = [e["edition"] for e in cfg["editions"]]
    for a, b in zip(eds[:-1], eds[1:]):
        qa = quadro[quadro["edition"] == a].set_index("municipio_code")["municipio_name"]
        qb = quadro[quadro["edition"] == b].set_index("municipio_code")["municipio_name"]
        for c in sorted(set(qb.index) - set(qa.index)):
            changes.append(dict(edition_from=a, edition_to=b, municipio_code=c, change="appears", name_from=NA, name_to=qb[c]))
        for c in sorted(set(qa.index) - set(qb.index)):
            changes.append(dict(edition_from=a, edition_to=b, municipio_code=c, change="disappears", name_from=qa[c], name_to=NA))
        for c in sorted(set(qa.index) & set(qb.index)):
            if fold(qa[c]) != fold(qb[c]):
                changes.append(dict(edition_from=a, edition_to=b, municipio_code=c, change="renamed", name_from=qa[c], name_to=qb[c]))
    ch = pd.DataFrame(changes)
    ch.to_csv(Path(cfg["report_folder"]) / "quadro_changes.csv", sep=";", index=False)
    logger.info("Edition changes: %s (report quadro_changes.csv)", ch["change"].value_counts().to_dict())

    gen = build_genealogy(cfg, quadro, cons, logger)
    logger.info("Genealogy: %d rows (%d source rows), relations %s", len(gen), gen["source_row"].nunique(),
                gen["relation"].value_counts().to_dict())

    pat = cfg["name_pattern"]
    span_q = f"{eds[0]}-{eds[-1]}"
    write_csv(quadro, out / pat.format(NAME="QUADRO-MUNICIPIOS", YEAR=span_q), args.overwrite, logger)
    dy = sorted(distritos["edition"].unique())
    write_csv(distritos, out / pat.format(NAME="QUADRO-DISTRITOS", YEAR=f"{dy[0]}-{dy[-1]}"), args.overwrite, logger)
    sy = sorted(subdistritos["edition"].unique())
    write_csv(subdistritos, out / pat.format(NAME="QUADRO-SUBDISTRITOS", YEAR=f"{sy[0]}-{sy[-1]}"), args.overwrite, logger)
    gy = cfg["genealogy_years"]
    write_csv(gen, out / pat.format(NAME="GENEALOGIA", YEAR=f"{gy[0]}-{gy[-1]}"), args.overwrite, logger)
    kept = merge_consistency(out / cfg["consistency_name"], cons, LAYERS)
    logger.info("CONSISTENCY: %d rows written for %s (%d rows of other layers kept)", len(cons), LAYERS, kept)


if __name__ == "__main__":
    main()

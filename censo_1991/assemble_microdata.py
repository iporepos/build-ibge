"""
Assemble the IBGE Census 1991 sample microdata into the T1 tables `microdados-domicilios` and
`microdados-pessoas` (files MICRODADOS-DOMICILIOS / MICRODADOS-PESSOAS: sample records, not aggregates).

Source: one dBase file per UF (CD91AMOUP<UF>.DBF), one record per person of a sampled household,
the household answers repeated on every member. Values are recoded with recode_1991.json (see its
header); the municipality comes from the DTB 1991 (7-digit code, prefix = UFNUM + MUNICNUM).

Household and person codes (record order is used ONCE, here, and never again):
  * a household is a block of consecutive records starting at PESSOAN = 1
  * hard checks before any code is assigned (the run stops, nothing is written, on failure):
      - every household variable (layer "microdados-domicilios" in the recode table, after recoding) is the
        same on every record of the block
      - PESSOAN strictly increases inside the block (gaps are allowed and logged)
      - at most one head (PARENDOM = 1); a block without head must be a collective-dwelling
        person (PARENDOM = collective_parendom)
      - every UFNUM + MUNICNUM is a DTB municipality; METROP is constant per municipality
  * household_code = municipio_code + 6-digit sequence per municipality (order of appearance)
  * person_code    = household_code + PESSOAN (2 digits)
From then on records are grouped by code only; the source position of each household is kept in
lineage_households.csv.gz (report folder).

Outputs (output_folder, ';' UTF-8, flags: -9999 not applicable, -2 ignored / doesn't know):
  microdados-domicilios  household_code, municipio_code, situacao_code, situacao_type, household V columns
  microdados-pessoas     person_code, household_code, municipio_code, family and person V columns,
                         municipioanterior_code, municipio1986_code
  CONSISTENCY  layer;code;issue;value;action;note (person-number gaps, garbage cells fixed by the
               recode table) — full runs only; partial runs (--ufs) write it to the report folder
Reports: flags_by_variable.csv, migration_unmatched.csv, municipios_microdata.csv (per
municipality: metropolitan region, households, persons, weights), lineage_households.csv.gz.

Usage:
    python assemble_microdata.py --config assemble_microdata.local.json [--ufs 12,14] [--overwrite]
"""

import argparse
import csv
import gzip
import json
import logging
import re
import shutil
import struct
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

REQUIRED_KEYS = ["data_folder", "dtb_xls", "recode", "output_folder", "report_folder"]
NA_TEXT = "NA"
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
CONSISTENCY_FIELDS = ["layer", "code", "issue", "value", "action", "note"]
DOM, PES = "microdados-domicilios", "microdados-pessoas"  # layer names = recode table "layer"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("data_glob", "*/CD91AMOUP*.DBF")
    cfg.setdefault("table_pattern", "IBGE_CENSO-1991_{NAME}_BRASIL_1991.csv")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    cfg.setdefault("situacao_type", {})
    cfg.setdefault("collective_parendom", "20")
    cfg.setdefault("workers", 4)
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"microdata_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_microdata")
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


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def dbf_layout(path: Path):
    with open(path, "rb") as fh:
        head = fh.read(32)
        nrec, hlen, rlen = struct.unpack("<IHH", head[4:12])
        fields, off = {}, 1  # byte 0 of each record is the deletion flag
        for _ in range((hlen - 33) // 32):
            d = fh.read(32)
            name = d[:11].split(b"\0")[0].decode("ascii")
            fields[name] = (off, d[16], d[17])
            off += d[16]
    return nrec, hlen, rlen, fields


class DBF:
    def __init__(self, path: Path):
        self.path = path
        self.nrec, hlen, rlen, self.fields = dbf_layout(path)
        self.a = np.memmap(path, dtype=np.uint8, mode="r", offset=hlen, shape=(self.nrec, rlen))
        if (self.a[:, 0] == 0x2A).any():
            raise RuntimeError(f"{path.name}: deleted records present")

    def raw(self, name: str) -> np.ndarray:
        off, width, _ = self.fields[name]
        return np.char.strip(self.a[:, off:off + width].copy().view(f"S{width}").ravel())

    def is_real(self, name: str) -> bool:
        return self.fields[name][2] > 0


def read_dtb(path: str) -> pd.DataFrame:
    d = pd.read_excel(path, dtype=str, engine="calamine")
    d.columns = [c.strip() for c in d.columns]
    d["code6"] = d["COD MUN"].str[:6]
    if d["code6"].duplicated().any():
        raise RuntimeError("DTB: duplicated 6-digit prefixes")
    return d


# ---------------------------------------------------------------------------
# Recoding
# ---------------------------------------------------------------------------

def recode(raw: np.ndarray, spec: dict, real: bool, default_blank: int, where: str):
    """Recode one column. Returns (values, hits) where hits counts blank and each map key."""
    n = len(raw)
    out = np.zeros(n, dtype=np.float64 if real else np.int64)
    done = raw == b""
    out[done] = spec.get("blank", default_blank)
    hits = Counter({"<blank>": int(done.sum())})
    for key, val in (spec.get("map") or {}).items():
        m = (raw == key.encode()) & ~done
        if real and NUM_RE.match(key):  # reals may be written with other padding
            m |= ~done & (np.char.str_len(raw) > 0) & _safe_float_eq(raw, float(key))
        if m.any():
            if val is None:
                raise RuntimeError(f"{where}: map value None is only for code columns")
            out[m] = val
            done |= m
            hits[key] = int(m.sum())
    rest = ~done
    if rest.any():
        vals = raw[rest]
        try:
            num = vals.astype(np.float64) if real else vals.astype(np.int64)
        except ValueError:
            bad = sorted({v.decode("latin-1") for v in vals if not NUM_RE.match(v.decode("latin-1"))})
            raise RuntimeError(f"{where}: unmapped non-numeric values {bad[:10]}")
        if spec.get("transform") == "year_1900":
            num = np.where(num > 0, num + 1900, num)
        out[rest] = num
        if spec["class"] == "boolean":
            odd = sorted(set(np.unique(out[rest]).tolist()) - {0, 1})
            if odd:
                raise RuntimeError(f"{where}: boolean with values {odd} after recoding")
    return out, hits


def _safe_float_eq(raw: np.ndarray, value: float) -> np.ndarray:
    s = pd.to_numeric(pd.Series(raw.astype(str)), errors="coerce").to_numpy()
    return s == value


def zfill_codes(raw: np.ndarray, width: int) -> np.ndarray:
    return np.char.zfill(raw.astype(str), width)


# ---------------------------------------------------------------------------
# One UF file
# ---------------------------------------------------------------------------

def process_file(path: str, ctx: dict) -> dict:
    src = DBF(Path(path))
    rec = ctx["recode"]
    vars_ = rec["variables"]
    default_blank = rec["defaults"]["blank"]
    fname = Path(path).name
    n = src.nrec
    errors = []
    cons = []

    # --- municipality ------------------------------------------------------
    uf = src.raw("UFNUM").astype(str)
    code6 = np.char.add(uf, zfill_codes(src.raw("MUNICNUM"), 4))
    to7 = ctx["dtb6"]
    mun = pd.Series(code6).map(to7)
    if mun.isna().any():
        errors.append(f"{fname}: municipalities not in DTB: {sorted(set(code6[mun.isna().to_numpy()]))[:10]}")
        return dict(file=fname, errors=errors)
    mun = mun.to_numpy(dtype=object)

    # --- recode every variable ----------------------------------------------
    values, flags = {}, {}
    for var, spec in vars_.items():
        if spec["layer"] in ("drop", "geo", "municipios") or spec["class"] == "code":
            continue
        try:
            v, hits = recode(src.raw(var), spec, src.is_real(var), default_blank, f"{fname} {var}")
        except RuntimeError as e:
            errors.append(str(e))
            continue
        values[var] = v
        flags[var] = hits
        for key in (spec.get("map") or {}):
            if not NUM_RE.match(key) and hits.get(key):
                raw = src.raw(var)
                for i in np.flatnonzero(raw == key.encode()):
                    cons.append(dict(layer=spec["layer"], code=f"__row{i}", issue="garbage_value",
                                     value=key, action=f"set to {spec['map'][key]}",
                                     note=f"{var} ({spec['name']}) in {fname}, record {i + 1}"))
    if errors:
        return dict(file=fname, errors=errors)
    for var, spec in vars_.items():
        if spec.get("follow_na") and var in values:
            values[var] = np.where(values[spec["follow_na"]] == -9999, -9999, values[var])
    situacao = src.raw("SITSET").astype(str)
    metrop = src.raw("METROP").astype(str)

    # --- household blocks ----------------------------------------------------
    p = values["PESSOAN"].astype(np.int64)
    start = p == 1
    if not start[0]:
        return dict(file=fname, errors=[f"{fname}: first record is not PESSOAN = 1"])
    hid = np.cumsum(start) - 1
    first = np.flatnonzero(start)
    nh = len(first)
    inner = ~start[1:]
    step = np.diff(p)[inner]
    if (step <= 0).any():
        errors.append(f"{fname}: PESSOAN not increasing inside {int((step <= 0).sum())} households")
    hh_vars = [v for v, s in vars_.items() if s["layer"] == DOM and v in values]
    checks = {v: values[v] for v in hh_vars}
    checks["SITSET"] = situacao
    checks["MUNICIPIO"] = mun
    for v, arr in checks.items():
        bad = arr != arr[first[hid]]
        if bad.any():
            errors.append(f"{fname}: household variable {v} differs inside "
                          f"{len(np.unique(hid[bad]))} households")
    par = values["PARENDOM"].astype(np.int64)
    heads = np.bincount(hid, weights=(par == 1), minlength=nh)
    if (heads > 1).any():
        errors.append(f"{fname}: {int((heads > 1).sum())} households with more than one head")
    coll = int(ctx["collective_parendom"])
    no_head = np.flatnonzero(heads == 0)
    if len(no_head):
        not_coll = np.bincount(hid, weights=(par != coll), minlength=nh)[no_head]
        if (not_coll > 0).any():
            errors.append(f"{fname}: {int((not_coll > 0).sum())} households without head that are "
                          f"not collective-dwelling persons")
    mets = defaultdict(set)
    for m, mt in set(zip(mun[first], metrop[first])):
        mets[m].add(mt)
    if errors:
        return dict(file=fname, errors=errors)

    # --- codes -----------------------------------------------------------------
    hh_mun = pd.Series(mun[first])
    seq = hh_mun.groupby(hh_mun, sort=False).cumcount().to_numpy() + 1
    if seq.max() > 999999:
        return dict(file=fname, errors=[f"{fname}: more than 999999 households in a municipality"])
    hh_code = np.char.add(hh_mun.to_numpy().astype(str), np.char.zfill(seq.astype(str), 6))
    person_hh = hh_code[hid]
    person_code = np.char.add(person_hh, np.char.zfill(p.astype(str), 2))
    if len(np.unique(person_code)) != n:
        return dict(file=fname, errors=[f"{fname}: person_code not unique"])

    # gaps in the person numbers (allowed, logged)
    gap_rows = np.flatnonzero(np.concatenate([[False], ~start[1:] & (np.diff(p) > 1)]))
    for h in np.unique(hid[gap_rows]):
        seq_txt = ",".join(str(x) for x in p[first[h]:(first[h + 1] if h + 1 < nh else n)])
        cons.append(dict(layer=PES, code=hh_code[h], issue="person_number_gap", value=seq_txt,
                         action="kept", note=f"PESSOAN skips numbers, {fname}, record {first[h] + 1}"))
    for c in cons:
        if c["code"].startswith("__row"):
            c["code"] = hh_code[hid[int(c["code"][5:])]]

    # --- migration codes -----------------------------------------------------------
    unmatched = Counter()

    def migration(ufvar, muvar, same=None):
        u = src.raw(ufvar).astype(str)
        m = src.raw(muvar).astype(str)
        out = np.full(n, NA_TEXT, dtype=object)
        ok_uf = np.isin(u, ctx["uf_codes"])
        sel = ok_uf & (m != "") & (m != "9999") & (np.char.zfill(m, 4) != "0000")
        c6 = np.char.add(u[sel], np.char.zfill(m[sel], 4))
        c7 = pd.Series(c6).map(to7).to_numpy(dtype=object)
        hit = pd.notna(c7)
        idx = np.flatnonzero(sel)
        out[idx[hit]] = c7[hit]
        for c in c6[~hit]:
            unmatched[(muvar, c)] += 1
        if same is not None:
            s = u == same
            out[s] = mun[s]
        unmatched[(muvar, "__matched")] += int(hit.sum()) + (int((u == same).sum()) if same else 0)
        return out

    anterior = migration("MIANTEUF", "MIANTEMU")
    m1986 = migration("MIMO86UF", "MIMO86MU", same="70")

    # --- write parts -----------------------------------------------------------------
    tmp = Path(ctx["tmp"])
    st = situacao[first]
    dom = {"household_code": hh_code, "municipio_code": mun[first].astype(str),
           "situacao_code": st, "situacao_type": np.array([ctx["situacao_type"].get(x, NA_TEXT) for x in st])}
    for v in hh_vars:
        dom[vars_[v]["name"]] = values[v][first]
    pes = {"person_code": person_code, "household_code": person_hh, "municipio_code": mun.astype(str)}
    for v, s in vars_.items():
        if s["layer"] == PES and v in values:
            pes[s["name"]] = values[v]
    pes["municipioanterior_code"] = anterior.astype(str)
    pes["municipio1986_code"] = m1986.astype(str)
    parts = {}
    for name, cols in ((DOM, dom), (PES, pes)):
        table = pa.table({k: _arrow(v) for k, v in cols.items()})
        out = tmp / f"{name}_{fname}.csv"
        pacsv.write_csv(table, out, write_options=pacsv.WriteOptions(
            include_header=False, delimiter=";", quoting_style="none"))
        parts[name] = str(out)
        parts[name + "_header"] = list(cols)
    lin = tmp / f"lineage_{fname}.csv"
    pd.DataFrame({"household_code": hh_code, "source_file": fname, "source_record": first + 1}) \
        .to_csv(lin, sep=";", index=False, header=False)
    parts["lineage"] = str(lin)

    weights = values["PESO"][first]
    size = np.bincount(hid, minlength=nh)
    by_mun = pd.DataFrame({"municipio_code": hh_mun, "persons": size, "weight": weights,
                           "weighted_persons": weights * size}).groupby("municipio_code") \
        .agg(households=("weight", "size"), persons=("persons", "sum"),
             households_estimated=("weight", "sum"), persons_estimated=("weighted_persons", "sum"))
    return dict(file=fname, errors=[], uf=uf[0], records=n, households=nh, parts=parts, flags=flags,
                consistency=cons, metrop={k: sorted(v) for k, v in mets.items()},
                by_mun=by_mun.reset_index().to_dict("records"), unmatched=unmatched,
                weight_households=float(weights.sum()),
                collective=int(len(no_head)))


def _arrow(v):
    v = np.asarray(v)
    if v.dtype.kind == "i":
        if v.min() >= np.iinfo(np.int32).min and v.max() <= np.iinfo(np.int32).max:
            return pa.array(v.astype(np.int32))
        return pa.array(v)
    if v.dtype.kind == "f":
        return pa.array(v)
    return pa.array(v.astype(str))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def concat(parts: list, header: list, dst: Path):
    tmp = dst.with_name(dst.name + ".part")
    with open(tmp, "wb") as out:
        out.write((";".join(header) + "\n").encode("utf-8"))
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 16 << 20)
    tmp.replace(dst)


def write_csv(path: Path, rows: list, fields: list):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--ufs", help="Comma-separated UF codes for a partial (test) run")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)
    with open(cfg["recode"], encoding="utf-8") as f:
        rec = json.load(f)

    files = sorted(str(p) for p in Path(cfg["data_folder"]).glob(cfg["data_glob"]))
    files.sort(key=lambda p: Path(p).stem[-2:])
    if args.ufs:
        want = {x.strip() for x in args.ufs.split(",")}
        files = [f for f in files if Path(f).stem[-2:] in want]
    if not files:
        sys.exit("No input files")
    full = not args.ufs
    out_dir = Path(cfg["output_folder"])
    names = {k: out_dir / cfg["table_pattern"].format(NAME=k.upper()) for k in (DOM, PES)}
    if not args.overwrite and any(p.exists() for p in names.values()):
        sys.exit(f"Outputs exist in {out_dir} (use --overwrite)")
    _, _, _, layout = dbf_layout(Path(files[0]))
    missing = sorted(set(layout) - set(rec["variables"])) + sorted(set(rec["variables"]) - set(layout))
    if missing:
        sys.exit(f"Recode table and DBF layout differ: {missing}")

    dtb = read_dtb(cfg["dtb_xls"])
    tmp = out_dir / ".assemble_microdata_tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    ctx = dict(recode=rec, dtb6=dict(zip(dtb["code6"], dtb["COD MUN"])), tmp=str(tmp),
               uf_codes=sorted(dtb["UF"].unique()), situacao_type=cfg["situacao_type"],
               collective_parendom=cfg["collective_parendom"])
    logger.info("%d files, recode table %s (%d variables)", len(files), cfg["recode"], len(rec["variables"]))

    results = []
    try:
        with ProcessPoolExecutor(max_workers=cfg["workers"]) as ex:
            for r in ex.map(process_file, files, [ctx] * len(files)):
                if r["errors"]:
                    for e in r["errors"]:
                        logger.error(e)
                    raise SystemExit("Checks failed — nothing written")
                logger.info("  %-18s %9d persons %8d households (%d collective-dwelling persons)",
                            r["file"], r["records"], r["households"], r["collective"])
                results.append(r)

        for name, dst in names.items():
            concat([r["parts"][name] for r in results], results[0]["parts"][name + "_header"], dst)
            logger.info("Wrote %s", dst)
        with gzip.open(report / "lineage_households.csv.gz", "wb") as g:
            g.write(b"household_code;source_file;source_record\n")
            for r in results:
                with open(r["parts"]["lineage"], "rb") as f:
                    shutil.copyfileobj(f, g)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- reports -----------------------------------------------------------------------
    persons = sum(r["records"] for r in results)
    households = sum(r["households"] for r in results)
    logger.info("Sample: %d persons, %d households", persons, households)
    cons = [c for r in results for c in r["consistency"]]
    cons_path = (out_dir if full else report) / cfg["consistency_name"]
    kept = []
    if cons_path.exists():  # rows of other layers (geography) are kept
        with open(cons_path, encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter=";") if r["layer"] not in (DOM, PES, "domicilios", "pessoas")]
    write_csv(cons_path, kept + cons, CONSISTENCY_FIELDS)
    logger.info("Consistency: %d rows (+%d kept from other layers) -> %s %s", len(cons), len(kept), cons_path,
                Counter(c["issue"] for c in cons))

    flags = defaultdict(Counter)
    for r in results:
        for v, c in r["flags"].items():
            flags[v].update(c)
    rows = []
    for v, c in flags.items():
        spec = rec["variables"][v]
        for key, cnt in sorted(c.items()):
            if cnt:
                to = spec.get("blank", rec["defaults"]["blank"]) if key == "<blank>" else spec["map"][key]
                rows.append(dict(variable=v, name=spec["name"], source_value=key, t1_value=to, records=cnt))
    write_csv(report / "flags_by_variable.csv", rows, ["variable", "name", "source_value", "t1_value", "records"])

    metrop = defaultdict(set)
    for r in results:
        for k, v in r["metrop"].items():
            metrop[k].update(v)
    varying = {k: v for k, v in metrop.items() if len(v) > 1}
    if varying:
        logger.error("METROP varies within municipalities: %s", list(varying.items())[:5])
    by_mun = pd.DataFrame([x for r in results for x in r["by_mun"]])
    by_mun["regiaometropolitana_code"] = by_mun["municipio_code"].map(
        lambda m: NA_TEXT if min(metrop[m]) == "0" else min(metrop[m]))
    by_mun[["households_estimated", "persons_estimated"]] = by_mun[["households_estimated", "persons_estimated"]].round(2)
    by_mun.to_csv(report / "municipios_microdata.csv", sep=";", index=False)
    logger.info("Municipalities %d; estimated (weighted) households %.0f, persons %.0f", len(by_mun),
                by_mun["households_estimated"].sum(), by_mun["persons_estimated"].sum())

    un = Counter()
    for r in results:
        un.update(r["unmatched"])
    rows = [dict(variable=k[0], code=k[1], count=v) for k, v in un.most_common()]
    write_csv(report / "migration_unmatched.csv", rows, ["variable", "code", "count"])
    for var in ("MIANTEMU", "MIMO86MU"):
        ok = un.get((var, "__matched"), 0)
        bad = sum(v for k, v in un.items() if k[0] == var and k[1] != "__matched")
        logger.info("%s -> code: %d matched, %d not in the DTB 1991 (see migration_unmatched.csv)",
                    var, ok, bad)
    if not full:
        logger.warning("Partial run (--ufs %s): tables hold only these UFs", args.ufs)


if __name__ == "__main__":
    main()

"""
Profile the IBGE Census 1991 sample microdata (CD91AMOUP<UF>.DBF) against its dictionary.

Read-only on the sources. For every DBF field it counts the actual values over all the files
and compares them with the dictionary (Dicionario 1991.xls): declared range, NSA / ignored codes
and category labels. The output is the basis of the recoding table used to build T1:

  profile_variables.csv  one row per variable: dictionary metadata, blanks, non-numeric values,
                         distinct values, min/max of the numeric values, counts of the declared
                         NSA and ignored codes, values out of the declared range, values that
                         are not a declared category, and a PROPOSED class to review:
                           text          STRING fields (names, codes)
                           boolean       two answer categories that read as yes / no
                           categorical   coded answers with labels
                           quantity      measured values (income, age, counts...), where the
                                         labelled codes are special values (NSA, ignored...)
  profile_values.csv     variable;value;count;label;declared — every distinct value of the
                         variables with at most value_limit distinct values; for the others only
                         the labelled codes, the NSA / ignored codes and out-of-range values.

Usage:
    python profile_microdata.py --config profile_microdata.local.json
"""

import argparse
import csv
import json
import logging
import re
import struct
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED_KEYS = ["data_folder", "dictionary_xls", "report_folder"]
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
SPECIAL_RE = re.compile(r"\b(nsa|ignorad[oa]|nao sabe|nao lembra)\b")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("data_glob", "*/CD91AMOUP*.DBF")
    cfg.setdefault("value_limit", 150)
    cfg.setdefault("no_value_counts", ["PESO"])
    cfg.setdefault("yes_labels", ["tem", "sim", "ler e escrever"])
    cfg.setdefault("no_labels", ["nao tem", "nao", "nao sabe"])
    cfg.setdefault("workers", 8)
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"profile_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_profile")
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


def fold(text: str) -> str:
    """Lower case without accents, for label matching."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------

def read_dictionary(path: str) -> dict:
    """{variable: {...}} from the 'Layout' sheet. Category rows follow their variable row:
    code in column 2, label in column 3. Rows without a type (DOMICIL, FAMILIA, PESSOA) open
    a section."""
    d = pd.read_excel(path, header=None, dtype=str, engine="calamine").fillna("")
    out, cur, section = {}, None, ""
    for _, r in d.iloc[2:].iterrows():
        name = r[1].strip()
        if name:
            if not r[3].strip() and not r[6].strip():
                section = name
                cur = None
                continue
            cur = dict(number=r[0].strip(), description=r[2].strip(), type=r[3].strip(),
                       min=r[6].strip(), max=r[7].strip(), decimals=r[8].strip(),
                       nsa=r[9].strip(), ignored=r[10].strip(), section=section, categories={})
            out[name] = cur
        elif cur is not None and r[2].strip() and r[2].strip() != "Categorías":
            cur["categories"][r[2].strip()] = r[3].strip()
    return out


# ---------------------------------------------------------------------------
# DBF reading
# ---------------------------------------------------------------------------

def dbf_layout(path: Path):
    with open(path, "rb") as fh:
        head = fh.read(32)
        nrec, hlen, rlen = struct.unpack("<IHH", head[4:12])
        fields, off = [], 1  # byte 0 of each record is the deletion flag
        for _ in range((hlen - 33) // 32):
            d = fh.read(32)
            name = d[:11].split(b"\0")[0].decode("ascii")
            fields.append((name, chr(d[11]), off, d[16], d[17]))
            off += d[16]
    return nrec, hlen, rlen, fields


def profile_file(path: str, skip: list) -> tuple:
    """(file, records, deleted, {field: Counter of stripped values})."""
    nrec, hlen, rlen, fields = dbf_layout(Path(path))
    a = np.memmap(path, dtype=np.uint8, mode="r", offset=hlen, shape=(nrec, rlen))
    deleted = int((a[:, 0] == 0x2A).sum())
    out = {}
    for name, _, off, width, _ in fields:
        col = np.char.strip(a[:, off:off + width].copy().view(f"S{width}").ravel())
        if name in skip:
            # keep blanks / non-numeric visible, collapse the numeric values to min/max
            vals = col.astype(str)
            num = pd.to_numeric(pd.Series(vals), errors="coerce")
            c = Counter({v: int(n) for v, n in Counter(vals[num.isna().to_numpy()]).items()})
            c["__min__"], c["__max__"] = float(num.min()), float(num.max())
            out[name] = c
            continue
        vals, counts = np.unique(col, return_counts=True)
        out[name] = Counter({v.decode("latin-1"): int(n) for v, n in zip(vals, counts)})
    return path, nrec, deleted, out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def propose_class(meta: dict, cfg: dict) -> str:
    if meta is None:
        return "unknown"
    if meta["type"] == "STRING":
        return "text"
    cats = {k: fold(v) for k, v in meta["categories"].items()}
    answers = {k: v for k, v in cats.items() if not SPECIAL_RE.search(v)}
    yes, no = set(cfg["yes_labels"]), set(cfg["no_labels"])
    if len(answers) == 2 and {v in yes for v in answers.values()} == {True, False} \
            and all(v in yes | no for v in answers.values()):
        return "boolean"
    lo, hi = meta["min"], meta["max"]
    span = (int(float(hi)) - int(float(lo)) + 1) if NUM_RE.match(lo or "x") and NUM_RE.match(hi or "x") else 0
    # labelled codes covering (almost) the whole declared range -> coded answers
    if len(answers) >= 3 and span and len(answers) >= 0.6 * min(span, 100):
        return "categorical"
    if meta["type"] == "REAL" or span > 30 or len(answers) <= 2:
        return "quantity"
    return "categorical"


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()
    cfg = load_config(args.config)
    report = Path(cfg["report_folder"])
    logger = setup_logging(report)

    dic = read_dictionary(cfg["dictionary_xls"])
    logger.info("Dictionary: %d variables", len(dic))
    files = sorted(str(p) for p in Path(cfg["data_folder"]).glob(cfg["data_glob"]))
    if not files:
        sys.exit(f"No files match {cfg['data_glob']} in {cfg['data_folder']}")
    _, _, _, fields = dbf_layout(Path(files[0]))
    layout = {f[0]: f for f in fields}
    logger.info("%d files, %d fields", len(files), len(fields))

    total = {name: Counter() for name in layout}
    nrec_all = 0
    with ProcessPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = [ex.submit(profile_file, f, cfg["no_value_counts"]) for f in files]
        for fut in futs:
            path, nrec, deleted, counts = fut.result()
            if dbf_layout(Path(path))[3] != fields:
                sys.exit(f"Field layout of {path} differs from {files[0]}")
            nrec_all += nrec
            logger.info("  %-18s %10d records  %d deleted", Path(path).name, nrec, deleted)
            for name, c in counts.items():
                mn, mx = c.pop("__min__", None), c.pop("__max__", None)
                total[name].update(c)
                if mn is not None:
                    t = total[name]
                    t["__min__"] = mn if "__min__" not in t else min(t["__min__"], mn)
                    t["__max__"] = mx if "__max__" not in t else max(t["__max__"], mx)
    logger.info("Total records: %d", nrec_all)

    missing = sorted(set(layout) - set(dic))
    extra = sorted(set(dic) - set(layout))
    if missing:
        logger.warning("Fields without dictionary entry: %s", missing)
    if extra:
        logger.warning("Dictionary entries without field: %s", extra)

    vrows, valrows = [], []
    for name, (_, ftype, _, width, dec) in layout.items():
        c = Counter(total[name])
        mn_skip, mx_skip = c.pop("__min__", None), c.pop("__max__", None)
        meta = dic.get(name)
        cats = meta["categories"] if meta else {}
        nsa, ign = (meta["nsa"], meta["ignored"]) if meta else ("", "")
        blank = c.get("", 0)
        nonnum = {v: n for v, n in c.items() if v and not NUM_RE.match(v)}
        nums = {float(v): n for v, n in c.items() if v and NUM_RE.match(v)}
        norm = lambda v: str(int(float(v))) if NUM_RE.match(v) and float(v) == int(float(v)) else v
        count_of = lambda code: sum(n for v, n in c.items() if v and NUM_RE.match(v)
                                    and code and NUM_RE.match(code) and float(v) == float(code))
        lo = float(meta["min"]) if meta and NUM_RE.match(meta["min"] or "x") else None
        hi = float(meta["max"]) if meta and NUM_RE.match(meta["max"] or "x") else None
        specials = {float(k) for k in cats if NUM_RE.match(k)} | \
                   {float(x) for x in (nsa, ign) if NUM_RE.match(x or "x")}
        out_range = sum(n for v, n in nums.items()
                        if lo is not None and hi is not None and not lo <= v <= hi and v not in specials)
        cls = propose_class(meta, cfg)
        undeclared = 0
        if cls in ("categorical", "boolean"):
            declared = {float(k) for k in cats if NUM_RE.match(k)} | specials
            undeclared = sum(n for v, n in nums.items() if v not in declared)
        vrows.append(dict(
            variable=name, number=meta["number"] if meta else "", section=meta["section"] if meta else "",
            description=meta["description"] if meta else "", dict_type=meta["type"] if meta else "",
            dbf_type=f"{ftype}{width}" + (f".{dec}" if dec else ""), proposed_class=cls,
            n_blank=blank, n_nonnumeric=sum(nonnum.values()),
            nonnumeric_sample="|".join(list(nonnum)[:5]),
            n_distinct=len(c) if mn_skip is None else "",
            min=(min(nums) if nums else "") if mn_skip is None else mn_skip,
            max=(max(nums) if nums else "") if mn_skip is None else mx_skip,
            dict_min=meta["min"] if meta else "", dict_max=meta["max"] if meta else "",
            nsa_code=nsa, nsa_count=count_of(nsa), ignored_code=ign,
            ignored_count=count_of(ign) if ign != nsa else "",
            n_out_of_range=out_range, n_undeclared_codes=undeclared,
            n_categories=len(cats),
            categories="; ".join(f"{k}={v}" for k, v in cats.items())))
        small = mn_skip is None and len(c) <= cfg["value_limit"]
        labels = {norm(k): v for k, v in cats.items()}
        for v, n in sorted(c.items(), key=lambda kv: (not NUM_RE.match(kv[0] or "x"),
                                                         float(kv[0]) if NUM_RE.match(kv[0] or "x") else 0,
                                                         kv[0])):
            key = norm(v) if v else v
            is_num = bool(v) and bool(NUM_RE.match(v))
            special = is_num and float(v) in specials
            oor = is_num and lo is not None and hi is not None and not lo <= float(v) <= hi
            if small or special or oor or not is_num:
                valrows.append(dict(variable=name, value=v if v else "<blank>", count=n,
                                    label=labels.get(key, ""),
                                    declared="yes" if key in labels or special else "no"))

    for fname, rows in (("profile_variables.csv", vrows), ("profile_values.csv", valrows)):
        with open(report / fname, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";", lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        logger.info("Wrote %s (%d rows)", report / fname, len(rows))
    by_class = Counter(r["proposed_class"] for r in vrows)
    logger.info("Proposed classes: %s", dict(by_class))
    flagged = [r["variable"] for r in vrows if r["n_out_of_range"] or r["n_undeclared_codes"]
               or r["n_nonnumeric"] and r["proposed_class"] != "text"]
    logger.info("Variables with out-of-range / undeclared / non-numeric values: %d %s",
                len(flagged), flagged)


if __name__ == "__main__":
    main()

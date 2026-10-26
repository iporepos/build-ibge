"""
Infer the 1991 census tract of each sampled household (T1 link table `microdados-setores`).

The sample microdata carry no tract code (finest geography: municipality + tract situation SITSET),
but the records are stored tract by tract and the expansion weight PESO is calibrated per tract.
The tract is recovered per municipality:
  1. blocks: the households of the municipality, in record order, cut where CD107 (number of the
     dwelling on the tract listing sheet, restarts in every tract) does not increase or SITSET changes
  2. a monotone dynamic program assigns consecutive blocks (0 to max_blocks per tract) to the
     municipality's tracts in setor_code order (DIST, SUBD, SETOR), minimizing
       |weight of the private households of the blocks - private households of the tract| / sqrt(that + 25)
       + situacao_penalty per block whose SITSET differs from the tract situacao_code
       + merge_penalty per block beyond the first
     (max_blocks is raised to twice the blocks per tract where a municipality has more; a search
     restricted to a band around the expected position was tried and dropped: faster, but it misplaced
     tracts in small UFs such as RR)
The result is an inference, not an IBGE code: `setor_code_inferred`. It is meant for sample-based
aggregation by tract; the municipality stays the strong link of the microdata.

Inputs: the DBF microdata (same files as assemble_microdata.py), its lineage report
(household_code -> source_file, source_record of the first person) and the T1 tract tables basico
(setor_code, municipio_code, situacao_code) and domicilio (private households = universe_variable).
Output (T1): IBGE_CENSO-1991_MICRODADOS-SETORES_BRASIL_1991.csv
  household_code; setor_code_inferred; inference_block (block number within the municipality)
Reports (report_folder): tracts_inferred.csv (per tract: sample households, blocks, weight vs
universe), validation by UF (tract shares of the sample vs the universe, with a shifted-by-one
baseline), log. CONSISTENCY (layer microdados-setores): municipalities with tracts that received no
sampled household, municipalities with more blocks per tract than max_blocks (weaker inference).

Usage:
    python infer_tracts.py --config infer_tracts.local.json [--overwrite]
"""

import argparse
import json
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from assemble_geography import merge_consistency
from assemble_microdata import DBF

REQUIRED_KEYS = ["microdata_folder", "lineage", "basico", "domicilio", "output_folder", "report_folder"]
LAYER = "microdados-setores"
SITS = tuple(range(1, 9))
INF = 1e18


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("microdata_glob", "*/CD91AMOUP*.DBF")
    cfg.setdefault("output_name", "IBGE_CENSO-1991_MICRODADOS-SETORES_BRASIL_1991.csv")
    cfg.setdefault("consistency_name", "IBGE_CENSO-1991_CONSISTENCY_X_X.csv")
    cfg.setdefault("universe_variable", "V0068")
    cfg.setdefault("collective_especie", 3)
    cfg.setdefault("max_blocks", 40)
    cfg.setdefault("situacao_penalty", 3.0)
    cfg.setdefault("merge_penalty", 1.0)
    cfg.setdefault("workers", 8)
    cfg.setdefault("validation", [])
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"infer_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("censo1991_infer")
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
# Households in record order
# ---------------------------------------------------------------------------

def read_households(path: Path, lin: pd.DataFrame, extra: list) -> pd.DataFrame:
    """Fields of the first record of each household of one DBF, in record order."""
    d = DBF(path)
    rec = lin.source_record.to_numpy() - 1
    if len(rec) and (rec.max() >= d.nrec or (np.diff(rec) <= 0).any()):
        raise SystemExit(f"{path.name}: lineage records out of range or not increasing")
    out = pd.DataFrame({"household_code": lin.household_code.to_numpy()})
    for c in ["UFNUM", "MUNICNUM", "SITSET", "CD107", "PESO", "ESPECIE", *extra]:
        raw = d.raw(c)[rec].astype(str)
        out[c] = raw if c in ("UFNUM", "MUNICNUM") else pd.to_numeric(pd.Series(raw), errors="coerce").to_numpy()
    if (out.UFNUM + out.MUNICNUM.str.zfill(4) != out.household_code.str[:6]).any():
        raise SystemExit(f"{path.name}: household_code municipality differs from UFNUM + MUNICNUM")
    out["source_file"] = path.name
    return out


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def align(bw, bsit, target, tsit, K, cfg):
    """Block -> tract index (0-based) for one municipality (consecutive blocks, 0 to K per tract)."""
    B, T = len(bw), len(target)
    cw = np.concatenate([[0.0], np.cumsum(bw)])
    mis = {s: np.concatenate([[0], np.cumsum(bsit != s)]) for s in SITS}
    other = np.concatenate([[0], np.arange(1, B + 1)])
    D = np.full(B + 1, INF)
    D[0] = 0.0
    choices = []
    ends = np.arange(B + 1)
    for j in range(T):
        scale = np.sqrt(target[j] + 25.0)
        m = mis.get(int(tsit[j]), other)
        best = np.full(len(ends), INF)
        ch = np.zeros(len(ends), dtype=np.int16)
        for k in range(0, K + 1):
            starts = ends - k
            ok = starts >= 0
            if not ok.any():
                break
            s, e = starts[ok], ends[ok]
            cand = (D[s] + np.abs(cw[e] - cw[s] - target[j]) / scale
                    + cfg["situacao_penalty"] * (m[e] - m[s]) + cfg["merge_penalty"] * max(k - 1, 0))
            cur = best[ok]
            better = cand < cur
            cur[better] = cand[better]
            best[ok] = cur
            c2 = ch[ok]
            c2[better] = k
            ch[ok] = c2
        D = best
        choices.append(ch)
    if D[B] >= INF:
        raise SystemExit("alignment found no path (unexpected with K >= blocks per tract)")
    assign = np.full(B, -1)
    i = B
    for j in range(T - 1, -1, -1):
        k = int(choices[j][i])
        assign[i - k:i] = j
        i -= k
    return assign


def shares_score(est: pd.DataFrame, uni: pd.DataFrame, group: pd.Series, shift: int = 0) -> dict:
    u = uni.groupby(group).shift(shift) if shift else uni
    j = est.join(u, rsuffix="_u").dropna()
    out = {}
    for k in est.columns:
        if len(j) > 2:
            out[k] = (round(float(np.corrcoef(j[k], j[k + "_u"])[0, 1]), 3),
                      round(float((j[k] - j[k + "_u"]).abs().mean()), 4))
    return out


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
    out_dir = Path(cfg["output_folder"])
    out_path = out_dir / cfg["output_name"]
    if out_path.exists() and not args.overwrite:
        sys.exit(f"{out_path} exists (use --overwrite)")

    # --- tracts ------------------------------------------------------------------------
    uv = cfg["universe_variable"]
    val_vars = sorted({v for x in cfg["validation"] for v in x["tract_numerator"] + [x["tract_denominator"]]})
    tr = pd.read_csv(cfg["basico"], sep=";", dtype=str, usecols=["setor_code", "municipio_code", "situacao_code"])
    dom = pd.read_csv(cfg["domicilio"], sep=";", dtype={"setor_code": str}, usecols=["setor_code", uv, *val_vars])
    tr = tr.merge(dom, on="setor_code", how="left", validate="1:1").sort_values("setor_code").reset_index(drop=True)
    if tr[uv].isna().any():
        sys.exit("domicilio lacks tracts of basico")
    tr["situacao_code"] = tr.situacao_code.astype(int)
    logger.info("Tracts: %d in %d municipalities (universe variable %s = %d)", len(tr), tr.municipio_code.nunique(),
                uv, int(tr[uv].sum()))

    # --- households ----------------------------------------------------------------------
    lin = pd.read_csv(cfg["lineage"], sep=";", dtype={"household_code": str, "source_file": str, "source_record": np.int64})
    files = {Path(p).name: Path(p) for p in Path(cfg["microdata_folder"]).glob(cfg["microdata_glob"])}
    lost = sorted(set(lin.source_file) - set(files))
    if lost:
        sys.exit(f"lineage names files not found: {lost}")
    extra = sorted({x["micro_variable"] for x in cfg["validation"]})
    groups = [(files[f], g.sort_values("source_record")) for f, g in lin.groupby("source_file", sort=True)]
    with ThreadPoolExecutor(cfg["workers"]) as ex:
        parts = list(ex.map(lambda a: read_households(a[0], a[1], extra), groups))
    hh = pd.concat(parts, ignore_index=True)
    hh["municipio_code"] = hh.household_code.str[:7]
    logger.info("Households: %d from %d files (CD107 not numeric: %d)", len(hh), len(groups), int(hh.CD107.isna().sum()))
    runs = hh.municipio_code.ne(hh.municipio_code.shift()).sum()
    if runs != hh.municipio_code.nunique():
        logger.warning("Municipalities split in %d runs (%d municipalities): blocks are cut at every run start",
                       runs, hh.municipio_code.nunique())
    miss = sorted(set(hh.municipio_code) - set(tr.municipio_code))
    if miss:
        sys.exit(f"Microdata municipalities without tracts: {miss[:10]}")

    # --- blocks ----------------------------------------------------------------------------
    new = ((hh.CD107.diff() <= 0) | (hh.SITSET != hh.SITSET.shift()) | (hh.municipio_code != hh.municipio_code.shift()))
    hh["blk"] = new.cumsum() - 1
    hh["wpriv"] = hh.PESO.where(hh.ESPECIE != cfg["collective_especie"], 0.0)
    bl = hh.groupby("blk").agg(m=("municipio_code", "first"), sit=("SITSET", "first"), w=("wpriv", "sum"))
    hh["inference_block"] = hh.groupby("municipio_code").blk.transform(lambda s: s - s.min() + 1)
    logger.info("Blocks: %d (%.2f per tract)", len(bl), len(bl) / len(tr))

    # --- alignment per municipality --------------------------------------------------------------
    tract_of_block = np.full(len(bl), -1)
    cons = []
    tidx = tr.groupby("municipio_code").indices
    K0 = int(cfg["max_blocks"])
    for mun, b in bl.groupby("m", sort=False):
        ti = tidx[mun]
        t = tr.iloc[ti]
        K = K0
        if len(b) > K0 * len(t):
            K = 2 * int(np.ceil(len(b) / len(t)))
            cons.append(dict(layer=LAYER, code=mun, issue="many_blocks_per_tract", value=f"{len(b)} blocks, {len(t)} tracts",
                             action=f"max_blocks raised to {K}", note="CD107 restarts far more often than tracts change; "
                                                                     "weaker inference in this municipality"))
        a = align(b.w.to_numpy(), b.sit.to_numpy(), t[uv].to_numpy(float), t.situacao_code.to_numpy(), K, cfg)
        tract_of_block[b.index.to_numpy()] = ti[a]
    hh["tract"] = tract_of_block[hh.blk.to_numpy()]
    hh["setor_code_inferred"] = tr.setor_code.to_numpy()[hh.tract.to_numpy()]
    logger.info("Alignment done")

    # --- quality per tract -------------------------------------------------------------------------
    q = hh.groupby("tract").agg(sample_households=("household_code", "size"), blocks=("blk", "nunique"),
                                weight_private=("wpriv", "sum"),
                                situacao_mismatch=("SITSET", "size"))
    sit_bad = (hh.SITSET.to_numpy() != tr.situacao_code.to_numpy()[hh.tract.to_numpy()])
    q["situacao_mismatch"] = pd.Series(sit_bad, index=hh.index).groupby(hh.tract).sum()
    q = tr[["setor_code", "municipio_code", "situacao_code", uv]].rename(columns={uv: "universe_private"}).join(q)
    q[["sample_households", "blocks", "situacao_mismatch"]] = q[["sample_households", "blocks", "situacao_mismatch"]].fillna(0).astype(int)
    q["weight_private"] = q.weight_private.fillna(0).round(2)
    q["weight_ratio"] = (q.weight_private / q.universe_private.replace(0, np.nan)).round(3)
    q.to_csv(report / "tracts_inferred.csv", sep=";", index=False)
    empty = q[q.sample_households == 0]
    for mun, g in empty.groupby("municipio_code"):
        cons.append(dict(layer=LAYER, code=mun, issue="tracts_without_sample", value=str(len(g)), action="kept",
                         note=f"tracts with no inferred sampled household (universe private households: "
                              f"{int(g.universe_private.sum())}); list in tracts_inferred.csv"))
    d = (q.weight_private - q.universe_private).abs()
    logger.info("Tracts with sampled households: %d of %d; weight = universe (<0.5): %.1f%%, within 10%%: %.1f%%; "
                "households in a tract of another situacao: %d", int((q.sample_households > 0).sum()), len(q),
                100 * (d < 0.5).mean(), 100 * (d <= 0.1 * q.universe_private + 0.5).mean(), int(sit_bad.sum()))

    # --- validation: tract shares, sample vs universe ----------------------------------------------
    if cfg["validation"]:
        p = hh[hh.ESPECIE == 1]
        est, uni = pd.DataFrame(), pd.DataFrame()
        for x in cfg["validation"]:
            num = p[x["micro_variable"]].isin(x["micro_codes"]) * p.PESO
            g = pd.DataFrame({"n": num, "w": p.PESO, "t": p.tract}).groupby("t").sum()
            est[x["name"]] = g.n / g.w.replace(0, np.nan)
            uni[x["name"]] = tr[x["tract_numerator"]].sum(axis=1) / tr[x["tract_denominator"]].replace(0, np.nan)
        rows = []
        uf = tr.setor_code.str[:2]
        for u in sorted(uf.unique()):
            sel = uf == u
            e = est[est.index.isin(np.flatnonzero(sel))]
            a = shares_score(e, uni[sel], tr.municipio_code[sel])
            b = shares_score(e, uni[sel], tr.municipio_code[sel], shift=1)
            for k in a:
                rows.append(dict(unidade_code=u, share=k, corr=a[k][0], mean_abs_diff=a[k][1],
                                 corr_shift1=b.get(k, (np.nan,))[0], mean_abs_diff_shift1=b.get(k, (np.nan, np.nan))[1]))
        val = pd.DataFrame(rows)
        val.to_csv(report / "validation_by_uf.csv", sep=";", index=False)
        for k, g in val.groupby("share"):
            logger.info("Validation %-10s corr %.3f-%.3f (shifted by one tract: %.3f-%.3f)", k, g["corr"].min(),
                        g["corr"].max(), g.corr_shift1.min(), g.corr_shift1.max())

    # --- write ----------------------------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    hh.sort_values("household_code")[["household_code", "setor_code_inferred", "inference_block"]] \
        .to_csv(tmp, sep=";", index=False, lineterminator="\n")
    tmp.replace(out_path)
    logger.info("Wrote %s (%d households)", out_path, len(hh))
    cons_path = out_dir / cfg["consistency_name"]
    kept = merge_consistency(cons_path, cons, (LAYER,))
    logger.info("Consistency: %d rows (+%d kept from other layers) -> %s %s", len(cons), kept, cons_path,
                dict(Counter(r["issue"] for r in cons)))


if __name__ == "__main__":
    main()

"""
Download the PNSB (Pesquisa Nacional de Saneamento Básico) tables of SIDRA at municipal level into the SRC of
one PNSB edition (PNSB-<year>; one config per year).

The survey's table list comes from the IBGE aggregates API (servicodados, /api/v3/agregados); for every table
that has the configured year(s) and the municipal level (N6) the metadata is saved and the values are
fetched from apisidra (/values, codes only), cut into requests of at most max_values values (SIDRA refuses
more than 50,000). The responses of one table and year are saved together as one reproducible gzip CSV
(header from the API, same bytes for the same content). Existing files are kept unless --overwrite.

Checks: every response has the expected header; every request returns exactly locations x categories rows;
every slice of a table has the same set of municipalities; no (municipality, variable, categories) key twice.
A failed table is logged and the script exits non-zero after trying the others.

Outputs (output_folder): SIDRA_SB_TABLES.json (the survey's table list), meta/T<table>.json (metadata of the
tables of the year), values/T<table>_<year>_N6.csv.gz, and the manifest (table, year, rows, municipalities, sha256, time).
Log in report_folder.

CONFIG (JSON, --config)
  required:
    output_folder   SRC folder of the SIDRA files (e.g. <IBGE root>/PNSB-2017/V1/SRC/SIDRA)
    report_folder   log folder
    survey          SIDRA survey code ("SB" = PNSB)
    years           years to fetch, e.g. [2017]
  optional:
    tables          only these table codes (default: every table of the survey with a configured year and N6)
    level           territorial level (default "N6")
    max_values      values per request (default 50000)
    expected_locations  locations used to cut requests (default 5600)
    workers         parallel requests (default 4)
    timeout         seconds per request (default 300)
    retries         attempts per request (default 4)
    manifest_name   default "SIDRA_SB_MANIFEST.csv"
    api_tables      default "https://servicodados.ibge.gov.br/api/v3/agregados"
    api_values      default "https://apisidra.ibge.gov.br/values"

Usage:
    python download_pnsb.py --config download_pnsb_2017.local.json [--dry-run] [--overwrite] [--tables 7460 7484]
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["output_folder", "report_folder", "survey", "years"]
MANIFEST_COLUMNS = ["table", "year", "level", "file", "rows", "municipalities", "requests", "sha256", "downloaded_at"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("level", "N6")
    cfg.setdefault("max_values", 50000)
    cfg.setdefault("expected_locations", 5600)
    cfg.setdefault("workers", 4)
    cfg.setdefault("timeout", 300)
    cfg.setdefault("retries", 4)
    cfg.setdefault("manifest_name", f"SIDRA_{cfg['survey']}_MANIFEST.csv")
    cfg.setdefault("api_tables", "https://servicodados.ibge.gov.br/api/v3/agregados")
    cfg.setdefault("api_values", "https://apisidra.ibge.gov.br/values")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"download_pnsb_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("download_pnsb")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def get_json(url: str, cfg: dict, logger):
    for attempt in range(1, cfg["retries"] + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (build-ibge downloader)"})
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":  # servicodados answers gzip even unasked
                body = gzip.decompress(body)
            try:
                return json.loads(body)
            except ValueError:
                raise RuntimeError(f"not JSON: {body[:200]!r}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            if attempt == cfg["retries"] or (isinstance(exc, RuntimeError) and b"excedeu" in str(exc).encode()):
                raise RuntimeError(f"{url}: {exc}")
            logger.debug("retry %d %s: %s", attempt, url, exc)
            time.sleep(5 * attempt)


def write_gzip(path: Path, text: str) -> str:
    """Reproducible gzip (no name, mtime 0); returns the sha256 of the file."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=6) as g:
        g.write(text.encode("utf-8"))
    data = buf.getvalue()
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)
    return hashlib.sha256(data).hexdigest()


def slices(meta: dict, cfg: dict) -> list:
    """Requests of one table: [(variable ids, [(class id, category ids or None = all)])], each under max_values.

    Classifications are taken from the last one backwards while the product of their categories fits; the
    first one that does not fit is cut into chunks; the earlier ones are fixed one category at a time."""
    cap = max(1, cfg["max_values"] // cfg["expected_locations"])
    classes = [(c["id"], [k["id"] for k in c["categorias"]]) for c in meta["classificacoes"]]
    variables = [v["id"] for v in meta["variaveis"]]
    if not classes:
        return [(variables[i:i + cap], []) for i in range(0, len(variables), cap)]
    k, prod = len(classes), 1
    while k > 0 and prod * len(classes[k - 1][1]) <= cap:
        k -= 1
        prod *= len(classes[k][1])
    tail = [(cid, None) for cid, _ in classes[k:]]
    if k == 0:
        return [([v], tail) for v in variables]
    cut_id, cut_cats = classes[k - 1]
    step = max(1, cap // prod)
    chunks = [cut_cats[i:i + step] for i in range(0, len(cut_cats), step)]
    heads = [[]]
    for cid, cats in classes[:k - 1]:
        heads = [h + [(cid, [c])] for h in heads for c in cats]
    return [([v], h + [(cut_id, ch)] + tail) for v in variables for h in heads for ch in chunks]


def expected_combos(meta: dict, sl: tuple) -> int:
    sizes = {c["id"]: len(c["categorias"]) for c in meta["classificacoes"]}
    return len(sl[0]) * math.prod(sizes[cid] if cats is None else len(cats) for cid, cats in sl[1])


def slice_url(cfg: dict, table: int, year: int, sl: tuple, classes: list) -> str:
    vs, cl = sl
    parts = [cfg["api_values"], "t", str(table), cfg["level"].lower(), "all", "v", ",".join(map(str, vs)), "p", str(year)]
    given = dict(cl)
    for cid in classes:  # every classification in metadata order (SIDRA defaults a missing one to its total)
        cats = given.get(cid)
        parts += [f"c{cid}", "all" if cats is None else ",".join(map(str, cats))]
    return "/".join(parts) + "/f/c/h/y"


def fetch_table(table: int, year: int, meta: dict, cfg: dict, logger) -> tuple:
    classes = [c["id"] for c in meta["classificacoes"]]
    sls = slices(meta, cfg)
    want_header = ["NC", "MC", "V", "D1C", "D2C", "D3C"] + [f"D{4 + i}C" for i in range(len(classes))]

    def one(sl):
        url = slice_url(cfg, table, year, sl, classes)
        data = get_json(url, cfg, logger)
        head, rows = data[0], data[1:]
        if sorted(head) != sorted(want_header):
            raise RuntimeError(f"unexpected header {sorted(head)} for {url}")
        return url, sl, rows

    with ThreadPoolExecutor(cfg["workers"]) as ex:
        results = list(ex.map(one, sls))
    locs, seen, out = None, set(), []
    for url, sl, rows in results:
        here = {r["D1C"] for r in rows}
        if locs is None:
            locs = here
        elif here != locs:
            raise RuntimeError(f"municipalities differ between slices ({len(here)} vs {len(locs)}): {url}")
        n = expected_combos(meta, sl) * len(here)
        if len(rows) != n:
            raise RuntimeError(f"{len(rows)} rows, expected {n}: {url}")
        for r in rows:
            key = tuple(r[f"D{i}C"] for i in [1, 2] + list(range(4, 4 + len(classes))))
            if key in seen:
                raise RuntimeError(f"duplicate key {key}: {url}")
            seen.add(key)
            out.append(r)
    return out, len(locs or ()), len(sls)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--dry-run", action="store_true", help="list the tables and the number of requests, fetch no values")
    ap.add_argument("--overwrite", action="store_true", help="fetch again the tables already downloaded")
    ap.add_argument("--tables", nargs="*", type=int, help="only these tables (overrides the config)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out = Path(cfg["output_folder"])
    logger = setup_logging(Path(cfg["report_folder"]))
    (out / "meta").mkdir(parents=True, exist_ok=True)
    (out / "values").mkdir(parents=True, exist_ok=True)

    survey = cfg["survey"]
    listing = [p for p in get_json(cfg["api_tables"], cfg, logger) if p["id"] == survey]
    if not listing:
        sys.exit(f"Survey {survey} not in {cfg['api_tables']}")
    listing = listing[0]
    tl = out / f"SIDRA_{survey}_TABLES.json"
    write_text = json.dumps(listing, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not tl.is_file() or tl.read_text(encoding="utf-8") != write_text:
        tl.with_name(tl.name + ".part").write_text(write_text, encoding="utf-8")
        tl.with_name(tl.name + ".part").replace(tl)
    logger.info("Survey %s (%s): %d tables", survey, listing["nome"], len(listing["agregados"]))

    only = set(args.tables or cfg.get("tables") or [])
    ids = [int(a["id"]) for a in listing["agregados"] if not only or int(a["id"]) in only]
    if only - set(ids):
        logger.error("Tables not in the survey: %s", sorted(only - set(ids)))

    man_path = out / cfg["manifest_name"]
    manifest = {}
    if man_path.is_file():
        with open(man_path, encoding="utf-8", newline="") as f:
            manifest = {(r["table"], r["year"], r["level"]): r for r in csv.DictReader(f, delimiter=";")}

    failed, todo = [], []
    for t in ids:
        mp = out / "meta" / f"T{t}.json"
        meta = get_json(f"{cfg['api_tables']}/{t}/metadados", cfg, logger)
        per = meta["periodicidade"]
        levels = meta["nivelTerritorial"].get("Administrativo", [])
        years = [y for y in cfg["years"] if per["inicio"] <= y <= per["fim"] and cfg["level"] in levels]
        if not years:
            continue
        todo += [(t, y, meta) for y in years]
        text = json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if not mp.is_file() or mp.read_text(encoding="utf-8") != text:
            if mp.is_file():
                logger.warning("T%d metadata changed on the server", t)
            mp.with_name(mp.name + ".part").write_text(text, encoding="utf-8")
            mp.with_name(mp.name + ".part").replace(mp)
    logger.info("%d table-years to consider (%s)", len(todo), ", ".join(map(str, cfg["years"])))

    for t, y, meta in todo:
        name = f"T{t}_{y}_{cfg['level']}.csv.gz"
        dest = out / "values" / name
        n_req = len(slices(meta, cfg))
        if args.dry_run:
            logger.info("T%d %d: %d requests%s", t, y, n_req, " (exists)" if dest.is_file() else "")
            continue
        if dest.is_file() and not args.overwrite:
            logger.debug("T%d %d: exists, kept", t, y)
            continue
        t0 = time.time()
        try:
            rows, nloc, nreq = fetch_table(t, y, meta, cfg, logger)
        except RuntimeError as exc:
            # a period listed in the metadata may still have no values for this level
            logger.error("T%d %d: %s", t, y, exc)
            failed.append((t, y))
            continue
        if not rows:
            logger.warning("T%d %d: no values", t, y)
            continue
        n_cls = len(meta["classificacoes"])
        cols = ["D1C", "D2C", "D3C"] + [f"D{4 + i}C" for i in range(n_cls)] + ["MC", "V"]
        names = ["municipio_code", "variable", "year"] + [f"c{c['id']}" for c in meta["classificacoes"]] + ["unit", "value"]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(names)
        rows.sort(key=lambda r: tuple(r[c] for c in cols[:-2]))
        w.writerows([r[c] for c in cols] for r in rows)
        sha = write_gzip(dest, buf.getvalue())
        manifest[(str(t), str(y), cfg["level"])] = dict(
            table=t, year=y, level=cfg["level"], file=f"values/{name}", rows=len(rows), municipalities=nloc,
            requests=nreq, sha256=sha, downloaded_at=f"{datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info("T%d %d: %d rows, %d municipalities, %d requests, %.0f s", t, y, len(rows), nloc, nreq, time.time() - t0)
        with open(man_path.with_name(man_path.name + ".part"), "w", encoding="utf-8", newline="") as f:
            mw = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter=";")
            mw.writeheader()
            mw.writerows(manifest[k] for k in sorted(manifest, key=lambda k: (int(k[0]), int(k[1]), k[2])))
        man_path.with_name(man_path.name + ".part").replace(man_path)

    if failed:
        logger.error("%d table-years failed: %s", len(failed), failed)
        sys.exit(1)
    logger.info("Done")


if __name__ == "__main__":
    main()

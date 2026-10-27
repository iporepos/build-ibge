"""
Package a T1 folder for distribution (e.g. a Zenodo deposit): one gzip per data file.

Every file of the T1 folder that matches `compress` (the sector tables, *.csv, and the geography
layers, *.gpkg) is written to the output folder (SRC, next to T1) as `<name>.gz`. All other files
(the LAYERS/FIELDS/VARIABLES catalogs, READMEs...) are copied unchanged so they stay readable on
the repository. Files stay separate on purpose: a new version of the deposit only needs the files
that changed.

  * the gzip is reproducible: no file name and a fixed time stamp in the header, so the same data
    gives the same bytes and the same checksum on every run
  * each .gz is verified by decompressing it and comparing it with the source (verify)
  * incremental: a file whose source is unchanged since the last run (same SHA-256 as recorded in
    the manifest) and whose output exists is skipped; --overwrite rebuilds everything. The log
    tells which files are new, changed, unchanged, so you know what to upload
  * the manifest <prefix>_MANIFEST_X_X.csv lists, for each packaged file: name, size, SHA-256,
    source file, source size, source SHA-256; <prefix>_SHA256SUMS_X_X.txt holds the same SHA-256 in
    the `sha256sum -c` format. Both stay next to the packaged files.
  * bundles (optional): the files matching a bundle's patterns go together into one zip instead of
    one gzip each (e.g. the per-year files of a layer). The zip is reproducible too (members sorted,
    fixed time stamp); it is verified member by member against the sources. Its manifest row has
    source_file "bundle <name template>" (number of members in the log), source_size the sum of the
    members and source_sha256 the SHA-256 of their "sha256  name" lines. {YEARS} in the name is the
    first-last year of the members (4 digits before the extension); when it changes, the row of the
    old zip leaves the manifest, the old zip is not deleted, and the log names it (to replace).

CONFIG (JSON, --config) — required: input_folder, output_folder, report_folder, prefix
  input_folder    the T1 folder (e.g. .../CENSO-2022/V300/T1/ASSETS, .../CENSO-2010/V300/T1/ASSETS)
  output_folder   where the package goes (SRC, same level as T1); created if missing. It may hold
                  other files; only the ones produced here are touched
  report_folder   where the log is written
  prefix          file name prefix of the manifest files, e.g. IBGE_CENSO-2022
  compress        glob patterns of the files to gzip [["*.csv", "*.gpkg"]]
  copy_as_is      glob patterns, checked first, of files to copy unchanged [["*_X_X.*"]]
                  (the catalogs; the LAYERS catalog is X_X by naming convention)
  level           gzip level 1-9 [6]
  verify          decompress and compare each gzip with its source [true]
  workers         files processed in parallel [min(CPUs, 8)]
  bundles         {"<zip name>": [glob patterns]} [{}], e.g.
                  {"IBGE_POP_ESTIMATIVAS-NOTAS_BRASIL_{YEARS}.zip": ["IBGE_POP_ESTIMATIVAS-NOTAS_BRASIL_????.csv"]};
                  checked before copy_as_is / compress; a file may belong to one bundle only

Usage:
    python package_t1.py --config package_t1_2022.local.json [--only basico,setores] [--overwrite]

  --only       comma-separated substrings; only source files whose name contains one of them
               (case-insensitive) are processed; the manifest keeps the entries of the others
  --overwrite  recompress and recopy everything, ignoring the manifest
"""

import argparse
import csv
import fnmatch
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

CHUNK = 8 << 20
REQUIRED_KEYS = ["input_folder", "output_folder", "report_folder", "prefix"]
MANIFEST_FIELDS = ["file", "size", "sha256", "source_file", "source_size", "source_sha256"]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    if Path(cfg["input_folder"]).resolve() == Path(cfg["output_folder"]).resolve():
        sys.exit("output_folder must differ from input_folder")
    cfg.setdefault("compress", ["*.csv", "*.gpkg"])
    cfg.setdefault("copy_as_is", ["*_X_X.*"])
    cfg.setdefault("level", 6)
    cfg.setdefault("verify", True)
    cfg.setdefault("workers", min(os.cpu_count() or 1, 8))
    cfg.setdefault("bundles", {})
    if not 1 <= cfg["level"] <= 9:
        sys.exit("level must be between 1 and 9")
    return cfg


def setup_logging(report_folder: Path) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"package_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    logger = logging.getLogger("package_t1")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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
# Hashing / compression
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> tuple:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


class HashingWriter:
    """File wrapper that hashes what is written to it."""

    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def write(self, data):
        self.h.update(data)
        self.n += len(data)
        return self.f.write(data)

    def flush(self):
        self.f.flush()


def gzip_file(src: Path, dst: Path, level: int) -> dict:
    """Compress src to dst (reproducible header). Returns hashes and sizes of both."""
    src_h, src_n = hashlib.sha256(), 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        out = HashingWriter(fo)
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=level, mtime=0) as gz:
            while chunk := fi.read(CHUNK):
                src_h.update(chunk)
                src_n += len(chunk)
                gz.write(chunk)
    return dict(source_sha256=src_h.hexdigest(), source_size=src_n,
                sha256=out.h.hexdigest(), size=out.n)


def gunzip_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with gzip.open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Per file
# ---------------------------------------------------------------------------

def matches(name: str, patterns: list) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def process_file(src: Path, out_dir: Path, cfg: dict, previous: dict, overwrite: bool) -> dict:
    """Returns the manifest row plus a 'status' of new / changed / unchanged / failed."""
    packed = not matches(src.name, cfg["copy_as_is"]) and matches(src.name, cfg["compress"])
    out_name = src.name + ".gz" if packed else src.name
    dst = out_dir / out_name
    prev = previous.get(out_name)
    row = dict(file=out_name, source_file=src.name)

    src_sha, src_size = sha256_file(src)
    if not overwrite and prev and dst.exists() and prev["source_sha256"] == src_sha \
            and int(prev["size"]) == dst.stat().st_size:
        return dict(prev, status="unchanged")

    part = dst.with_name(dst.name + ".part")
    try:
        if packed:
            info = gzip_file(src, part, cfg["level"])
            if info["source_sha256"] != src_sha:
                raise RuntimeError("source changed while it was being compressed")
            if cfg["verify"] and gunzip_sha256(part) != src_sha:
                raise RuntimeError("verification failed: gzip does not decompress to the source")
            row.update(size=info["size"], sha256=info["sha256"])
        else:
            shutil.copyfile(src, part)
            sha, size = sha256_file(part)
            if sha != src_sha:
                raise RuntimeError("copy differs from the source")
            row.update(size=size, sha256=sha)
        row.update(source_size=src_size, source_sha256=src_sha)
        if dst.exists():
            dst.unlink()
        part.rename(dst)
    except Exception as e:  # report and go on with the other files
        if part.exists():
            part.unlink()
        return dict(row, status="failed", error=str(e))
    row["status"] = "changed" if prev else "new"
    return row


ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def bundle_name(template: str, members: list) -> str:
    years = sorted({m.group(1) for p in members if (m := re.search(r"(\d{4})\.[^.]+$", p.name))})
    span = (years[0] if len(years) == 1 else f"{years[0]}-{years[-1]}") if years else "X"
    return template.replace("{YEARS}", span)


def process_bundle(template: str, members: list, out_dir: Path, cfg: dict, previous: dict, overwrite: bool) -> dict:
    """One reproducible zip of members (sorted). Returns the manifest row plus 'status' and 'members'."""
    out_name = bundle_name(template, members)
    dst = out_dir / out_name
    prev = previous.get(out_name)
    row = dict(file=out_name, source_file=f"bundle {template}", members=len(members))
    hashes = {p.name: sha256_file(p) for p in members}
    src_sha = hashlib.sha256("".join(f"{hashes[n][0]}  {n}\n" for n in sorted(hashes)).encode()).hexdigest()
    src_size = sum(n for _, n in hashes.values())
    if not overwrite and prev and dst.exists() and prev["source_sha256"] == src_sha \
            and int(prev["size"]) == dst.stat().st_size:
        return dict(prev, status="unchanged", members=len(members))

    part = dst.with_name(dst.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=cfg["level"]) as z:
            for p in sorted(members, key=lambda x: x.name):
                info = zipfile.ZipInfo(p.name, date_time=ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(p, "rb") as fi, z.open(info, "w") as fo:
                    while chunk := fi.read(CHUNK):
                        fo.write(chunk)
        if cfg["verify"]:
            with zipfile.ZipFile(part) as z:
                if sorted(z.namelist()) != sorted(hashes):
                    raise RuntimeError("verification failed: zip members differ from the sources")
                for n in z.namelist():
                    h = hashlib.sha256()
                    with z.open(n) as f:
                        while chunk := f.read(CHUNK):
                            h.update(chunk)
                    if h.hexdigest() != hashes[n][0]:
                        raise RuntimeError(f"verification failed: {n} in the zip differs from the source")
        if {p.name: sha256_file(p)[0] for p in members} != {n: h for n, (h, _) in hashes.items()}:
            raise RuntimeError("a source changed while it was being zipped")
        sha, size = sha256_file(part)
        row.update(size=size, sha256=sha, source_size=src_size, source_sha256=src_sha)
        if dst.exists():
            dst.unlink()
        part.rename(dst)
    except Exception as e:  # report and go on with the other files
        if part.exists():
            part.unlink()
        return dict(row, status="failed", error=str(e))
    row["status"] = "changed" if prev else "new"
    return row


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def read_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {r["file"]: r for r in csv.DictReader(f, delimiter=";")}


def write_manifest(out_dir: Path, prefix: str, rows: dict):
    ordered = [rows[k] for k in sorted(rows)]
    mpath = out_dir / f"{prefix}_MANIFEST_X_X.csv"
    with open(mpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, delimiter=";", lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)
    spath = out_dir / f"{prefix}_SHA256SUMS_X_X.txt"
    with open(spath, "w", encoding="utf-8", newline="") as f:
        for r in ordered:
            f.write(f"{r['sha256']}  {r['file']}\n")
    return mpath, spath


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--only", help="comma-separated substrings of the source files to process")
    ap.add_argument("--overwrite", action="store_true", help="rebuild everything, ignoring the manifest")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))

    in_dir, out_dir = Path(cfg["input_folder"]), Path(cfg["output_folder"])
    if not in_dir.is_dir():
        sys.exit(f"input_folder not found: {in_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    skip = {f"{cfg['prefix']}_MANIFEST_X_X.csv", f"{cfg['prefix']}_SHA256SUMS_X_X.txt"}
    sources = sorted(p for p in in_dir.iterdir()
                     if p.is_file() and not p.name.endswith(".part") and p.name not in skip)
    for d in sorted(p for p in in_dir.iterdir() if p.is_dir()):
        logger.warning("subfolder %s not packaged", d.name)
    bundles = {t: [] for t in cfg["bundles"]}
    for p in sources:
        hit = [t for t, pats in cfg["bundles"].items() if matches(p.name, pats)]
        if len(hit) > 1:
            sys.exit(f"{p.name} matches more than one bundle: {hit}")
        if hit:
            bundles[hit[0]].append(p)
    for t in [t for t, m in bundles.items() if not m]:
        logger.warning("bundle %s: no matching file", t)
        del bundles[t]
    in_bundle = {p for m in bundles.values() for p in m}
    sources = [p for p in sources if p not in in_bundle]
    if args.only:
        subs = [s.lower() for s in args.only.split(",")]
        sources = [p for p in sources if any(s in p.name.lower() for s in subs)]
        bundles = {t: m for t, m in bundles.items() if any(s in p.name.lower() for p in m for s in subs)}
    if not sources and not bundles:
        sys.exit("no files to process")

    manifest_path = out_dir / f"{cfg['prefix']}_MANIFEST_X_X.csv"
    previous = read_manifest(manifest_path)
    logger.info("=" * 60)
    logger.info("Packaging %s (%d files, %d bundles of %d files, level %d, %d workers)", in_dir, len(sources),
                len(bundles), len(in_bundle), cfg["level"], cfg["workers"])
    logger.info("Output %s", out_dir)
    logger.info("=" * 60)

    results = []
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = [(p.name, pool.submit(process_file, p, out_dir, cfg, previous, args.overwrite))
                   for p in sources]
        futures += [(f"{bundle_name(t, m)} ({len(m)} files)",
                     pool.submit(process_bundle, t, m, out_dir, cfg, previous, args.overwrite))
                    for t, m in bundles.items()]
        for label, fut in futures:
            r = fut.result()
            results.append(r)
            if r["status"] == "failed":
                logger.error("%-58s FAILED: %s", label, r["error"])
            else:
                ratio = f"{int(r['size']) / max(int(r['source_size']), 1):5.0%}"
                logger.info("%-58s %-9s %9.1f MB -> %9.1f MB (%s)", label, r["status"],
                            int(r["source_size"]) / 1e6, int(r["size"]) / 1e6, ratio)

    rows = dict(previous)
    for r in results:
        if r["status"] != "failed":
            rows[r["file"]] = {k: r[k] for k in MANIFEST_FIELDS}
    # a bundle whose name changed ({YEARS}): its old zip leaves the manifest
    for r in results:
        if r["source_file"].startswith("bundle ") and r["status"] != "failed":
            for old in [k for k, v in rows.items() if v["source_file"] == r["source_file"] and k != r["file"]]:
                del rows[old]
                logger.warning("%s replaces %s (old zip left in the output folder; remove it from the deposit)",
                               r["file"], old)
    mpath, spath = write_manifest(out_dir, cfg["prefix"], rows)

    by = {s: [r["file"] for r in results if r["status"] == s]
          for s in ("new", "changed", "unchanged", "failed")}
    logger.info("=" * 60)
    logger.info("new %d, changed %d, unchanged %d, failed %d",
                *(len(by[s]) for s in ("new", "changed", "unchanged", "failed")))
    todo = by["new"] + by["changed"]
    if todo:
        logger.info("To upload: %s (plus the manifest and checksum files)", ", ".join(todo))
    logger.info("Manifest: %s", mpath)
    logger.info("Checksums: %s", spath)
    logger.info("=" * 60)
    if by["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

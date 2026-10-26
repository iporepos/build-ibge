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

CONFIG (JSON, --config) — required: input_folder, output_folder, report_folder, prefix
  input_folder    the T1 folder (e.g. .../CENSO-2022/V200/T1/ASSETS, .../CENSO-2010/V200/T1/ASSETS)
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
import shutil
import sys
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
    if args.only:
        subs = [s.lower() for s in args.only.split(",")]
        sources = [p for p in sources if any(s in p.name.lower() for s in subs)]
    if not sources:
        sys.exit("no files to process")

    manifest_path = out_dir / f"{cfg['prefix']}_MANIFEST_X_X.csv"
    previous = read_manifest(manifest_path)
    logger.info("=" * 60)
    logger.info("Packaging %s (%d files, level %d, %d workers)", in_dir, len(sources),
                cfg["level"], cfg["workers"])
    logger.info("Output %s", out_dir)
    logger.info("=" * 60)

    results = []
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = [(p, pool.submit(process_file, p, out_dir, cfg, previous, args.overwrite))
                   for p in sources]
        for p, fut in futures:
            r = fut.result()
            results.append(r)
            if r["status"] == "failed":
                logger.error("%-58s FAILED: %s", p.name, r["error"])
            else:
                ratio = f"{int(r['size']) / max(int(r['source_size']), 1):5.0%}"
                logger.info("%-58s %-9s %9.1f MB -> %9.1f MB (%s)", p.name, r["status"],
                            int(r["source_size"]) / 1e6, int(r["size"]) / 1e6, ratio)

    rows = dict(previous)
    for r in results:
        if r["status"] != "failed":
            rows[r["file"]] = {k: r[k] for k in MANIFEST_FIELDS}
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

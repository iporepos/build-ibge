"""
Download the IBGE Census 2010 sector aggregates (universo + sinopse) from ftp.ibge.gov.br into SRC.

Every file is written to <name>.part and renamed only after its size (vs Content-Length) and, for zips,
the CRC of every member are verified. An existing file is never overwritten: if the server copy differs in
size it is reported and left alone (pass --overwrite to replace it). The server listing of every product
folder is compared with the config, so a new IBGE release (new file names) is reported instead of silently
missed. A manifest (url, size, sha256, Last-Modified, download time) is kept in the output folder.

Usage:
    python download_2010.py --config download_2010.local.json [--dry-run] [--overwrite] [--only universo]
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (populated from --config by load_config)
# ---------------------------------------------------------------------------
OUTPUT_FOLDER: Path
LOG_FOLDER: Path
PRODUCTS: list
MANIFEST_NAME: str
CHUNK_SIZE: int
TIMEOUT: int
RETRIES: int

REQUIRED_KEYS = ["output_folder", "log_folder", "products"]
MANIFEST_COLUMNS = ["product", "file", "url", "size", "sha256", "last_modified", "downloaded_at"]


def load_config(path: str) -> None:
    """Load the JSON config and set module-level settings."""
    global OUTPUT_FOLDER, LOG_FOLDER, PRODUCTS, MANIFEST_NAME, CHUNK_SIZE, TIMEOUT, RETRIES
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    OUTPUT_FOLDER = Path(cfg["output_folder"])
    LOG_FOLDER = Path(cfg["log_folder"])
    PRODUCTS = cfg["products"]
    for p in PRODUCTS:
        miss = [k for k in ("name", "base_url", "folder") if k not in p]
        if miss:
            sys.exit(f"Product {p.get('name', '?')} is missing keys: {miss}")
    MANIFEST_NAME = cfg.get("manifest_name", "download_manifest.csv")
    CHUNK_SIZE = int(cfg.get("chunk_size", 1 << 20))
    TIMEOUT = int(cfg.get("timeout", 120))  # seconds per request
    RETRIES = int(cfg.get("retries", 3))


def product_files(product: dict) -> list:
    """Explicit 'files' plus 'template' expanded over 'items' (placeholders {item} and {release})."""
    files = list(product.get("files", []))
    template = product.get("template")
    if template:
        fields = {k: v for k, v in product.items() if isinstance(v, str)}
        files += [template.format(**{**fields, "item": item}) for item in product.get("items", [])]
    return files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Create a logger that writes to both console and a timestamped log file."""
    LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    log_path = LOG_FOLDER / f"download_log_{datetime.now():%Y%m%d_%H%M%S}.txt"

    logger = logging.getLogger("download_2010")
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


def file_url(base_url: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/{urllib.parse.quote(name)}"


def request(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 (bench downloader)"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def with_retries(fn, what: str, logger: logging.Logger):
    """Run fn() up to RETRIES times; HTTP 4xx is not retried. Returns fn() or raises the last error."""
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise
            err = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            err = exc
        logger.warning("  %s failed (attempt %d/%d): %s", what, attempt, RETRIES, err)
        if attempt < RETRIES:
            time.sleep(5 * attempt)
    raise err


def remote_listing(base_url: str, logger: logging.Logger) -> set:
    """File names in an Apache directory listing (sub-folders and sort links left out)."""
    def fetch():
        with request(base_url.rstrip("/") + "/") as r:
            return r.read().decode("latin-1")
    html = with_retries(fetch, f"listing {base_url}", logger)
    names = set()
    for href in re.findall(r'href="([^"]+)"', html):
        if href.startswith(("?", "/", "http", "#", "mailto:")) or href.endswith("/"):
            continue
        names.add(urllib.parse.unquote(href))
    return names


def remote_info(url: str, logger: logging.Logger) -> tuple:
    """(size or None, Last-Modified or '') from a HEAD request."""
    def head():
        with request(url, "HEAD") as r:
            size = r.headers.get("Content-Length")
            return (int(size) if size else None), r.headers.get("Last-Modified", "")
    return with_retries(head, f"HEAD {url}", logger)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path, expected_size) -> str:
    """'' if the file is good, else the reason."""
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        return f"size {size} != server {expected_size}"
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
            if bad:
                return f"zip CRC error in member {bad!r}"
        except zipfile.BadZipFile as exc:
            return f"bad zip: {exc}"
    return ""


def download(url: str, dest: Path, logger: logging.Logger) -> tuple:
    """Download url to dest via dest.part. Returns (Last-Modified, Content-Length or None)."""
    part = dest.with_name(dest.name + ".part")

    def fetch():
        with request(url) as r, open(part, "wb") as f:
            total = r.headers.get("Content-Length")
            total = int(total) if total else None
            done = 0
            while chunk := r.read(CHUNK_SIZE):
                f.write(chunk)
                done += len(chunk)
                if total and sys.stdout.isatty():
                    print(f"\r    {done / 2**20:8.1f} / {total / 2**20:.1f} MB", end="", flush=True)
            if total and sys.stdout.isatty():
                print()
            return r.headers.get("Last-Modified", ""), total

    try:
        last_modified, total = with_retries(fetch, f"GET {url}", logger)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    reason = verify(part, total)
    if reason:
        part.unlink(missing_ok=True)
        raise IOError(f"verification failed: {reason}")
    os.replace(part, dest)
    return last_modified, total


def read_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["product"], r["file"]): r for r in csv.DictReader(f)}


def write_manifest(path: Path, rows: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow(rows[key])
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--dry-run", action="store_true", help="check listings and sizes only, download nothing")
    parser.add_argument("--overwrite", action="store_true", help="replace local files that differ from the server")
    parser.add_argument("--only", nargs="+", metavar="PRODUCT", help="restrict to these product names")
    args = parser.parse_args()
    load_config(args.config)

    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("IBGE Censo 2010 sector aggregates — downloader%s", " (DRY RUN)" if args.dry_run else "")
    logger.info("Output folder: %s", OUTPUT_FOLDER)
    logger.info("=" * 60)

    manifest_path = OUTPUT_FOLDER / MANIFEST_NAME
    manifest = read_manifest(manifest_path)
    counts = {"downloaded": 0, "present": 0, "would download": 0, "differs": 0, "failed": 0}
    problems = []

    products = [p for p in PRODUCTS if not args.only or p["name"] in args.only]
    if args.only and len(products) != len(args.only):
        sys.exit(f"Unknown product in --only {args.only}; known: {[p['name'] for p in PRODUCTS]}")

    for product in products:
        name, base_url = product["name"], product["base_url"]
        folder = OUTPUT_FOLDER / product["folder"]
        files = product_files(product)
        logger.info("Product %s: %d files -> %s", name, len(files), folder)

        try:
            listing = remote_listing(base_url, logger)
        except Exception as exc:  # the listing is a check only; downloads can still go on
            logger.warning("  could not read the server listing: %s", exc)
            listing = None
        if listing is not None:
            not_on_server = sorted(set(files) - listing)
            not_in_config = sorted(listing - set(files))
            for f in not_on_server:
                logger.error("  in config but NOT on the server: %s", f)
                problems.append(f"{name}: not on server: {f}")
            for f in not_in_config:
                logger.warning("  on the server but not in config: %s", f)
                problems.append(f"{name}: not in config: {f}")

        if not args.dry_run:
            folder.mkdir(parents=True, exist_ok=True)

        for i, fname in enumerate(files, 1):
            url = file_url(base_url, fname)
            dest = folder / fname
            tag = f"[{name} {i}/{len(files)}] {fname}"
            try:
                size, last_modified = remote_info(url, logger)
                if dest.exists():
                    local = dest.stat().st_size
                    if size is not None and local != size:
                        counts["differs"] += 1
                        if not args.overwrite or args.dry_run:
                            logger.warning("%s  DIFFERS (local %d, server %d) — left alone%s", tag, local, size,
                                           "" if args.dry_run else ", use --overwrite to replace")
                            problems.append(f"{name}: differs from server: {fname}")
                            continue
                        logger.info("%s  differs from server, replacing (--overwrite)", tag)
                    else:
                        counts["present"] += 1
                        row = manifest.get((name, fname))
                        if not args.dry_run and (row is None or int(row["size"]) != local):
                            manifest[(name, fname)] = {
                                "product": name, "file": fname, "url": url, "size": local,
                                "sha256": sha256(dest), "last_modified": last_modified, "downloaded_at": "",
                            }
                        logger.info("%s  present (%.1f MB)", tag, local / 2**20)
                        continue
                if args.dry_run:
                    counts["would download"] += 1
                    logger.info("%s  would download (%s MB, %s)", tag,
                                f"{size / 2**20:.1f}" if size is not None else "?", last_modified)
                    continue
                logger.info("%s  downloading (%s MB)", tag, f"{size / 2**20:.1f}" if size is not None else "?")
                last_modified, total = download(url, dest, logger)
                manifest[(name, fname)] = {
                    "product": name, "file": fname, "url": url, "size": dest.stat().st_size,
                    "sha256": sha256(dest), "last_modified": last_modified,
                    "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                }
                write_manifest(manifest_path, manifest)  # after every file, so an interrupted run keeps its rows
                counts["downloaded"] += 1
                logger.info("%s  OK (verified)", tag)
            except Exception as exc:
                counts["failed"] += 1
                logger.error("%s  FAILED: %s", tag, exc)
                problems.append(f"{name}: failed: {fname}: {exc}")

    if not args.dry_run:
        write_manifest(manifest_path, manifest)
        logger.info("Manifest: %s (%d rows)", manifest_path, len(manifest))

    logger.info("=" * 60)
    logger.info("DONE — %s", ", ".join(f"{k} {v}" for k, v in counts.items()))
    if problems:
        logger.info("Problems (%d):", len(problems))
        for p in problems:
            logger.info("  • %s", p)
    logger.info("=" * 60)
    sys.exit(1 if counts["failed"] else 0)


if __name__ == "__main__":
    main()

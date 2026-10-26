"""
Download IBGE Census 2010 Setores Censitários shapefiles (distritos & subdistritos)
for all Brazilian states from geoftp.ibge.gov.br.

Usage:
    python download_malhas.py --config download_malhas.local.json
"""

import argparse
import json
import logging
import sys
import requests
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (populated from --config by load_config)
# ---------------------------------------------------------------------------
OUTPUT_FOLDER: Path
BASE_URL: str
UFS: list
TARGETS: list
CHUNK_SIZE: int
TIMEOUT: int

REQUIRED_KEYS = ["output_folder", "base_url", "ufs", "targets"]


def load_config(path: str) -> None:
    """Load the JSON config and set module-level settings."""
    global OUTPUT_FOLDER, BASE_URL, UFS, TARGETS, CHUNK_SIZE, TIMEOUT
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    OUTPUT_FOLDER = Path(cfg["output_folder"])
    BASE_URL = cfg["base_url"].rstrip("/")
    UFS = [u.lower() for u in cfg["ufs"]]
    TARGETS = cfg["targets"]
    CHUNK_SIZE = int(cfg.get("chunk_size", 8192))
    TIMEOUT = int(cfg.get("timeout", 120))  # seconds per request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Create a logger that writes to both console and a timestamped log file."""
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_FOLDER / f"download_log_{timestamp}.txt"

    logger = logging.getLogger("ibge_downloader")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

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


def download_file(url: str, dest: Path, logger: logging.Logger) -> bool:
    """
    Download a single file with a progress bar printed to the console.
    Returns True on success, False on failure.
    """
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0

            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        bar_len = 30
                        filled = int(bar_len * downloaded / total)
                        bar = "█" * filled + "░" * (bar_len - filled)
                        print(
                            f"\r  [{bar}] {pct:5.1f}%  "
                            f"({downloaded / 1024:.0f} / {total / 1024:.0f} KB)",
                            end="", flush=True,
                        )
            print()  # newline after progress bar
            logger.info("OK  %s  (%d KB)", dest.name, downloaded // 1024)
            return True

    except requests.exceptions.HTTPError as exc:
        logger.warning("HTTP error for %s: %s", url, exc)
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Connection error for %s: %s", url, exc)
    except requests.exceptions.Timeout:
        logger.warning("Timeout for %s", url)
    except requests.exceptions.RequestException as exc:
        logger.warning("Request error for %s: %s", url, exc)
    except OSError as exc:
        logger.error("File I/O error writing %s: %s", dest, exc)

    # Clean up partial file
    if dest.exists():
        dest.unlink()
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    load_config(parser.parse_args().config)

    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("IBGE Setores Censitários 2010 — Bulk Downloader")
    logger.info("Output folder: %s", OUTPUT_FOLDER)
    logger.info("=" * 60)

    total_files = len(UFS) * len(TARGETS)
    success_count = 0
    fail_count = 0
    failures = []

    for i_uf, uf in enumerate(UFS, 1):
        uf_dir = OUTPUT_FOLDER / uf
        uf_dir.mkdir(parents=True, exist_ok=True)

        for target_template in TARGETS:
            filename = target_template.format(uf=uf)
            url = f"{BASE_URL}/{uf}/{filename}"
            dest = uf_dir / filename

            file_index = (i_uf - 1) * len(TARGETS) + TARGETS.index(target_template) + 1
            logger.info(
                "[%d/%d]  UF=%s  file=%s", file_index, total_files, uf.upper(), filename
            )

            if dest.exists():
                logger.info("SKIP (already exists)  %s", dest)
                success_count += 1
                continue

            ok = download_file(url, dest, logger)
            if ok:
                success_count += 1
            else:
                fail_count += 1
                failures.append(url)

    # Summary
    logger.info("=" * 60)
    logger.info("DONE — %d succeeded, %d failed out of %d", success_count, fail_count, total_files)
    if failures:
        logger.info("Failed URLs:")
        for f in failures:
            logger.info("  • %s", f)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
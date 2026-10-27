"""
Move dataset version folders from the work area (the repo's data/) to the warehouse, e.g.
data/PNSB-2017/V1 -> <IBGE root>/PNSB-2017/V100. A move, not a copy: the source is deleted, but only after
its copy is complete and verified.

For each move: the source must exist and the target must not; the tree is copied to <target>.part, every
file of the copy is compared with the source (same list of files, same sizes, same SHA-256), the copy is
renamed to the target, the tier folders of the warehouse template (ensure_folders) are created when missing,
and the source is deleted. A failed check stops before anything is deleted (the .part copy is left for
inspection). Nothing is done without --execute (dry run: lists the moves, files and sizes).

CONFIG (JSON, --config)
  required:
    moves           [{"from": folder, "to": folder}]; in-repo paths relative to the repo root
    report_folder   log folder
  optional:
    ensure_folders  folders created in each target if missing [["SRC", "T0/ASSETS", "T1/ASSETS", "T2/ASSETS"]]
    remove_empty_parent  delete the source's parent folder when the move leaves it empty [true]

Usage (from the repo root):
    python move_datasets.py --config move_datasets.local.json            # dry run
    python move_datasets.py --config move_datasets.local.json --execute
"""

import argparse
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["moves", "report_folder"]
CHUNK = 8 << 20


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("ensure_folders", ["SRC", "T0/ASSETS", "T1/ASSETS", "T2/ASSETS"])
    cfg.setdefault("remove_empty_parent", True)
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"move_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("move_datasets")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def files_of(root: Path) -> dict:
    """{relative path: size} of every file under root."""
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--execute", action="store_true", help="do the moves (default: dry run)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(Path(cfg["report_folder"]))

    plan, errors = [], []
    for mv in cfg["moves"]:
        src, dst = Path(mv["from"]), Path(mv["to"])
        part = dst.with_name(dst.name + ".part")
        if not src.is_dir():
            errors.append(f"source missing: {src}")
        elif dst.exists():
            errors.append(f"target exists: {dst}")
        elif part.exists():
            errors.append(f"left-over copy exists (inspect and delete it): {part}")
        else:
            files = files_of(src)
            plan.append((src, dst, part, files))
            logger.info("%s -> %s: %d files, %.1f MB", src, dst, len(files), sum(files.values()) / 1e6)
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)
    if not args.execute:
        logger.info("Dry run: nothing moved (use --execute)")
        return

    for src, dst, part, files in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Copying %s -> %s", src, part)
        shutil.copytree(src, part, copy_function=shutil.copy2)
        copied = files_of(part)
        if copied != files:
            logger.error("%s: the copy differs from the source in files or sizes; source kept, copy left at %s", src, part)
            sys.exit(1)
        bad = [f for f in files if sha256(src / f) != sha256(part / f)]
        if bad:
            logger.error("%s: SHA-256 differs for %s; source kept, copy left at %s", src, bad[:5], part)
            sys.exit(1)
        part.rename(dst)
        for f in cfg["ensure_folders"]:
            (dst / f).mkdir(parents=True, exist_ok=True)
        shutil.rmtree(src)
        if cfg["remove_empty_parent"] and not any(src.parent.iterdir()):
            src.parent.rmdir()
        logger.info("Moved %s -> %s (%d files verified)", src, dst, len(files))
    logger.info("Done")


if __name__ == "__main__":
    main()

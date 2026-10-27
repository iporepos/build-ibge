"""
Run the build pipelines of the repository in order, from the repo root.

Each pipeline (dtb, pop, censo_2022, ...) is a list of steps in the config:
  run   a script of the repo, run from its own folder (in-repo config paths are relative to it) with
        --config <config>.<config_suffix>.json, with the Python that runs this script (the activated venv)
  copy  a file copied into a folder (hand-kept catalogs into T1, a census T1 file into a SRC): skipped when
        the target is already identical, else written as .part and renamed
The suite stops at the first failing step (non-zero exit of the script, missing file) and exits non-zero.

Checks: every script and config of the selected steps exists before anything runs; every copy source exists
when its step comes (a source may be the output of an earlier step).

Outputs (report_folder): suite_log_<timestamp>.txt (the output of every step) and suite_steps_<timestamp>.csv
(pipeline, step, status, seconds).

CONFIG (JSON, --config)
  required:
    report_folder    folder for the log and the step report (relative to the repo root)
    config_suffix    "local" or "example": the step configs are <config>.<suffix>.json
    pipelines        {name: [step, ...]} in run order; a step is
                       {"run": "<folder>/<script>.py"}
                         optional "config" (default the script name, e.g. "build_database_2010"),
                         "args" (extra arguments, default []), "overwrite" (default true: gets --overwrite
                         when the suite runs with --overwrite and the script has that option; false for
                         package_t1, whose --overwrite repacks everything), "optional" (default false:
                         runs only with --with-optional, e.g. downloads)
                       {"copy": "<file>", "to": "<folder>"}, paths absolute or relative to the repo root,
                         optional "optional"
  optional:
    default_pipelines  pipelines run when none is named (default: none, the names must be given)

Usage (from the repo root, venv activated):
    python run_suite.py --config run_suite.local.json --list
    python run_suite.py --config run_suite.local.json dtb pop --dry-run
    python run_suite.py --config run_suite.local.json censo_2010 --overwrite --from harmonize_2010
    python run_suite.py --config run_suite.local.json censo_2010 censos_checks --overwrite --from censo_2010:4
    python run_suite.py --config run_suite.local.json all --overwrite
"""

import argparse
import csv
import filecmp
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["report_folder", "config_suffix", "pipelines"]
ROOT = Path(__file__).resolve().parent


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    return cfg


def setup_logging(report_folder: Path, stamp: str) -> logging.Logger:
    report_folder.mkdir(parents=True, exist_ok=True)
    log_path = report_folder / f"suite_log_{stamp}.txt"
    logger = logging.getLogger("run_suite")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


def repo_path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def label(step: dict) -> str:
    if "run" in step:
        stem = Path(step["run"]).stem
        conf = step.get("config", stem)
        return step["run"] + ("" if conf == stem else f" [{conf}]")
    return f"copy {Path(step['copy']).name} -> {step['to']}"


def command(step: dict, suffix: str, overwrite: bool) -> tuple:
    script = repo_path(step["run"])
    conf = f"{step.get('config', script.stem)}.{suffix}.json"
    cmd = [sys.executable, script.name, "--config", conf] + list(step.get("args", []))
    if overwrite and step.get("overwrite", True) and '"--overwrite"' in script.read_text(encoding="utf-8"):
        cmd.append("--overwrite")
    return script.parent, cmd


def run_step(step: dict, suffix: str, overwrite: bool, logger) -> int:
    folder, cmd = command(step, suffix, overwrite)
    logger.info("  $ cd %s && %s", folder.relative_to(ROOT), " ".join(["python"] + cmd[1:]))
    proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        logger.info("  | %s", line.rstrip("\n"))
    return proc.wait()


def copy_step(step: dict, logger) -> int:
    src, folder = repo_path(step["copy"]), repo_path(step["to"])
    if not src.is_file():
        logger.error("  copy source missing: %s", src)
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
        logger.info("  %s unchanged", dst)
        return 0
    part = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, part)
    part.replace(dst)
    logger.info("  copied %s -> %s", src, dst)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("pipelines", nargs="*", help="pipelines to run, in config order ('all' = every pipeline)")
    ap.add_argument("--list", action="store_true", help="list the pipelines and their steps, run nothing")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--overwrite", action="store_true", help="pass --overwrite to the scripts that have it")
    ap.add_argument("--from", dest="start", metavar="STEP",
                    help="start at <pipeline>:<n> (n as in --list) or at the first step whose label contains STEP")
    ap.add_argument("--with-optional", action="store_true", help="also run the optional steps (downloads)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pipes = cfg["pipelines"]
    if args.list:
        for name, steps in pipes.items():
            print(name)
            for i, s in enumerate(steps, 1):
                print(f"  {i:2d}. {label(s)}" + ("  (optional)" if s.get("optional") else ""))
        return
    names = args.pipelines or cfg.get("default_pipelines", [])
    if not names:
        sys.exit(f"Name the pipelines to run (or 'all'): {', '.join(pipes)}")
    if "all" in names:
        names = list(pipes)
    unknown = [n for n in names if n not in pipes]
    if unknown:
        sys.exit(f"Unknown pipelines {unknown}; known: {', '.join(pipes)}")
    names = [n for n in pipes if n in names]

    plan = [(n, i, s) for n in names for i, s in enumerate(pipes[n], 1) if args.with_optional or not s.get("optional")]
    if args.start:
        hit = [k for k, (n, i, s) in enumerate(plan) if args.start == f"{n}:{i}" or args.start in label(s)]
        if not hit:
            sys.exit(f"--from {args.start}: no step of {names} matches")
        plan = plan[hit[0]:]

    missing = []
    for _, _, s in plan:
        if "run" in s:
            script = repo_path(s["run"])
            conf = script.parent / f"{s.get('config', script.stem)}.{cfg['config_suffix']}.json"
            missing += [str(p) for p in (script, conf) if not p.is_file()]
    if missing:
        sys.exit("Missing scripts / configs:\n  " + "\n  ".join(missing))

    if args.dry_run:
        for n, i, s in plan:
            if "run" in s:
                folder, cmd = command(s, cfg["config_suffix"], args.overwrite)
                print(f"[{n}:{i}] cd {folder.relative_to(ROOT)} && python {' '.join(cmd[1:])}")
            else:
                print(f"[{n}:{i}] cp {s['copy']} {s['to']}/")
        return

    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    report = repo_path(cfg["report_folder"])
    logger = setup_logging(report, stamp)
    logger.info("Python %s; pipelines %s; %d steps%s", sys.executable, names, len(plan), "; --overwrite" if args.overwrite else "")
    if sys.prefix == sys.base_prefix:
        logger.warning("Not running in a virtual environment: activate .venv (the scripts need requirements.txt)")
    rows, failed = [], None
    for k, (n, i, s) in enumerate(plan, 1):
        logger.info("[%d/%d] %s:%d %s", k, len(plan), n, i, label(s))
        t0 = time.time()
        rc = run_step(s, cfg["config_suffix"], args.overwrite, logger) if "run" in s else copy_step(s, logger)
        rows.append(dict(pipeline=n, step=label(s), status="ok" if rc == 0 else f"failed ({rc})", seconds=round(time.time() - t0, 1)))
        if rc != 0:
            failed = (n, i, s)
            break
    rows += [dict(pipeline=n, step=label(s), status="not run", seconds="") for n, _, s in plan[len(rows):]]
    with open(report / f"suite_steps_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pipeline", "step", "status", "seconds"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
    if failed:
        logger.error("FAILED at %s:%d %s (resume with --from %s:%d)", failed[0], failed[1], label(failed[2]), failed[0], failed[1])
        sys.exit(1)
    logger.info("All %d steps ok", len(plan))


if __name__ == "__main__":
    main()

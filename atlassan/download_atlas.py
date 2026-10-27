"""
Harvest the layers of the Atlas de Saneamento 2021 from the IBGE GeoServer (workspace CGEO) into SRC,
without geometry: the attributes of every layer (WFS GetFeature, CSV), its schema (DescribeFeatureType),
its legend with the class labels (WMS GetLegendGraphic, JSON) and its ISO 19115 record (the MetadataURL of
the WMS capabilities, INDE catalogue). The capabilities documents are saved too.

The server answers some requests with an ExceptionReport ("details omitted") at random: such answers are
retried. Existing files are kept unless --overwrite.

Checks: the attribute CSV of every layer has as many rows as the server count (resultType=hits) and the
columns of its schema; every layer of the capabilities with the prefix is harvested (the prefix is matched
ignoring case: chapters 2 and 3 are published as atlassan2021_*, the others as AtlasSan2021_*). Layers whose legend has
no JSON rendering (composite styles) are logged, not failed. A failed layer is logged and the script exits
non-zero after trying the others.

Outputs (output_folder): capabilities/{wfs,wms}.xml, schema/<layer>.xml, attributes/<layer>.csv,
legend/<layer>.json, iso/<layer>.xml, and the manifest (layer, title, rows, columns, geometry, metadata id,
sha256 of the attributes). Log in report_folder.

CONFIG (JSON, --config)
  required:
    output_folder   SRC folder of the harvest (e.g. <IBGE root>/ATLASSAN/V1/SRC/GEOSERVER)
    report_folder   log folder
    prefix          layer name prefix, any case ("AtlasSan2021_")
  optional:
    wfs_url         default "https://geoservicos.ibge.gov.br/geoserverCGEO/CGEO/wfs"
    wms_url         default "https://geoservicos.ibge.gov.br/geoserverCGEO/CGEO/ows"
    workspace       default "CGEO"
    layers          only these layers (default all with the prefix)
    workers         parallel layers (default 4)
    timeout         seconds per request (default 300)
    retries         attempts per request (default 6)
    manifest_name   default "GEOSERVER_MANIFEST.csv"

Usage:
    python download_atlas.py --config download_atlas.local.json [--overwrite] [--layers AtlasSan2021_010201]
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

REQUIRED_KEYS = ["output_folder", "report_folder", "prefix"]
MANIFEST_COLUMNS = ["layer", "title", "rows", "columns", "geometry", "metadata_id", "legend", "sha256", "downloaded_at"]
NS = {"xsd": "http://www.w3.org/2001/XMLSchema", "wms": "http://www.opengis.net/wms",
      "xlink": "http://www.w3.org/1999/xlink", "wfs": "http://www.opengis.net/wfs/2.0"}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"Config {path} is missing required keys: {missing}")
    cfg.setdefault("wfs_url", "https://geoservicos.ibge.gov.br/geoserverCGEO/CGEO/wfs")
    cfg.setdefault("wms_url", "https://geoservicos.ibge.gov.br/geoserverCGEO/CGEO/ows")
    cfg.setdefault("workspace", "CGEO")
    cfg.setdefault("workers", 4)
    cfg.setdefault("timeout", 300)
    cfg.setdefault("retries", 6)
    cfg.setdefault("manifest_name", "GEOSERVER_MANIFEST.csv")
    return cfg


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"download_atlas_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("download_atlas")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h, lv in ((logging.FileHandler(log_path, encoding="utf-8"), logging.DEBUG), (logging.StreamHandler(), logging.INFO)):
        h.setLevel(lv)
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.info("Log file: %s", log_path)
    return logger


class ServerError(RuntimeError):
    pass


def fetch(url: str, params: dict, cfg: dict, logger, accept=None) -> bytes:
    """GET with retries; an OGC ExceptionReport counts as a failed attempt unless accept(body) says otherwise."""
    full = url + "?" + urllib.parse.urlencode(params, safe=":,")
    last = None
    for attempt in range(1, cfg["retries"] + 1):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0 (build-ibge harvester)"})
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            if b"ExceptionReport" in body[:400] or b"ServiceExceptionReport" in body[:400]:
                if accept and accept(body):
                    return body
                raise ServerError(re.sub(rb"\s+", b" ", body[:600]).decode("utf-8", "replace"))
            return body
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ServerError) as exc:
            last = exc
            logger.debug("attempt %d %s: %s", attempt, full, str(exc)[:200])
            time.sleep(3 * attempt)
    raise ServerError(f"{full}: {last}")


def save(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)
    return hashlib.sha256(data).hexdigest()


def wms_layers(xml: bytes, workspace: str, prefix: str) -> dict:
    """{layer: (title, metadata url)} from the WMS capabilities."""
    root = ET.fromstring(xml)
    out = {}
    for lay in root.iter("{http://www.opengis.net/wms}Layer"):
        name = lay.findtext("wms:Name", namespaces=NS) or ""
        short = name.split(":")[-1]
        if not short.lower().startswith(prefix.lower()):
            continue
        res = lay.find("wms:MetadataURL/wms:OnlineResource", NS)
        out[short] = (lay.findtext("wms:Title", namespaces=NS) or "",
                      res.get("{http://www.w3.org/1999/xlink}href") if res is not None else None)
    return out


def schema_columns(xml: bytes) -> tuple:
    """([(name, type)] without geometry, geometry type) from DescribeFeatureType."""
    root = ET.fromstring(xml)
    cols, geom = [], "NA"
    for el in root.iter("{http://www.w3.org/2001/XMLSchema}element"):
        t = el.get("type") or ""
        if el.get("name") and t.startswith("gml:"):
            geom = t[4:].replace("PropertyType", "")
        elif el.get("name") and el.get("maxOccurs"):
            cols.append((el.get("name"), t.split(":")[-1]))
    return cols, geom


def harvest(layer: str, title: str, meta_url, cfg: dict, out: Path, overwrite: bool, logger) -> dict:
    ws = cfg["workspace"]
    typ = f"{ws}:{layer}"
    att = out / "attributes" / f"{layer}.csv"
    sch = out / "schema" / f"{layer}.xml"
    if att.is_file() and sch.is_file() and not overwrite:
        return None
    sxml = fetch(cfg["wfs_url"], dict(service="WFS", version="2.0.0", request="DescribeFeatureType", typeNames=typ), cfg, logger)
    cols, geom = schema_columns(sxml)
    hits = fetch(cfg["wfs_url"], dict(service="WFS", version="2.0.0", request="GetFeature", typeNames=typ, resultType="hits"), cfg, logger)
    n = int(re.search(rb'numberMatched="(\d+)"', hits).group(1))
    params = dict(service="WFS", version="2.0.0", request="GetFeature", typeNames=typ, outputFormat="csv")
    if cols:
        params["propertyName"] = ",".join(c for c, _ in cols)
    body = fetch(cfg["wfs_url"], params, cfg, logger)
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    head = rows[0] if rows else []
    if [c for c in head if c != "FID"] != [c for c, _ in cols]:
        raise ServerError(f"{layer}: CSV columns {head} differ from the schema {cols}")
    if len(rows) - 1 != n:
        raise ServerError(f"{layer}: {len(rows) - 1} rows, server count {n}")
    # the FID column carries a per-request id (changes every call): dropped, so the file is reproducible
    fi = head.index("FID") if "FID" in head else None
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerows([r[:fi] + r[fi + 1:] if fi is not None else r for r in rows])
    sha = save(att, buf.getvalue().encode("utf-8"))
    save(sch, sxml)
    legend = "json"
    try:
        lg = fetch(cfg["wms_url"], dict(service="WMS", version="1.3.0", request="GetLegendGraphic",
                                        format="application/json", layer=typ), cfg, logger)
        json.loads(lg)
        save(out / "legend" / f"{layer}.json", lg)
    except (ServerError, ValueError) as exc:
        legend = "none"
        logger.warning("%s: no JSON legend (%s)", layer, str(exc)[:120])
    mid = "NA"
    if meta_url:
        mid = urllib.parse.parse_qs(urllib.parse.urlparse(meta_url).query).get("id", ["NA"])[0]
        try:
            iso = fetch(meta_url.split("?")[0], dict(urllib.parse.parse_qsl(urllib.parse.urlparse(meta_url).query)), cfg, logger)
            save(out / "iso" / f"{layer}.xml", iso)
        except ServerError as exc:
            logger.warning("%s: no ISO record (%s)", layer, str(exc)[:120])
            mid = "NA"
    return dict(layer=layer, title=title, rows=n, columns=" ".join(c for c, _ in cols), geometry=geom,
                metadata_id=mid, legend=legend, sha256=sha, downloaded_at=f"{datetime.now():%Y-%m-%d %H:%M:%S}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to JSON config file")
    ap.add_argument("--overwrite", action="store_true", help="harvest again the layers already saved")
    ap.add_argument("--layers", nargs="*", help="only these layers (overrides the config)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out = Path(cfg["output_folder"])
    logger = setup_logging(Path(cfg["report_folder"]))
    wms = fetch(cfg["wms_url"], dict(service="WMS", version="1.3.0", request="GetCapabilities"), cfg, logger)
    save(out / "capabilities" / "wms.xml", wms)
    wfs = fetch(cfg["wfs_url"], dict(service="WFS", version="2.0.0", request="GetCapabilities"), cfg, logger)
    save(out / "capabilities" / "wfs.xml", wfs)
    layers = wms_layers(wms, cfg["workspace"], cfg["prefix"])
    in_wfs = set(re.findall(rf"<wfs:Name>{cfg['workspace']}:({re.escape(cfg['prefix'])}[^<]+)</wfs:Name>", wfs.decode("utf-8"),
                            flags=re.IGNORECASE))
    if in_wfs - set(layers):
        logger.warning("in WFS but not in WMS: %s", sorted(in_wfs - set(layers)))
    for extra in sorted(in_wfs - set(layers)):
        layers[extra] = ("", None)
    only = set(args.layers or cfg.get("layers") or [])
    names = sorted(l for l in layers if not only or l in only)
    logger.info("%d layers with prefix %s (%d asked)", len(layers), cfg["prefix"], len(names))

    man_path = out / cfg["manifest_name"]
    manifest = {}
    if man_path.is_file():
        with open(man_path, encoding="utf-8", newline="") as f:
            manifest = {r["layer"]: r for r in csv.DictReader(f, delimiter=";")}

    failed = []

    def one(name):
        try:
            return name, harvest(name, layers[name][0], layers[name][1], cfg, out, args.overwrite, logger), None
        except (ServerError, AttributeError, ET.ParseError) as exc:
            return name, None, exc

    with ThreadPoolExecutor(cfg["workers"]) as ex:
        for name, row, exc in ex.map(one, names):
            if exc is not None:
                logger.error("%s: %s", name, str(exc)[:300])
                failed.append(name)
            elif row is None:
                logger.debug("%s: exists, kept", name)
            else:
                manifest[name] = row
                logger.info("%s: %d rows, %s", name, row["rows"], row["columns"])
    part = man_path.with_name(man_path.name + ".part")
    with open(part, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(manifest[k] for k in sorted(manifest))
    part.replace(man_path)
    if failed:
        logger.error("%d layers failed: %s", len(failed), failed)
        sys.exit(1)
    logger.info("Done: %d layers in the manifest", len(manifest))


if __name__ == "__main__":
    main()

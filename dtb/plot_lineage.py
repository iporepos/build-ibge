"""
Plot the municipal lineage ("splitting") graph built by build_lineage.py.

Trees (config "trees"): starting from one municipality of one snapshot, the graph is walked
  descendants  forward:  continuation, split, recode, redistribution are followed; absorption and transfer
               edges are drawn to their target, which is not expanded further
  ancestors    backward: continuation, split, recode, redistribution, emergence are followed; absorption
               and transfer sources are drawn but not expanded
and rendered with Graphviz (dot) as SVG (node tooltips) and PNG: one column per snapshot year, one node per
municipality and year (name + code), edges colored and dashed by relation, width by share_to. Edges below
min_share (share_to for incoming territory, share_from for absorption) are not drawn.

Summary (config "summary"): municipalities per snapshot and lineage events per interval (PNG).

Usage:
    python plot_lineage.py --config plot_lineage.local.json
"""

import argparse
import html
import subprocess
import sys
from collections import deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dtbtools import NA, load_config, setup_logging  # noqa: E402

REQUIRED_KEYS = ["lineage_csv", "output_folder", "report_folder", "trees", "styles"]
FOLLOW = {"descendants": {"continuation", "split", "recode", "redistribution"},
          "ancestors": {"continuation", "split", "recode", "redistribution", "emergence"}}


def load_edges(path: str) -> pd.DataFrame:
    e = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
    for c in ("year_from", "year_to"):
        e[c] = e[c].astype(int)
    for c in ("share_from", "share_to", "area_km2"):
        e[c] = pd.to_numeric(e[c].replace(NA, ""), errors="coerce")
    return e


def walk(e: pd.DataFrame, code: str, year: int, direction: str, tree: dict) -> pd.DataFrame:
    """Edges reachable from (year, code); nodes reached through a non-followed relation are leaves."""
    min_share = tree.get("min_share", 0.0)
    rels = set(tree.get("relations", FOLLOW[direction] | {"absorption", "transfer", "emergence"}))
    follow = FOLLOW[direction] & rels
    year_lo, year_hi = tree.get("from_year", -1), tree.get("to_year", 9999)
    e = e[e["relation"].isin(rels) & (e["year_from"] >= year_lo) & (e["year_to"] <= year_hi)]
    share = e["share_from"].where(e["relation"] == "absorption", e["share_to"]).fillna(1.0)
    e = e[(share >= min_share) | (e["relation"] == "continuation")]
    if direction == "descendants":
        idx = {k: g.index for k, g in e.groupby(["year_from", "code_from"])}
    else:
        idx = {k: g.index for k, g in e.groupby(["year_to", "code_to"])}
    seen, keep = {(year, code)}, set()
    q = deque([(year, code)])
    while q:
        node = q.popleft()
        for i in idx.get(node, []):
            r = e.loc[i]
            keep.add(i)
            nxt = (r["year_to"], r["code_to"]) if direction == "descendants" else (r["year_from"], r["code_from"])
            if r["relation"] in follow and nxt[1] != NA and nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return e.loc[sorted(keep)]


def to_dot(sub: pd.DataFrame, title: str, styles: dict, root) -> str:
    nodes = {}
    for r in sub.itertuples():
        nodes.setdefault((r.year_from, r.code_from), r.name_from)
        nodes.setdefault((r.year_to, r.code_to), r.name_to)
    if not nodes:
        nodes[root] = root[1]
    years = sorted({y for y, _ in nodes})
    ids = {n: f"n{i}" for i, n in enumerate(sorted(nodes, key=lambda n: (n[0], str(n[1]), str(nodes[n]))))}
    out = ["digraph lineage {",
           '  graph [rankdir=LR, ranksep=1.1, nodesep=0.12, fontname="Helvetica", fontsize=14, labelloc=t, '
           f'label="{html.escape(title)}", bgcolor="#fcfcfb", fontcolor="#0b0b0b"];',
           '  node [shape=box, style="rounded,filled", fillcolor="#ffffff", color="#c3c2b7", fontname="Helvetica", '
           'fontsize=9, fontcolor="#0b0b0b", height=0.3, margin="0.06,0.02"];',
           '  edge [arrowsize=0.5];']
    for y in years:
        out.append(f'  y{y} [label="{y}", shape=plaintext, style="", fontsize=13, fontcolor="#52514e"];')
    out.append("  " + " -> ".join(f"y{y}" for y in years) + " [style=invis];" if len(years) > 1 else "")
    for y in years:
        members = [ids[n] for n in nodes if n[0] == y]
        out.append(f"  {{rank=same; y{y}; {'; '.join(members)};}}")
    for n, nid in ids.items():
        y, c = n
        name = nodes[n] if nodes[n] not in (NA, "") else "?"
        label = name if c == NA else f"{name}\\n{c}"
        extra = ', penwidth=2, color="#0b0b0b"' if n == root else ""
        if c == NA:
            extra += ', style="rounded,dashed", fillcolor="#f2f1ee"'
        out.append(f'  {nid} [label="{html.escape(label)}", tooltip="{html.escape(name)} ({c}, {y})"{extra}];')
    for r in sub.itertuples():
        st = styles[r.relation]
        sh = r.share_from if r.relation == "absorption" else r.share_to
        w = 0.6 + 2.4 * (1.0 if pd.isna(sh) else float(sh))
        tip = f"{r.relation}: {r.name_from} -> {r.name_to}, {r.area_km2:.1f} km2, share_from {r.share_from:.3f}, share_to {r.share_to:.3f}"
        out.append(f'  {ids[(r.year_from, r.code_from)]} -> {ids[(r.year_to, r.code_to)]} '
                   f'[color="{st["color"]}", style={st["style"]}, penwidth={w:.2f}, tooltip="{html.escape(tip)}"];')
    # legend
    used = [k for k in styles if k in set(sub["relation"])]
    if used:
        rows = "".join(f'<TR><TD ALIGN="LEFT"><FONT COLOR="{styles[k]["color"]}">{styles[k]["glyph"]}</FONT></TD>'
                       f'<TD ALIGN="LEFT">{styles[k]["label"]}</TD></TR>' for k in used)
        out.append(f'  legend [shape=plaintext, style="", label=<<TABLE BORDER="0" CELLSPACING="2">{rows}'
                   '<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT COLOR="#52514e">largura = parte do território recebido</FONT></TD></TR></TABLE>>];')
        out.append(f"  {{rank=same; y{years[0]}; legend;}}")
    out.append("}")
    return "\n".join(x for x in out if x)


def render(dot: str, base: Path, logger):
    base.with_suffix(".dot").write_text(dot, encoding="utf-8")
    for fmt in ("svg", "png"):
        args = ["dot", f"-T{fmt}", str(base.with_suffix(".dot")), "-o", str(base.with_suffix(f".{fmt}"))]
        if fmt == "png":
            args.insert(1, "-Gdpi=110")
        subprocess.run(args, check=True)
    logger.info("  wrote %s.{dot,svg,png}", base)


def plot_summary(e: pd.DataFrame, path: Path, styles: dict, logger):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ink, muted, grid, surface = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
    counts = e.groupby("year_to")["code_to"].nunique()
    counts.loc[e["year_from"].min()] = e[e["year_from"] == e["year_from"].min()]["code_from"].replace(NA, pd.NA).dropna().nunique()
    counts = counts.sort_index()
    ev = e[e["relation"] != "continuation"]
    new = ev[ev["relation"].isin(["split", "redistribution", "emergence", "recode"])].groupby("year_to")["code_to"].nunique()
    ext = ev[ev["relation"].isin(["absorption", "recode", "redistribution"])].groupby("year_to")["code_from"].nunique()
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), facecolor=surface)
    for ax in axes:
        ax.set_facecolor(surface)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.spines["left"].set_color(grid)
        ax.spines["bottom"].set_color(muted)
        ax.tick_params(colors=muted, labelsize=9)
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
    ax = axes[0]
    ax.plot(counts.index, counts.values, color=styles["split"]["color"], linewidth=2, marker="o", markersize=5,
            markeredgecolor=surface, markeredgewidth=2)
    for x, y in counts.items():
        if x in (counts.index[0], 1940, 1991, counts.index[-1]):
            ax.annotate(f"{y:,}".replace(",", "."), (x, y), textcoords="offset points", xytext=(0, 8), ha="center",
                        fontsize=9, color=ink)
    ax.set_title("Municípios por levantamento", loc="left", fontsize=11, color=ink)
    ax.set_ylim(0, counts.max() * 1.15)
    ax = axes[1]
    labels = [f"{a}–{str(b)[-2:]}" for a, b in zip(sorted(e["year_from"].unique()), sorted(e["year_to"].unique()))]
    xs = range(len(labels))
    yrs = sorted(e["year_to"].unique())
    bw = 0.38
    ax.bar([x - bw / 2 - 0.01 for x in xs], [new.get(y, 0) for y in yrs], width=bw, color=styles["split"]["color"],
           label="códigos novos (desmembramento, recodificação...)")
    ax.bar([x + bw / 2 + 0.01 for x in xs], [ext.get(y, 0) for y in yrs], width=bw, color=styles["absorption"]["color"],
           label="códigos extintos (anexação, recodificação...)")
    ax.set_xticks(list(xs), labels, rotation=45, ha="right")
    ax.set_title("Eventos por intervalo", loc="left", fontsize=11, color=ink)
    ax.set_ylim(0, max(new.max(), ext.max()) * 1.3)
    leg = ax.legend(frameon=False, fontsize=9, loc="upper left")
    for t in leg.get_texts():
        t.set_color(ink)
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=surface)
    plt.close(fig)
    logger.info("  wrote %s", path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()
    cfg = load_config(args.config, REQUIRED_KEYS)
    logger = setup_logging(Path(cfg["report_folder"]), "plot")
    out = Path(cfg["output_folder"])
    out.mkdir(parents=True, exist_ok=True)
    e = load_edges(cfg["lineage_csv"])
    styles = cfg["styles"]
    for t in cfg["trees"]:
        sub = walk(e, t["code"], int(t["year"]), t["direction"], t)
        n_nodes = len(set(zip(sub["year_from"], sub["code_from"])) | set(zip(sub["year_to"], sub["code_to"])))
        if n_nodes > cfg.get("max_nodes", 600):
            logger.warning("Tree %s has %d nodes (> max_nodes), skipped", t["label"], n_nodes)
            continue
        title = t.get("title", f"{t['label']}")
        logger.info("Tree %s: %d edges, %d nodes", t["label"], len(sub), n_nodes)
        render(to_dot(sub, title, styles, (int(t["year"]), t["code"])), out / t["label"], logger)
    if cfg.get("summary"):
        plot_summary(e, out / cfg["summary"], styles, logger)


if __name__ == "__main__":
    main()

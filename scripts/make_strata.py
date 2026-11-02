#!/usr/bin/env python3
"""
make_strata.py — turn a strata specification into bit ranges and weights.

Spec (TSV, '#' comments), one stratum per line, in PRIORITY order:
    name <TAB> runs <TAB> K <TAB> pattern[,pattern...]
  runs     runs per kernel for the measurement campaign
  K        faults per run (1 = clean single-fault; >1 only for low-density strata)
  pattern  display path of tree nodes; '*' = any text inside one level,
           '**' = anything (several levels). Other characters are literal
           (brackets included), e.g.
             **/g_cores[*]/core/schedule/active_warps
A stratum takes the bits of all matching nodes MINUS the bits already taken by
the strata above it. A line whose pattern is "REST" takes everything that is
left (put it last). The weight of a stratum is its share of the ruler bits.

Usage:
  python3 scripts/make_strata.py --tree ruler_tree.json --spec strata_spec.tsv -o strata.json
"""

import argparse
import json
import re
import sys


def pretty(p):
    return "/".join(c.replace("__BRA__", "[").replace("__KET__", "]").replace("__05F", "_") for c in p.split("/"))


def glob_re(pat):
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def merge(rs):
    out = []
    for lo, hi in sorted(rs):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def subtract(ranges, holes):
    out, j = [], 0
    for lo, hi in ranges:
        cur = lo
        for hl, hh in holes:
            if hh < cur or hl > hi:
                continue
            if hl > cur:
                out.append([cur, hl - 1])
            cur = max(cur, hh + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append([cur, hi])
    return out


def size(rs):
    return sum(h - l + 1 for l, h in rs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("-o", "--output", default="strata.json")
    a = ap.parse_args()

    t = json.load(open(a.tree))
    nodes, total = t["nodes"], t["total_bits"]
    disp = {p: pretty(p) for p in nodes}
    taken, strata = [], []
    for ln, line in enumerate(open(a.spec), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.rstrip("\n").split("\t")
        if len(f) < 4:
            sys.exit(f"{a.spec}:{ln}: want name<TAB>runs<TAB>K<TAB>patterns")
        name, runs, k, pats = f[0].strip(), int(f[1]), int(f[2]), [x.strip() for x in f[3].split(",") if x.strip()]
        if pats == ["REST"]:
            rs = subtract([[0, total - 1]], taken)
            matched = []
        else:
            matched = []
            for pat in pats:
                rx = glob_re(pat)
                m = [p for p, d in disp.items() if rx.match(d)]
                if not m:
                    sys.exit(f"{a.spec}:{ln}: pattern matches no node: {pat}")
                matched += m
            rs = subtract(merge([r for p in matched for r in nodes[p]["ranges"]]), taken)
        if not rs:
            print(f"WARNING: stratum {name} is empty (all its bits are taken by strata above)")
            continue
        taken = merge(taken + rs)
        strata.append({"name": name, "runs": runs, "K": k, "bits": size(rs), "weight": size(rs) / total,
                       "nodes": sorted(disp[p] for p in matched), "ranges": rs})

    covered = size(taken)
    print(f"{'stratum':22s} {'bits':>9s} {'weight':>8s} {'nodes':>5s} {'ranges':>6s} {'K':>3s} {'runs':>6s}")
    for s in strata:
        print(f"{s['name']:22s} {s['bits']:9d} {100 * s['weight']:7.3f}% {len(s['nodes']):5d} {len(s['ranges']):6d} "
              f"{s['K']:3d} {s['runs']:6d}")
    print(f"{'TOTAL':22s} {covered:9d} {100 * covered / total:7.3f}%  runs per kernel: {sum(s['runs'] for s in strata)}")
    if covered != total:
        print(f"WARNING: {total - covered} bits belong to no stratum: add a REST line at the end")
    json.dump({"total_bits": total, "tree": a.tree, "spec": a.spec, "strata": strata}, open(a.output, "w"))
    print(f"written {a.output}")


if __name__ == "__main__":
    main()
    
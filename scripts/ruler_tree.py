#!/usr/bin/env python3
"""
ruler_tree.py — hierarchy of the fault ruler: for every module / signal of the
design, the ruler bit ranges it occupies and their size.

The bit layout is taken from VerilatorBitMapper (the same mapper used to name
injections, which mirrors fault_targets.cpp and the injector exactly): inside a
FAULT_RULER entry, signal j of the run occupies the bits
    [start + j*B, start + (j+1)*B - 1],   B = element_bits * array_depth.
A signal can appear more than once (nested structs are parsed twice by the
generator), so a signal, and even more a module, owns a LIST of ranges.

Hierarchy: the Verilator name split on "__DOT__"
  rtlsim_shim__DOT__vortex__DOT__g_clusters__BRA__0__KET____DOT__... ->
  rtlsim_shim / vortex / g_clusters__BRA__0__KET__ / ...
(display names decode __BRA__ __KET__ __05F into [ ] _).

Commands
  build   --mapper scripts/find_bit.py --header build/ruler_header/Vrtlsim_shim___024root.h -o ruler_tree.json
          (also checks that the ranges tile [0, total) exactly and prints the
          duplication statistics)
  ls      ruler_tree.json [NODE] [--depth D] [--min-bits N]
          children of NODE (default: the root) with bits, share, signals, ranges
  ranges  ruler_tree.json NODE [-o FILE]
          the merged ranges of NODE, one "L-H" per line (FAULT_MULTI_BITS=@FILE)
  find    ruler_tree.json TEXT
          nodes whose path contains TEXT
  profile ruler_tree.json DB [--test-name T] [--node NODE] [--depth D] [--min-fail N]
          map the single-fault runs of a (random, uniform) campaign onto the
          tree: per node faults, failures (non-masked), SDC, per-fault failure
          probability with 95% Wilson interval, and share of all failures.
          Runs with more than one injection are ignored.

NODE is a path with "/" between levels, raw or display form, e.g.
  rtlsim_shim/vortex/g_clusters[0]/...
"""

import argparse
import bisect
import importlib.util
import math
import json
import sys
from collections import defaultdict

SEP = "__DOT__"


def pretty(comp):
    return comp.replace("__BRA__", "[").replace("__KET__", "]").replace("__05F", "_")


def merge(ranges):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def size(ranges):
    return sum(h - l + 1 for l, h in ranges)


# ---------------------------------------------------------------- build
def build(args):
    spec = importlib.util.spec_from_file_location("bit_mapper_module", args.mapper)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mapper = mod.VerilatorBitMapper(args.header)
    mapper.build_memory_map(args.class_name)

    sig = defaultdict(list)
    covered = 0
    for e in mapper.memory_map:
        per = e["element_bits"] * e["array_depth"]
        assert e["start_bit"] == covered, f"gap/overlap at entry {e['first_name']}"
        for j, name in enumerate(e["names"]):
            lo = e["start_bit"] + j * per
            sig[name].append([lo, lo + per - 1])
        covered = e["end_bit"] + 1
    total = mapper.total_bits
    assert covered == total, "ranges do not end at total_bits"

    # cross-check against query_bit on a sample of bits
    import random
    rng = random.Random(0)
    owner = sorted((r[0], r[1], n) for n, rs in sig.items() for r in rs)
    import bisect
    starts = [o[0] for o in owner]
    probes = [rng.randrange(total) for _ in range(5000)] + [0, total - 1] + [int(b) for b in args.check_bits]
    for b in probes:
        i = bisect.bisect_right(starts, b) - 1
        name = owner[i][2]
        q = mapper.query_bit(b)
        assert q.get("signal_name") == name, f"bit {b}: tree says {name}, mapper says {q}"

    # tree
    nodes = {}

    def node(path):
        if path not in nodes:
            nodes[path] = {"ranges": [], "nsig": 0, "children": set(), "leaf": False}
        return nodes[path]

    for name, rs in sig.items():
        comps = name.split(SEP)
        for d in range(1, len(comps) + 1):
            p = "/".join(comps[:d])
            n = node(p)
            n["ranges"].extend(rs)
            n["nsig"] += 1
            if d < len(comps):
                n["children"].add("/".join(comps[:d + 1]))
        node("/".join(comps))["leaf"] = True

    out = {"total_bits": total, "header": args.header, "nodes": {}}
    for p, n in nodes.items():
        rs = merge(n["ranges"])
        out["nodes"][p] = {"bits": size(rs), "nsig": n["nsig"], "leaf": n["leaf"],
                           "children": sorted(n["children"]), "ranges": rs}
    roots = sorted(p for p in nodes if "/" not in p)
    out["roots"] = roots

    dup = [n for n, rs in sig.items() if len(rs) > 1]
    phys = sum(rs[0][1] - rs[0][0] + 1 for rs in sig.values())
    with open(args.output, "w") as f:
        json.dump(out, f)
    print(f"ruler: {total} bits, {len(mapper.memory_map)} entries, {len(sig)} signals, "
          f"{len(dup)} signals listed more than once")
    print(f"distinct signal bits (each signal counted once): {phys}  ->  ruler / distinct = {total / phys:.4f}")
    print(f"tree: {len(nodes)} nodes, roots: {', '.join(roots)}; checked {len(probes)} bits against query_bit: OK")
    print(f"written {args.output}")


# ---------------------------------------------------------------- queries
def load(path):
    with open(path) as f:
        return json.load(f)


def resolve(tree, name):
    if name is None:
        if len(tree["roots"]) == 1:
            return tree["roots"][0]
        return None
    if name in tree["nodes"]:
        return name
    for p in tree["nodes"]:
        if "/".join(pretty(c) for c in p.split("/")) == name:
            return p
    sys.exit(f"node not found: {name}")


def disp(p):
    return "/".join(pretty(c) for c in p.split("/"))


def ls(args):
    t = load(args.tree)
    total = t["total_bits"]
    start = resolve(t, args.node)
    level = [start] if start else t["roots"]
    print(f"{'bits':>9} {'share':>7} {'signals':>7} {'ranges':>6}  node")
    for d in range(args.depth):
        nxt = []
        for p in level:
            n = t["nodes"][p]
            if d > 0 or start is None:
                if n["bits"] >= args.min_bits:
                    print(f"{n['bits']:9d} {100 * n['bits'] / total:6.2f}% {n['nsig']:7d} {len(n['ranges']):6d}  "
                          f"{'  ' * d}{disp(p)}{'' if not n['leaf'] else '  (signal)'}")
            nxt.extend(sorted(n["children"], key=lambda c: -t["nodes"][c]["bits"]))
        level = nxt
        if start is not None and d == 0:
            n = t["nodes"][start]
            print(f"{n['bits']:9d} {100 * n['bits'] / total:6.2f}% {n['nsig']:7d} {len(n['ranges']):6d}  {disp(start)}")


def ranges_cmd(args):
    t = load(args.tree)
    p = resolve(t, args.node)
    rs = t["nodes"][p]["ranges"]
    lines = "".join(f"{l}-{h}\n" if l != h else f"{l}\n" for l, h in rs)
    if args.output:
        with open(args.output, "w") as f:
            f.write(f"# {disp(p)}: {size(rs)} bits in {len(rs)} ranges\n" + lines)
        print(f"{disp(p)}: {size(rs)} bits in {len(rs)} ranges -> {args.output}")
    else:
        sys.stdout.write(lines)


def find(args):
    t = load(args.tree)
    for p, n in sorted(t["nodes"].items()):
        if args.text in p or args.text in disp(p):
            print(f"{n['bits']:9d} {len(n['ranges']):6d}  {disp(p)}{'  (signal)' if n['leaf'] else ''}")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def profile(args):
    import sqlite3
    t = load(args.tree)
    nodes = t["nodes"]
    leaves = sorted((lo, hi, p) for p, n in nodes.items() if n["leaf"] for lo, hi in n["ranges"])
    starts = [x[0] for x in leaves]
    conn = sqlite3.connect(args.db, timeout=60)
    where, params = ("where c.test_name = ?", (args.test_name,)) if args.test_name else ("", ())
    rows = conn.execute(f"""
        select r.outcome, min(i.bit_index), count(i.bit_index)
        from Run r join Campaign c on r.cid = c.id join Injection i on i.run_id = r.run_id
        {where} group by r.run_id""", params).fetchall()
    stat = defaultdict(lambda: [0, 0, 0])          # faults, failures, sdc
    used = skipped = 0
    for outcome, bit, n_inj in rows:
        if n_inj != 1 or bit is None:
            skipped += 1
            continue
        i = bisect.bisect_right(starts, bit) - 1
        if i < 0 or bit > leaves[i][1]:
            skipped += 1
            continue
        used += 1
        comps = leaves[i][2].split("/")
        fail = outcome != "masked_error"
        for d in range(1, len(comps) + 1):
            s_ = stat["/".join(comps[:d])]
            s_[0] += 1
            s_[1] += fail
            s_[2] += outcome == "sdc"
    total_fail = max(1, sum(v[1] for k, v in stat.items() if "/" not in k))
    start = resolve(t, args.node)
    print(f"{used} single-fault runs mapped ({skipped} skipped), {total_fail} failures")
    print(f"{'bits%':>6} {'faults':>7} {'fail':>5} {'sdc':>4} {'p_fail per fault [95% CI]':>27} {'fail%':>6}  node")
    level = [start]
    for d in range(args.depth + 1):
        nxt = []
        for p in level:
            f, k, sdc = stat.get(p, [0, 0, 0])
            n = nodes[p]
            if d == 0 or k >= args.min_fail:
                pr, lo, hi = wilson(k, f)
                print(f"{100 * n['bits'] / t['total_bits']:6.2f} {f:7d} {k:5d} {sdc:4d} "
                      f"{100 * pr:7.3f}% [{100 * lo:6.3f},{100 * hi:7.3f}]% {100 * k / total_fail:5.1f}%  "
                      f"{'  ' * d}{disp(p)}{'  (signal)' if n['leaf'] else ''}")
            nxt.extend(sorted(n["children"], key=lambda c: -stat.get(c, [0, 0])[1]))
        level = nxt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--mapper", required=True, help="script defining VerilatorBitMapper (find_bit.py)")
    b.add_argument("--header", required=True, help="the ruler header (build/ruler_header/Vrtlsim_shim___024root.h)")
    b.add_argument("--class-name", default="Vrtlsim_shim___024root")
    b.add_argument("--check-bits", nargs="*", default=[], help="extra bit indices to cross-check")
    b.add_argument("-o", "--output", default="ruler_tree.json")
    l = sub.add_parser("ls")
    l.add_argument("tree"); l.add_argument("node", nargs="?")
    l.add_argument("--depth", type=int, default=1); l.add_argument("--min-bits", type=int, default=0)
    r = sub.add_parser("ranges")
    r.add_argument("tree"); r.add_argument("node"); r.add_argument("-o", "--output")
    pr = sub.add_parser("profile")
    pr.add_argument("tree"); pr.add_argument("db"); pr.add_argument("--test-name")
    pr.add_argument("--node"); pr.add_argument("--depth", type=int, default=6)
    pr.add_argument("--min-fail", type=int, default=3, help="hide nodes with fewer failures")
    f = sub.add_parser("find")
    f.add_argument("tree"); f.add_argument("text")
    a = ap.parse_args()
    {"build": build, "ls": ls, "ranges": ranges_cmd, "find": find, "profile": profile}[a.cmd](a)


if __name__ == "__main__":
    main()
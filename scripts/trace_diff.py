#!/usr/bin/env python3
"""
trace_diff.py — compare the warp-control trace of a fault-free (golden) run
with the one of a faulty run (files written with FAULT_TRACE_FILE).

For every core it reports:
  - where the two runs first diverge, and in which fields;
  - per warp: how many times it was activated (spawn), when it last went
    inactive, how many PC changes it made (~ instructions issued), with the
    faulty/golden difference;
  - the PC sequence of the diverging warp around the divergence
    (golden vs faulty side by side), to see skipped/repeated instructions or a
    jump out of the code;
  - thread-mask and mscratch values that never appear in the golden run.

Usage:
  ./trace_diff.py golden.txt faulty.txt [--window 12] [--inject-ts 38050]
"""

import argparse
from collections import defaultdict

FIELDS = ["act", "stall", "bar", "tmask", "pc0", "pc1", "msc"]
WARPS = 2
THREADS = 8


def load(path):
    """-> {core: [(ts, {field: int})]}, end_ts"""
    per_core = defaultdict(list)
    end = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# end"):
                    end = int(line.split()[2])
                continue
            parts = line.split()
            ts, core = int(parts[0]), int(parts[1][1:])
            vals = {k: int(v, 16) for k, v in (p.split("=") for p in parts[2:])}
            per_core[core].append((ts, vals))
    return per_core, end


def warp_stats(seq):
    """per warp: activations, last deactivation ts, number of PC changes."""
    st = {w: {"spawns": 0, "last_end": None, "pc_changes": 0} for w in range(WARPS)}
    prev = None
    for ts, v in seq:
        for w in range(WARPS):
            a = (v["act"] >> w) & 1
            pa = (prev["act"] >> w) & 1 if prev else 0
            if a and not pa:
                st[w]["spawns"] += 1
            if pa and not a:
                st[w]["last_end"] = ts
            if prev and v[f"pc{w}"] != prev[f"pc{w}"]:
                st[w]["pc_changes"] += 1
        prev = v
    return st


def pc_seq(seq, w, t_from, n):
    out = []
    prev = None
    for ts, v in seq:
        if ts < t_from:
            prev = v[f"pc{w}"]
            continue
        pc = v[f"pc{w}"]
        if pc != prev:
            out.append((ts, pc))
            prev = pc
        if len(out) >= n:
            break
    return out


def first_divergence(g, f):
    """first index where the two change-sequences differ (ts or values)."""
    for i, (a, b) in enumerate(zip(g, f)):
        if a != b:
            ts = min(a[0], b[0])
            fields = [k for k in FIELDS if a[1].get(k) != b[1].get(k)]
            return ts, fields, i
    if len(g) != len(f):
        i = min(len(g), len(f))
        ts = (g[i] if i < len(g) else f[i])[0]
        return ts, ["(one run has more changes)"], i
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("golden")
    ap.add_argument("faulty")
    ap.add_argument("--window", type=int, default=12, help="PC changes shown around the divergence")
    ap.add_argument("--inject-ts", type=int, help="injection timestamp (for reference)")
    a = ap.parse_args()

    G, gend = load(a.golden)
    F, fend = load(a.faulty)
    print(f"kernel end: golden {gend}  faulty {fend}"
          + (f"  | injection at {a.inject_ts}" if a.inject_ts is not None else ""))

    for c in sorted(set(G) | set(F)):
        g, f = G.get(c, []), F.get(c, [])
        print(f"\n===== core {c} =====")
        d = first_divergence(g, f)
        if d is None:
            print("identical trace")
            continue
        ts, fields, i = d
        print(f"first divergence at ts {ts} in {', '.join(fields)}")
        before = g[i - 1][1] if i > 0 else None
        if before:
            print(f"  state before: act={before['act']:x} tmask={before['tmask']:04x} "
                  f"pc0={before['pc0']:08x} pc1={before['pc1']:08x} msc={before['msc']:08x}")
        gi = g[i][1] if i < len(g) else None
        fi = f[i][1] if i < len(f) else None
        for name, v, t in (("golden", gi, g[i][0] if gi else None), ("faulty", fi, f[i][0] if fi else None)):
            if v:
                print(f"  {name:<6} @ {t}: act={v['act']:x} stall={v['stall']:x} tmask={v['tmask']:04x} "
                      f"pc0={v['pc0']:08x} pc1={v['pc1']:08x} msc={v['msc']:08x}")

        gs, fs = warp_stats(g), warp_stats(f)
        print(f"  {'warp':<5}{'spawns g/f':>12}{'last end g/f':>22}{'PC changes g/f (diff)':>30}")
        for w in range(WARPS):
            x, y = gs[w], fs[w]
            print(f"  {w:<5}{x['spawns']:>6}/{y['spawns']:<5}{str(x['last_end']):>11}/{str(y['last_end']):<10}"
                  f"{x['pc_changes']:>14}/{y['pc_changes']:<6}({y['pc_changes'] - x['pc_changes']:+d})")

        # per warp: first point where the PC sequences differ, and the PCs around it
        for w in range(WARPS):
            gall = pc_seq(g, w, 0, 10 ** 9)
            fall = pc_seq(f, w, 0, 10 ** 9)
            k0 = next((k for k, (x, y) in enumerate(zip(gall, fall)) if x != y), None)
            if k0 is None:
                if len(gall) == len(fall):
                    continue
                k0 = min(len(gall), len(fall))
            start = max(0, k0 - 2)
            gp = gall[start:start + a.window]
            fp = fall[start:start + a.window]
            t0 = (fall[k0] if k0 < len(fall) else gall[k0])[0]
            print(f"  warp {w}: PC sequence diverges at ts {t0} (golden | faulty):")
            for k in range(max(len(gp), len(fp))):
                gl = f"{gp[k][0]:>7} {gp[k][1]:08x}" if k < len(gp) else " " * 16
                fl = f"{fp[k][0]:>7} {fp[k][1]:08x}" if k < len(fp) else ""
                mark = "" if k < len(gp) and k < len(fp) and gp[k][1] == fp[k][1] else "  <--"
                print(f"     {gl}  |  {fl}{mark}")

        gm = {v["tmask"] for _, v in g}
        fm = {v["tmask"] for _, v in f}
        new_masks = sorted(fm - gm)
        if new_masks:
            desc = []
            for m in new_masks[:6]:
                lanes = [(w, [t for t in range(THREADS) if (m >> (w * THREADS + t)) & 1]) for w in range(WARPS)]
                desc.append(f"{m:04x} " + " ".join(f"w{w}:{l}" for w, l in lanes))
            print(f"  thread masks never seen in golden: " + "; ".join(desc))
        gx = {v["msc"] for _, v in g}
        fx = sorted({v["msc"] for _, v in f} - gx)
        if fx:
            print(f"  mscratch values never seen in golden: {[hex(x) for x in fx[:6]]}")


if __name__ == "__main__":
    main()


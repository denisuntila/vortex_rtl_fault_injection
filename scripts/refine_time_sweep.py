#!/usr/bin/env python3
"""
refine_time_sweep.py — one bisection round of the temporal sweep.

For every selected bit it reads all the single-fault runs already in the DB
(any sweep / round), sorts them by injection cycle and looks at consecutive
points whose outcome class differs (sdc / masked / fail = crash+timeout).
Every such edge wider than --resolution gets its midpoint (even timestamp)
added to a new round. Repeating rounds halves the uncertainty on every window
edge; a round with nothing left to add means all edges are resolved.

Output (same layout as gen_time_sweep.py, so run_time_sweep.sh runs it):
  <out_dir>/shard_<n>of<m>.txt   pairs "timestamp bit_index"
  <out_dir>/manifest.tsv         label bit_index timestamp kind
It also prints, per bit, the current windows with their edge uncertainty.

Masked runs without an Injection row (older campaigns): pass the manifest(s)
of those sweeps with --manifest and add --unmatched-masked; points listed
there without any Injection row are then taken as masked.

Usage:
  ./refine_time_sweep.py --db tests/ocl_test/sweep_phase1_v2.db \
      --bits sweep_bits_v2.tsv --only 'mscratch|warp_pcs|thread_masks|tag_store' \
      --manifest tests/ocl_test/sweep_p1v2/manifest.tsv --unmatched-masked \
      -o tests/ocl_test/sweep_p2_r1 --shards 4 --resolution 200

  # report only, no new round
  ./refine_time_sweep.py --db ... --bits ... --report-only
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict

CLASS = {"sdc": "S", "masked_error": ".", "crash": "F", "timeout": "F"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Sweep DB (all rounds go into the same DB)")
    p.add_argument("--bits", required=True, help="TSV label bit_index ... (as for gen_time_sweep.py)")
    p.add_argument("--only", help="Regex on labels: refine only matching bits")
    p.add_argument("--manifest", action="append", default=[],
                   help="Manifest of an earlier sweep (repeatable), used with --unmatched-masked")
    p.add_argument("--unmatched-masked", action="store_true",
                   help="Points of --manifest without an Injection row count as masked")
    p.add_argument("-o", "--output-dir", help="Directory for the new round")
    p.add_argument("--shards", type=int, default=4)
    p.add_argument("--resolution", type=int, default=200,
                   help="Stop refining an edge when its two points are closer than this (default 200)")
    p.add_argument("--min-cycle", type=int, default=150)
    p.add_argument("--report-only", action="store_true", help="Print windows, do not write a round")
    return p.parse_args()


def load_bits(path, only):
    rx = re.compile(only) if only else None
    bits = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            label, bit = cols[0].strip(), int(cols[1])
            if rx is None or rx.search(label):
                bits.append((label, bit))
    return bits


def main():
    a = parse_args()
    if not a.report_only and not a.output_dir:
        sys.exit("need -o/--output-dir (or --report-only)")

    bits = load_bits(a.bits, a.only)
    if not bits:
        sys.exit("no bit selected")
    wanted = {b for _, b in bits}

    # observations: bit -> {cycle: class}; later runs override earlier ones
    obs = defaultdict(dict)
    conn = sqlite3.connect(a.db, timeout=60)
    ph = ",".join("?" * len(wanted))
    for bit, cyc, outcome in conn.execute(
            f"""SELECT i.bit_index, i.cycle, r.outcome FROM Injection i
                JOIN Run r ON r.run_id = i.run_id
                WHERE i.bit_index IN ({ph}) ORDER BY r.run_id""", sorted(wanted)):
        obs[bit][cyc] = CLASS.get(outcome, "F")
    conn.close()

    if a.unmatched_masked:
        for mpath in a.manifest:
            with open(mpath, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f, delimiter="\t"):
                    b, t = int(r["bit_index"]), int(r["timestamp"])
                    if b in wanted and not any(abs(t - c) <= 2 for c in obs[b]):
                        obs[b][t] = "."

    new_pairs = []
    width = max(len(l) for l, _ in bits)
    print(f"{'bit':<{width}} {'points':>6}  windows (S=sdc .=masked F=crash/timeout; "
          f"[a|b] = edge between points a and b)")
    for label, bit in bits:
        pts = sorted(obs[bit].items())
        if not pts:
            print(f"{label:<{width}} {0:>6}  no data")
            continue
        # windows: maximal runs of the same class
        segs = []
        start = 0
        for i in range(1, len(pts) + 1):
            if i == len(pts) or pts[i][1] != pts[start][1]:
                segs.append((pts[start][1], start, i - 1))
                start = i
        desc = []
        for k, (cls, i0, i1) in enumerate(segs):
            lo = pts[i0][0] if k == 0 else f"[{pts[i0 - 1][0]}|{pts[i0][0]}]"
            hi = pts[i1][0] if k == len(segs) - 1 else f"[{pts[i1][0]}|{pts[i1 + 1][0]}]"
            desc.append(f"{cls} {lo}..{hi}")
        print(f"{label:<{width}} {len(pts):>6}  " + "  ".join(desc))

        # edges to refine
        have = set(obs[bit])
        for (t1, c1), (t2, c2) in zip(pts, pts[1:]):
            if c1 != c2 and t2 - t1 > a.resolution:
                mid = 2 * round((t1 + t2) / 4)
                if mid <= t1:
                    mid = t1 + 2
                if mid not in have and mid >= a.min_cycle:
                    new_pairs.append((mid, bit, label))
                    have.add(mid)

    print(f"\nEdges still wider than {a.resolution}: {len(new_pairs)} new points")
    if a.report_only or not new_pairs:
        if not new_pairs:
            print("All edges resolved at this resolution.")
        return 0 if new_pairs == [] else 0

    os.makedirs(a.output_dir, exist_ok=True)
    new_pairs.sort()
    shards = [[] for _ in range(a.shards)]
    for i, p in enumerate(new_pairs):
        shards[i % a.shards].append(p)
    for n, sh in enumerate(shards):
        with open(os.path.join(a.output_dir, f"shard_{n}of{a.shards}.txt"), "w") as f:
            for t, b, _ in sh:
                f.write(f"{t} {b}\n")
    with open(os.path.join(a.output_dir, "manifest.tsv"), "w") as f:
        f.write("label\tbit_index\ttimestamp\tkind\n")
        for t, b, l in sorted(new_pairs, key=lambda x: (x[2], x[0])):
            f.write(f"{l}\t{b}\t{t}\tbisect\n")
    print(f"Round written to {a.output_dir} ({a.shards} shards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

    
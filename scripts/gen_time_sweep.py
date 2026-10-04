#!/usr/bin/env python3
"""
gen_time_sweep.py — build the pairs-files for a temporal sweep: every selected
bit is injected (single fault) at a grid of timestamps, so you can see WHEN in
the kernel each structure is vulnerable.

Input: a TSV (sweep_bits.tsv) with columns
    label  bit_index  original_sdc_cycles  [note]
Lines starting with '#' are ignored. original_sdc_cycles is a comma-separated
list (or '-'); those cycles are always added to the grid as a sanity check
(injecting there must reproduce the SDC).

Grid: --points timestamps evenly spaced in [--min-cycle, --max-cycle]
(defaults 150 .. 57000; set --max-cycle to the end of the kernel measured on a
fault-free run).

Output: <out_dir>/shard_<n>of<m>.txt, pairs "timestamp bit_index" distributed
round-robin, one file per parallel instance, plus <out_dir>/manifest.tsv.
Run each shard WITHOUT the stop-at-first-SDC wrapper, e.g. 4 instances:

  for n in 0 1 2 3; do
    DB=tests/ocl_test/time_sweep.db ./scripts/run_campaign.sh deterministic \
        sweep/shard_${n}of4.txt > sweep/log_${n}of4.txt 2>&1 &
  done; wait

Usage:
  ./gen_time_sweep.py --bits sweep_bits.tsv -o sweep --shards 4 \
      --points 30 --max-cycle 57000
"""

import argparse
import os
import sys


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bits", required=True, help="TSV: label bit_index original_sdc_cycles [note]")
    p.add_argument("-o", "--output-dir", required=True)
    p.add_argument("--shards", type=int, default=1, help="Number of parallel instances (default 1)")
    p.add_argument("--points", type=int, default=30, help="Grid points per bit (default 30)")
    p.add_argument("--min-cycle", type=int, default=150)
    p.add_argument("--max-cycle", type=int, default=57000)
    return p.parse_args()


def load_bits(path):
    bits = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 2:
                sys.exit(f"{path}:{ln}: need at least label and bit_index")
            label, bit = cols[0].strip(), int(cols[1])
            orig = []
            if len(cols) > 2 and cols[2].strip() not in ("", "-"):
                orig = [int(x) for x in cols[2].split(",") if x.strip()]
            bits.append((label, bit, orig))
    return bits


def main():
    a = parse_args()
    if a.shards < 1 or a.points < 2 or a.max_cycle <= a.min_cycle:
        sys.exit("Need shards >= 1, points >= 2, max-cycle > min-cycle")

    bits = load_bits(a.bits)
    step = (a.max_cycle - a.min_cycle) / (a.points - 1)
    # the injector only fires on EVEN timestamps (odd ones never inject and
    # leave no Injection row), so every grid point is rounded to an even value
    grid = sorted({2 * round((a.min_cycle + i * step) / 2) for i in range(a.points)})
    odd_orig = [(l, t) for l, _, orig in bits for t in orig if t % 2]
    if odd_orig:
        print(f"WARNING: odd original cycles (will not inject): {odd_orig}", file=sys.stderr)

    pairs = []          # (ts, bit, label, kind)
    for label, bit, orig in bits:
        cycles = {t: "grid" for t in grid}
        for t in orig:
            cycles[t] = "original"
        for t in sorted(cycles):
            pairs.append((t, bit, label, cycles[t]))

    # interleave bits so every shard gets a mix of early/late and cheap/expensive runs
    pairs.sort(key=lambda x: (x[0], x[1]))
    os.makedirs(a.output_dir, exist_ok=True)
    shards = [[] for _ in range(a.shards)]
    for i, pr in enumerate(pairs):
        shards[i % a.shards].append(pr)

    for n, sh in enumerate(shards):
        with open(os.path.join(a.output_dir, f"shard_{n}of{a.shards}.txt"), "w") as f:
            for t, bit, _, _ in sh:
                f.write(f"{t} {bit}\n")

    with open(os.path.join(a.output_dir, "manifest.tsv"), "w") as f:
        f.write("label\tbit_index\ttimestamp\tkind\n")
        for t, bit, label, kind in sorted(pairs, key=lambda x: (x[2], x[0])):
            f.write(f"{label}\t{bit}\t{t}\t{kind}\n")

    print(f"{len(bits)} bits x {len(grid)} grid points (+ original cycles) = {len(pairs)} runs")
    print(f"Grid: {grid[0]} .. {grid[-1]}, step ~{step:.0f} cycles")
    for n, sh in enumerate(shards):
        print(f"  shard_{n}of{a.shards}.txt: {len(sh)} runs")
    print(f"Manifest: {os.path.join(a.output_dir, 'manifest.tsv')}")


if __name__ == "__main__":
    main()


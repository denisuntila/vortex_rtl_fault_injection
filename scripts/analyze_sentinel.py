#!/usr/bin/env python3
"""
analyze_sentinel.py — detection coverage of the sentinel lanes (ocl_sentinel).

Every run is classified by its real outcome (from the result matrix C) and by
the sentinel alarm (from the Sentinel table written by run_experiment.py):

  masked, no alarm   fault had no effect, nothing reported            (ok)
  masked, alarm      no wrong output, but the sentinel fired          (false alarm / benign detection)
  SDC detected       wrong output AND alarm                           (covered)
  SDC missed         wrong output, no alarm                           (escaped)
  crash / timeout    no status printed: detected by other means

SDCs are split by size (number of wrong elements of C), since the sentinel is
designed for the "common mode" ones (a whole warp = 7 elements per round, or
more) and by construction cannot see an error confined to one real lane:
  1        one element
  2-6      several elements, less than a warp
  >=7      a warp or more  (common mode)

Usage:
  python3 scripts/analyze_sentinel.py --db <db> [--bits sweep_bits_v2.tsv] [--test-name NAME] [--csv out.csv]
  --bits: TSV "label bit_index ..." to group by signal (otherwise by bit_index)
"""

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict

COLS = ["runs", "masked", "masked_alarm", "sdc", "sdc_det", "sdc_miss",
        "sdc1", "sdc1_det", "sdc2_6", "sdc2_6_det", "sdc7", "sdc7_det", "crash", "timeout", "no_sentinel"]


def load_bits(path):
    labels = defaultdict(list)
    with open(path) as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            labels[int(p[1])].append(p[0])
    return {b: "/".join(v) for b, v in labels.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--bits", help="TSV label<TAB>bit_index... to name the bits")
    ap.add_argument("--test-name", help="only this Campaign.test_name")
    ap.add_argument("--csv")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    if not conn.execute("select 1 from sqlite_master where name='Sentinel'").fetchone():
        sys.exit("no Sentinel table: were these runs made with ocl_sentinel and the patched run_experiment.py?")
    names = load_bits(args.bits) if args.bits else {}

    where, params = "", ()
    if args.test_name:
        where, params = "where c.test_name = ?", (args.test_name,)
    rows = conn.execute(f"""
        select r.run_id, r.outcome,
               (select min(i.bit_index) from Injection i where i.run_id = r.run_id) as bit,
               (select count(*) from SDC s where s.run_id = r.run_id) as n_wrong,
               s.alarm
        from Run r join Campaign c on r.cid = c.id
        left join Sentinel s on s.run_id = r.run_id
        {where}""", params).fetchall()
    if not rows:
        sys.exit("no runs")

    stats = defaultdict(lambda: dict.fromkeys(COLS, 0))
    for run_id, outcome, bit, n_wrong, alarm in rows:
        key = names.get(bit, str(bit)) if bit is not None else "(no injection row)"
        for k in (key, "TOTAL"):
            d = stats[k]
            d["runs"] += 1
            if outcome in ("crash", "timeout"):
                d[outcome] += 1
                continue
            if alarm is None:
                d["no_sentinel"] += 1
                continue
            if outcome == "masked_error":
                d["masked"] += 1
                d["masked_alarm"] += alarm
            elif outcome == "sdc":
                d["sdc"] += 1
                d["sdc_det" if alarm else "sdc_miss"] += 1
                size = "sdc1" if n_wrong <= 1 else ("sdc2_6" if n_wrong <= 6 else "sdc7")
                d[size] += 1
                d[size + "_det"] += alarm

    def pct(a, b):
        return f"{100 * a / b:5.1f}%" if b else "    -"

    keys = sorted(k for k in stats if k != "TOTAL") + ["TOTAL"]
    print(f"{'signal':26s} {'runs':>5s} {'mask':>5s} {'m+alm':>5s} | {'SDC':>4s} {'det':>4s} {'cover':>6s} |"
          f" {'1el':>8s} {'2-6':>8s} {'>=7 (cm)':>10s} | {'crash':>5s} {'tmo':>4s}")
    for k in keys:
        d = stats[k]
        if k == "TOTAL":
            print("-" * 110)
        print(f"{k[:26]:26s} {d['runs']:5d} {d['masked']:5d} {d['masked_alarm']:5d} | {d['sdc']:4d} {d['sdc_det']:4d}"
              f" {pct(d['sdc_det'], d['sdc']):>6s} | {d['sdc1_det']:3d}/{d['sdc1']:<4d} {d['sdc2_6_det']:3d}/{d['sdc2_6']:<4d}"
              f" {d['sdc7_det']:4d}/{d['sdc7']:<5d} | {d['crash']:5d} {d['timeout']:4d}"
              + (f"  ({d['no_sentinel']} without sentinel data)" if d["no_sentinel"] else ""))
    print("\nm+alm = masked runs where the sentinel fired; columns x/y = detected/total SDCs of that size;"
          "\ncm = common mode (a warp or more of wrong elements)")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["signal"] + COLS)
            for k in keys:
                w.writerow([k] + [stats[k][c] for c in COLS])
        print(f"written {args.csv}")


if __name__ == "__main__":
    main()

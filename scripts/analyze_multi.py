#!/usr/bin/env python3
"""
analyze_multi.py — per-target results of a multi-injection campaign.

Each run flips the target K times. If the K faults act independently and each
one alone causes a failure with probability a (the per-fault vulnerability of
that bit, the quantity a single-fault campaign would measure), then

    P(run not masked) = p = 1 - (1 - a)^K   ->   a = 1 - (1 - p)^(1/K)

The 95% Wilson interval of p is mapped through the same (monotonic) formula.
Per-outcome rates a_o are approximated by a * (n_o / n_failed).

Independence check: run the same target with two different K. If the two
estimates of a agree (overlapping intervals), multi-injection is valid for that
structure; if the larger K gives a clearly smaller a, the faults interact
(e.g. a second flip undoes the first on a bit that is not rewritten in between)
and that structure needs single-fault runs.

Usage:
  python3 scripts/analyze_multi.py <db> [--csv out.csv]
"""

import argparse
import csv
import math
import re
import sqlite3
import sys
from collections import defaultdict

OUTCOMES = ("sdc", "crash", "timeout")


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def per_fault(p, K):
    return 1 - (1 - p) ** (1 / K) if p < 1 else 1.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--csv", help="also write the table as CSV")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    rows = conn.execute(
        """select c.test_name, r.outcome, count(*), avg(r.injection_count)
           from Run r join Campaign c on r.cid = c.id
           where c.test_name like 'multi:%' group by 1, 2"""
    ).fetchall()
    if not rows:
        sys.exit("no multi:* campaigns in this DB")

    data = defaultdict(lambda: {"n": 0, "inj_sum": 0.0, **{o: 0 for o in OUTCOMES}, "masked_error": 0})
    for tname, outcome, cnt, avg_inj in rows:
        d = data[tname]
        d["n"] += cnt
        d["inj_sum"] += (avg_inj or 0) * cnt
        d[outcome if outcome in d else "crash"] += cnt

    table = []
    for tname, d in data.items():
        m = re.match(r"multi:(.*):K(\d+)$", tname)
        label, K = (m.group(1), int(m.group(2))) if m else (tname, 1)
        n = d["n"]
        failed = n - d["masked_error"]
        p, plo, phi = wilson(failed, n)
        a, alo, ahi = per_fault(p, K), per_fault(plo, K), per_fault(phi, K)
        row = {"label": label, "K": K, "runs": n, "avg_inj": d["inj_sum"] / n,
               "masked": d["masked_error"], **{o: d[o] for o in OUTCOMES},
               "p_run": p, "a": a, "a_lo": alo, "a_hi": ahi}
        for o in OUTCOMES:
            row["a_" + o] = a * d[o] / failed if failed else 0.0
        table.append(row)
    table.sort(key=lambda r: (r["label"], r["K"]))

    print(f"{'target':28s} {'K':>3s} {'runs':>5s} {'inj':>5s} {'mask':>5s} {'sdc':>4s} {'crash':>5s} {'tmo':>4s}"
          f" {'P(run)':>7s}   {'a per fault [95% CI]':24s} {'a_sdc':>7s} {'a_crash':>7s} {'a_tmo':>7s}")
    prev = None
    for r in table:
        flag = ""
        if prev and prev["label"] == r["label"] and (r["a_lo"] > prev["a_hi"] or r["a_hi"] < prev["a_lo"]):
            flag = "  <- disagrees with K=%d: faults interact?" % prev["K"]
        print(f"{r['label']:28s} {r['K']:3d} {r['runs']:5d} {r['avg_inj']:5.1f} {r['masked']:5d} {r['sdc']:4d}"
              f" {r['crash']:5d} {r['timeout']:4d} {100*r['p_run']:6.1f}%   "
              f"{100*r['a']:6.2f}% [{100*r['a_lo']:5.2f}, {100*r['a_hi']:5.2f}]%  "
              f"{100*r['a_sdc']:6.2f}% {100*r['a_crash']:6.2f}% {100*r['a_timeout']:6.2f}%{flag}")
        prev = r
    print("\ninj = faults actually injected per run on average (< K when a crash ends the run early)")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
            w.writeheader()
            w.writerows(table)
        print(f"written {args.csv}")


if __name__ == "__main__":
    main()

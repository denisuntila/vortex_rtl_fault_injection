#!/usr/bin/env python3
"""
analyze_time_sweep.py — read back the temporal sweep produced with
gen_time_sweep.py and show, for every bit, the outcome as a function of the
injection timestamp.

It joins manifest.tsv (label, bit_index, timestamp) with the sweep DB
(Injection.bit_index/cycle -> Run.outcome, and the number of wrong output
elements from the SDC table), then prints one timeline per bit:

    c1.warp_pcs.a   935546  ....SSSS.SS..SSS....  SDC 9/31  window 4500-47000
                            ^ one char per timestamp, left = early

    .  masked_error     S  sdc (poison or numeric)     C  crash
    T  timeout/other    -  no Injection row for this   * original SDC cycle
                           (bit, cycle): not run yet,
                           or no fault was injected
                                                         reproduced (sdc)
                                                       ! original SDC cycle
                                                         NOT reproduced

A per-run CSV (--csv) keeps outcome, number of wrong elements and whether
they are poison, so the extent-vs-time relation (e.g. lost block size for
warp_pcs) can be plotted later.

Usage:
  ./analyze_time_sweep.py --db tests/ocl_test/time_sweep.db \
      --manifest sweep/manifest.tsv --csv sweep/results.csv
"""

import argparse
import csv
import sqlite3
from collections import defaultdict

POISON = 0xBAADF00D


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Sweep SQLite DB")
    p.add_argument("--manifest", required=True, help="manifest.tsv from gen_time_sweep.py")
    p.add_argument("--csv", help="Write per-run results here")
    p.add_argument("--unmatched-masked", action="store_true",
                   help="Treat every point without an Injection row as masked_error. Use it only "
                        "when the sweep has completed (every point was run at least once) and "
                        "masked runs were stored without Injection rows.")
    p.add_argument("--tolerance", type=int, default=2,
                   help="Max |logged cycle - requested timestamp| to match (default 2)")
    return p.parse_args()


def to_int(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v & 0xFFFFFFFF
    s = str(v).strip().lower()
    try:
        return int(s, 16) if s.startswith("0x") else int(s)
    except ValueError:
        return None


def main():
    a = parse_args()

    manifest = []
    with open(a.manifest, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            manifest.append((r["label"], int(r["bit_index"]), int(r["timestamp"]), r["kind"]))

    conn = sqlite3.connect(a.db, timeout=60)
    cur = conn.cursor()
    tables = {r[0].lower() for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"run", "injection"} - tables
    if missing:
        raise SystemExit(f"{a.db}: tables {sorted(missing)} missing - no run was stored. "
                         f"Check the shard logs for the error.")
    bits = sorted({m[1] for m in manifest})
    ph = ",".join("?" * len(bits))

    # Injections actually logged, per bit. The logged cycle can differ from the
    # requested timestamp (an odd timestamp fires on the next tick), so each
    # manifest point is matched to the nearest logged cycle within --tolerance.
    logged = defaultdict(list)  # bit -> [(cycle, run_id, outcome)]
    for run_id, bit, cyc, outcome in cur.execute(
            f"""SELECT r.run_id, i.bit_index, i.cycle, r.outcome
                FROM Injection i JOIN Run r ON r.run_id = i.run_id
                WHERE i.bit_index IN ({ph}) ORDER BY r.run_id""", bits):
        logged[bit].append((cyc, run_id, outcome))

    res = {}
    used = set()
    # exact matches first, then nearest within tolerance
    for tol in range(0, a.tolerance + 1):
        for _, bit, ts, _ in manifest:
            if (bit, ts) in res:
                continue
            for cyc, run_id, outcome in logged.get(bit, []):
                if run_id not in used and abs(cyc - ts) == tol:
                    res[(bit, ts)] = (run_id, outcome)
                    used.add(run_id)
                    break

    # Runs that ended masked_error may have no Injection row at all (the DB only
    # logs injections of non-masked runs). If every manifest point has been run
    # (#runs in DB == #points) and the unmatched points are exactly as many as
    # the masked runs without injections, those points are inferred masked.
    n_runs = cur.execute("SELECT COUNT(*) FROM Run").fetchone()[0]
    n_masked_noinj = cur.execute(
        """SELECT COUNT(*) FROM Run r WHERE r.outcome = 'masked_error'
           AND NOT EXISTS (SELECT 1 FROM Injection i WHERE i.run_id = r.run_id)""").fetchone()[0]
    unmatched = [(b, t) for _, b, t, _ in manifest if (b, t) not in res]
    inferred = False
    if unmatched and (a.unmatched_masked or
                      (n_runs == len(manifest) and len(unmatched) == n_masked_noinj)):
        for key in unmatched:
            res[key] = (None, "masked_error")
        inferred = True

    # SDC extent per run
    cols = [c[1].lower() for c in cur.execute("PRAGMA table_info(SDC)")]
    act = next((c for c in cols if c.startswith("actual")), None)
    extent = defaultdict(lambda: [0, 0])  # run_id -> [n_wrong, n_poison]
    if act:
        for run_id, val in cur.execute(f"SELECT run_id, {act} FROM SDC"):
            e = extent[run_id]
            e[0] += 1
            if to_int(val) == POISON:
                e[1] += 1
    conn.close()

    by_label = defaultdict(list)
    rows = []
    for label, bit, ts, kind in manifest:
        run_id, outcome = res.get((bit, ts), (None, None))
        n_wrong, n_poison = extent.get(run_id, (0, 0)) if run_id else (0, 0)
        by_label[(label, bit)].append((ts, kind, outcome, n_wrong))
        rows.append({"label": label, "bit_index": bit, "timestamp": ts, "kind": kind,
                     "run_id": run_id or "", "outcome": outcome or "",
                     "n_wrong": n_wrong, "n_poison": n_poison})

    def ch(kind, outcome):
        if outcome is None:
            return "-"
        if kind == "original":
            return "*" if outcome == "sdc" else "!"
        return {"masked_error": ".", "sdc": "S", "crash": "C"}.get(outcome, "T")

    width = max(len(l) for l, _ in by_label) if by_label else 10
    tot_done = sum(1 for r in rows if r["outcome"])
    print(f"Runs in DB: {n_runs} | manifest points: {len(rows)} | "
          f"matched to an Injection row: {len(used)} | masked runs without Injection row: {n_masked_noinj}")
    if inferred:
        print(f"NOTE: {len(unmatched)} unmatched points inferred as masked_error "
              f"(= masked runs without Injection row).")
    elif unmatched:
        print(f"WARNING: {len(unmatched)} points have no matching Injection row and cannot be "
              f"inferred (DB has {n_runs} runs for {len(rows)} points): shown as '-'.")
    print(f"Points with an outcome: {tot_done}/{len(rows)}\n")
    for (label, bit), pts in by_label.items():
        pts.sort()
        line = "".join(ch(k, o) for _, k, o, _ in pts)
        grid = [p for p in pts if p[1] == "grid" and p[2] is not None]
        sdc_t = [t for t, _, o, _ in grid if o == "sdc"]
        crash = sum(1 for _, _, o, _ in grid if o == "crash")
        window = f"{min(sdc_t)}-{max(sdc_t)}" if sdc_t else "-"
        sizes = sorted({n for _, _, o, n in pts if o == "sdc"})
        print(f"{label:<{width}} {bit:>8}  {line}  SDC {len(sdc_t)}/{len(grid)}"
              f"  crash {crash}  window {window}  elems {sizes}")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nPer-run results: {a.csv}")


if __name__ == "__main__":
    main()


    
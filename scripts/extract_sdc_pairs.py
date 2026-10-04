#!/usr/bin/env python3
"""
extract_sdc_pairs.py — pull SDC runs out of a multi-injection campaign DB and
write, for EACH such run, a separate pairs-file (one "timestamp bit_index" per
line) containing only that run's injections. Each file can be fed to:

    ./run_campaign.sh deterministic <pairs_file>

Output files are written into an output directory and named sequentially:
0001.txt, 0002.txt, ... On startup the script scans the output directory for
files named <digits>.txt, takes the highest number found and continues from
the next one, so re-running never overwrites earlier extractions.

Selection filters (all optional, AND'ed together):
  --min-hamming / --max-hamming   hamming_dist range (each bound inclusive)
  --min-run-id                    only SDCs from run_id >= this value
  --test-name/--cores/--warps/--threads   Campaign filters

Usage:
  # by hamming range
  ./extract_sdc_pairs.py --db campaign.db --min-hamming 1 --max-hamming 4 \
      -o sdc_pairs/

  # from a given run_id onwards (any hamming_dist)
  ./extract_sdc_pairs.py --db campaign.db --min-run-id 1500 -o sdc_pairs/

  # both, restricted to one campaign
  ./extract_sdc_pairs.py --db campaign.db --min-run-id 1500 --min-hamming 2 \
      --test-name sgemm_seu --cores 2 --warps 2 --threads 8 -o sdc_pairs/

Notes:
  - Runs here can have several Injection rows (e.g. a continuous fault
    schedule). An SDC can't be attributed to one specific injection, so each
    file lists ALL injections of the SDC's run as individual (cycle, bit_index)
    candidates. This does not reproduce the original multi-fault run.
  - One file per run_id: if a run has several matching SDC rows, it still
    produces a single file.
  - Pairs are de-duplicated within a file (same cycle+bit_index only once).
    There is no de-duplication across files.
  - A matching run with 0 injections produces no file (a warning is printed).
"""

import argparse
import os
import re
import sqlite3
import sys

FILE_RE = re.compile(r"^(\d+)\.txt$")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="SQLite database path")
    p.add_argument("-o", "--output-dir", required=True,
                   help="Output directory (created if missing); one NNNN.txt per SDC run")

    p.add_argument("--min-hamming", type=int, default=None,
                   help="Minimum hamming_dist (inclusive)")
    p.add_argument("--max-hamming", type=int, default=None,
                   help="Maximum hamming_dist (inclusive)")
    p.add_argument("--min-run-id", type=int, default=None,
                   help="Only consider SDCs from run_id >= this value")

    p.add_argument("--test-name", default=None, help="Filter by Campaign.test_name")
    p.add_argument("--cores", type=int, default=None, help="Filter by Campaign.cores")
    p.add_argument("--warps", type=int, default=None, help="Filter by Campaign.warps")
    p.add_argument("--threads", type=int, default=None, help="Filter by Campaign.threads")

    return p.parse_args()


def next_file_index(out_dir):
    """Return highest existing NNNN.txt number in out_dir + 1 (1 if none)."""
    highest = 0
    for name in os.listdir(out_dir):
        m = FILE_RE.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def main():
    args = parse_args()

    if (args.min_hamming is not None and args.max_hamming is not None
            and args.min_hamming > args.max_hamming):
        print("ERROR: --min-hamming must be <= --max-hamming", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    query = """
        SELECT s.run_id, s.hamming_dist
        FROM SDC s
        JOIN Run r ON r.run_id = s.run_id
        JOIN Campaign c ON c.id = r.cid
    """
    where = []
    params = []

    if args.min_hamming is not None:
        where.append("s.hamming_dist >= ?")
        params.append(args.min_hamming)
    if args.max_hamming is not None:
        where.append("s.hamming_dist <= ?")
        params.append(args.max_hamming)
    if args.min_run_id is not None:
        where.append("s.run_id >= ?")
        params.append(args.min_run_id)
    if args.test_name is not None:
        where.append("c.test_name = ?")
        params.append(args.test_name)
    if args.cores is not None:
        where.append("c.cores = ?")
        params.append(args.cores)
    if args.warps is not None:
        where.append("c.warps = ?")
        params.append(args.warps)
    if args.threads is not None:
        where.append("c.threads = ?")
        params.append(args.threads)

    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY s.run_id"

    cur.execute(query, params)
    sdc_rows = cur.fetchall()

    if not sdc_rows:
        print("No SDC rows found with the given filters.", file=sys.stderr)
        conn.close()
        sys.exit(0)

    # run_id -> hamming_dist values (run_ids come out in ascending order)
    sdc_by_run = {}
    for run_id, hamming in sdc_rows:
        sdc_by_run.setdefault(run_id, []).append(hamming)

    os.makedirs(args.output_dir, exist_ok=True)
    index = next_file_index(args.output_dir)
    first_index = index

    files_written = 0
    runs_without_injections = []

    for run_id, hammings in sdc_by_run.items():
        cur.execute(
            """
            SELECT cycle, bit_index
            FROM Injection
            WHERE run_id = ?
            ORDER BY cycle, bit_index
            """,
            (run_id,)
        )
        # de-dup within the run, keep order
        pairs = list(dict.fromkeys(cur.fetchall()))

        if not pairs:
            runs_without_injections.append(run_id)
            continue

        filename = f"{index:04d}.txt"
        path = os.path.join(args.output_dir, filename)
        with open(path, "x", encoding="utf-8") as f:
            for cycle, bit_index in pairs:
                f.write(f"{cycle} {bit_index}\n")

        hamming_str = ",".join(str(h) for h in sorted(set(hammings)))
        print(f"{filename} <- run_id {run_id} (hamming {hamming_str}, {len(pairs)} pairs)")

        index += 1
        files_written += 1

    conn.close()

    print(f"\nSDC rows matched: {len(sdc_rows)} (across {len(sdc_by_run)} runs)")
    if files_written:
        print(f"Files written to {args.output_dir}: {files_written} "
              f"({first_index:04d}.txt .. {index - 1:04d}.txt)")
    else:
        print(f"No files written to {args.output_dir}")
    if runs_without_injections:
        print(f"WARNING: {len(runs_without_injections)} run(s) with a matching SDC had "
              f"0 injections (no file written): {runs_without_injections}", file=sys.stderr)


if __name__ == "__main__":
    main()


    
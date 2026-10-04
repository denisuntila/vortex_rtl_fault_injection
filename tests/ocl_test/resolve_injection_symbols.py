#!/usr/bin/env python3
"""
resolve_injection_symbols.py — fill Injection.symbol_name for the injections
of runs with a given outcome (default: sdc), using VerilatorBitMapper.

Flow:
  1. select the injections of runs whose Run.outcome matches:
         SELECT i.id, i.run_id, i.bit_index
         FROM Injection i JOIN Run r ON r.run_id = i.run_id
         WHERE r.outcome = 'sdc'
     (equivalent to: select run_id from Run where outcome='sdc', then
      select bit_index from injection where run_id = ...)
  2. build the bit map ONCE from the Verilator header, resolve every distinct
     bit_index in one go (the mapper module is imported, not launched per bit,
     so there is no command-line length limit and the header is parsed once);
  3. write the signal name into Injection.symbol_name, one UPDATE per row id,
     in a single transaction.

By default only rows with symbol_name IS NULL are processed, so re-running is
cheap and idempotent. Use --force to recompute rows that already have a name.
Bit indices the mapper cannot resolve (e.g. out of range) are reported and left
NULL.

Usage:
  ./resolve_injection_symbols.py \
      --mapper path/to/your_mapper_script.py \
      --header path/to/Vrtlsim_shim___024root.h \
      --db tests/ocl_test/recheck_campaign.db

  ./resolve_injection_symbols.py --mapper ... --header ... --dry-run
"""

import argparse
import importlib.util
import sqlite3
import sys
from collections import Counter


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mapper", required=True,
                   help="Path to the script that defines VerilatorBitMapper")
    p.add_argument("--header", required=True,
                   help="Path to Vrtlsim_shim___024root.h")
    p.add_argument("--db", default="tests/ocl_test/recheck_campaign.db",
                   help="SQLite DB (default: %(default)s)")
    p.add_argument("--class-name", default="Vrtlsim_shim___024root",
                   help="Verilator root class name (default: %(default)s)")
    p.add_argument("--outcome", default="sdc",
                   help="Run.outcome to process (default: %(default)s)")
    p.add_argument("--force", action="store_true",
                   help="Also recompute rows whose symbol_name is already set")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and report, but do not write to the DB")
    return p.parse_args()


def load_mapper_class(path):
    spec = importlib.util.spec_from_file_location("bit_mapper_module", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load a module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VerilatorBitMapper


def main():
    args = parse_args()

    try:
        mapper_cls = load_mapper_class(args.mapper)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot import VerilatorBitMapper from {args.mapper}: {e}", file=sys.stderr)
        sys.exit(1)

    mapper = mapper_cls(args.header)
    try:
        mapper.build_memory_map(args.class_name)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: failed to build memory map: {e}", file=sys.stderr)
        sys.exit(1)

    # timeout: the DB may be in use by running recheck instances
    conn = sqlite3.connect(args.db, timeout=60)
    cur = conn.cursor()

    cols = [r[1].lower() for r in cur.execute("PRAGMA table_info(Injection)")]
    if "symbol_name" not in cols:
        print("ERROR: table Injection has no symbol_name column", file=sys.stderr)
        sys.exit(1)

    sql = """
        SELECT i.id, i.run_id, i.bit_index
        FROM Injection i
        JOIN Run r ON r.run_id = i.run_id
        WHERE r.outcome = ?
    """
    if not args.force:
        sql += " AND i.symbol_name IS NULL"
    sql += " ORDER BY i.id"

    rows = cur.execute(sql, (args.outcome,)).fetchall()
    if not rows:
        print(f"No injection rows to process for outcome '{args.outcome}'"
              f"{'' if args.force else ' with symbol_name IS NULL'}.")
        conn.close()
        return

    unique_bits = sorted({bit for _, _, bit in rows})
    resolved = {}
    failed = {}
    for bit in unique_bits:
        res = mapper.query_bit(bit)
        if "signal_name" in res:
            resolved[bit] = res["signal_name"]
        else:
            failed[bit] = res.get("error", "unknown error")

    updates = [(resolved[bit], row_id) for row_id, _, bit in rows if bit in resolved]
    unresolved_rows = len(rows) - len(updates)

    print(f"Runs with outcome '{args.outcome}': {len({r for _, r, _ in rows})}")
    print(f"Injection rows selected: {len(rows)} ({len(unique_bits)} distinct bit_index)")
    print(f"Resolved: {len(resolved)} bits -> {len(updates)} rows | "
          f"unresolved: {len(failed)} bits -> {unresolved_rows} rows")
    for bit, err in list(failed.items())[:10]:
        print(f"  bit {bit}: {err}")

    if resolved:
        counts = Counter(name for name, _ in updates)
        print("\nTop signals:")
        for name, c in counts.most_common(10):
            print(f"  {c:>5}  {name}")

    if args.dry_run:
        print("\nDry run: nothing written.")
        conn.close()
        return

    with conn:
        conn.executemany("UPDATE Injection SET symbol_name = ? WHERE id = ?", updates)
    print(f"\nUpdated symbol_name on {len(updates)} rows in {args.db}")
    conn.close()


if __name__ == "__main__":
    main()

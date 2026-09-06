#!/usr/bin/env python3
"""
Script che:
1. Interroga il database SQLite per trovare tutte le Injection legate a run
   il cui esito è un SDC con actual_v != '0xbaadf00d' e hamming_dist < 4.
2. Concatena i bit_index trovati separati da virgola e li passa a find_bit.py
   insieme al path dell'header Vrtlsim_shim generato da Verilator.
3. Parsa il JSON restituito da find_bit.py e aggiorna la colonna symbol_name
   nella tabella Injection, solo dove è ancora NULL.

Uso:
    python3 update_symbol_names.py <path_database.db> <path_Vrtlsim_shim___024root.h> \
        [--find-bit-script find_bit.py]
"""

import argparse
import json
import sqlite3
import subprocess
import sys

ACTUAL_V_EXCLUDED = "0xbaadf00d"
HAMMING_DIST_MAX = 4  # esclusivo: hamming_dist < 4


def get_bit_indices(conn: sqlite3.Connection) -> list[int]:
    query = """
        SELECT DISTINCT I.bit_index
        FROM Injection AS I
        JOIN SDC AS S ON S.run_id = I.run_id
        WHERE S.actual_v IS NOT NULL
          AND S.actual_v != ?
          AND S.hamming_dist IS NOT NULL
          AND S.hamming_dist < ?
        ORDER BY I.bit_index
    """
    cur = conn.execute(query, (ACTUAL_V_EXCLUDED, HAMMING_DIST_MAX))
    return [row[0] for row in cur.fetchall()]


def run_find_bit(find_bit_script: str, vrtlsim_header: str, bit_indices: list[int]) -> list[dict]:
    bit_str = ",".join(str(b) for b in bit_indices)
    cmd = ["python3", find_bit_script, vrtlsim_header, "-b", bit_str]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Errore nell'esecuzione di {find_bit_script}:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    output = result.stdout
    # find_bit.py stampa una riga di log prima del JSON (es. "[Mapper] ..."),
    # quindi isoliamo il JSON a partire dalla prima parentesi quadra.
    json_start = output.find("[")
    if json_start == -1:
        print("Impossibile trovare l'output JSON di find_bit.py:", file=sys.stderr)
        print(output, file=sys.stderr)
        sys.exit(1)

    return json.loads(output[json_start:])


def update_symbol_names(conn: sqlite3.Connection, entries: list[dict]) -> int:
    updated = 0
    for entry in entries:
        bit_index = entry.get("global_bit_index")
        signal_name = entry.get("signal_name")
        if bit_index is None or signal_name is None:
            continue
        cur = conn.execute(
            "UPDATE Injection SET symbol_name = ? WHERE bit_index = ? AND symbol_name IS NULL",
            (signal_name, bit_index),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def main():
    parser = argparse.ArgumentParser(
        description="Aggiorna i symbol_name delle Injection legate a SDC filtrati, usando find_bit.py"
    )
    parser.add_argument("db", help="Path al database SQLite")
    parser.add_argument("vrtlsim_header", help="Path al file Vrtlsim_shim___024root.h")
    parser.add_argument(
        "--find-bit-script",
        default="find_bit.py",
        help="Path allo script find_bit.py (default: find_bit.py nella cwd)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")

    bit_indices = get_bit_indices(conn)
    if not bit_indices:
        print("Nessuna Injection trovata per i criteri richiesti.")
        conn.close()
        return

    print(f"Trovati {len(bit_indices)} bit_index distinti da risolvere.")

    entries = run_find_bit(args.find_bit_script, args.vrtlsim_header, bit_indices)
    print(f"find_bit.py ha restituito {len(entries)} entry.")

    updated = update_symbol_names(conn, entries)
    print(f"Aggiornati {updated} record nella tabella Injection (symbol_name era NULL).")

    conn.close()


if __name__ == "__main__":
    main()



